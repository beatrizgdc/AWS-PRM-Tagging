"""Pré-checagem de uma conta antes do deploy da Etapa 3 — somente leitura.

Responde duas perguntas, por conta:

1. **Há uma trail do CloudTrail registrando eventos de gerenciamento de
   ESCRITA em cada região?** Sem isso, as regras "AWS API Call via
   CloudTrail" do EventBridge não recebem eventos (exigência documentada
   pela AWS). A regra nativa de EC2
   continua funcionando sem trail; o resto da Etapa 3 não.
2. **Em quais regiões implantar a stack regional?** Regiões ativas da
   conta (mesmo critério da Etapa 1) + `us-east-1` SEMPRE — é a única
   região onde o EventBridge recebe eventos de serviços globais (Route 53,
   CloudFront), mesmo que a conta não tenha nada lá.

Nenhuma escrita. Permissões: `ec2:DescribeRegions`,
`cloudtrail:DescribeTrails`, `cloudtrail:GetTrailStatus`,
`cloudtrail:GetEventSelectors`.
"""
from __future__ import annotations

import logging

import boto3
from botocore.exceptions import ClientError

from .regions import get_active_regions

logger = logging.getLogger(__name__)

REGIAO_OBRIGATORIA = "us-east-1"


def _registra_escrita(client, trail_arn: str) -> bool | None:
    """True se os seletores da trail incluem eventos de gerenciamento de
    escrita; None se não foi possível ler."""
    try:
        resp = client.get_event_selectors(TrailName=trail_arn)
    except ClientError:
        return None
    basicos = resp.get("EventSelectors") or []
    avancados = resp.get("AdvancedEventSelectors") or []
    if not basicos and not avancados:
        # Nenhum seletor configurado = configuração padrão da trail, que
        # registra TODOS os eventos de gerenciamento (leitura e escrita).
        return True
    for sel in basicos:
        if sel.get("IncludeManagementEvents", True) and sel.get("ReadWriteType", "All") in ("All", "WriteOnly"):
            return True
    for adv in avancados:
        campos = {f["Field"]: f for f in adv.get("FieldSelectors", [])}
        categoria = campos.get("eventCategory", {}).get("Equals", [])
        if "Management" not in categoria:
            continue
        somente_leitura = campos.get("readOnly", {}).get("Equals", [])
        if somente_leitura != ["true"]:
            return True
    return False


def _avaliar_trails(session: boto3.Session, regiao_base: str) -> list[dict]:
    client = session.client("cloudtrail", region_name=regiao_base)
    trails = client.describe_trails(includeShadowTrails=True).get("trailList", [])
    avaliadas: list[dict] = []
    for t in trails:
        arn = t.get("TrailARN")
        home = t.get("HomeRegion")
        cliente_home = session.client("cloudtrail", region_name=home) if home else client
        try:
            logging_ativo = cliente_home.get_trail_status(Name=arn).get("IsLogging")
        except ClientError as exc:
            # Trail de organização vista de uma conta-membro pode negar a
            # leitura de status — registra como desconhecido, não como falso.
            logger.warning("Sem acesso ao status da trail %s: %s", arn, exc.response.get("Error", {}).get("Code"))
            logging_ativo = None
        avaliadas.append(
            {
                "arn": arn,
                "home_region": home,
                "multi_regiao": bool(t.get("IsMultiRegionTrail")),
                "organizacao": bool(t.get("IsOrganizationTrail")),
                "logging_ativo": logging_ativo,
                "registra_escrita": _registra_escrita(cliente_home, arn),
            }
        )
    return avaliadas


def _status_regiao(regiao: str, trails: list[dict]) -> str:
    """ok | incerto | sem_trail — para UMA região."""
    candidatas = [t for t in trails if t["multi_regiao"] or t["home_region"] == regiao]
    if any(t["logging_ativo"] is True and t["registra_escrita"] is True for t in candidatas):
        return "ok"
    if any(t["logging_ativo"] is not False and t["registra_escrita"] is not False for t in candidatas):
        return "incerto"
    return "sem_trail"


def run_preflight(session: boto3.Session, regiao_base: str = REGIAO_OBRIGATORIA) -> dict:
    ativas = get_active_regions(session, regiao_base)
    regioes_stackset = sorted(set(ativas) | {REGIAO_OBRIGATORIA})
    trails = _avaliar_trails(session, regiao_base)
    por_regiao = {r: _status_regiao(r, trails) for r in regioes_stackset}
    sem = sorted(r for r, st in por_regiao.items() if st == "sem_trail")
    incertas = sorted(r for r, st in por_regiao.items() if st == "incerto")

    recomendacoes: list[str] = []
    if sem:
        recomendacoes.append(
            "Sem trail registrando eventos de escrita em: " + ", ".join(sem) + ". As regras de CloudTrail da "
            "Etapa 3 não receberão eventos nessas regiões (a regra nativa de EC2 funciona). Solução: uma trail "
            "multi-região com eventos de gerenciamento (a primeira cópia é gratuita; há custo de armazenamento "
            "no S3) — ou confirmar se já existe uma trail de organização."
        )
    if incertas:
        recomendacoes.append(
            "Status incerto (sem permissão para ler a trail — comum em trail de organização vista da conta-membro) "
            "em: " + ", ".join(incertas) + ". Confirmar na conta de gerenciamento."
        )
    return {
        "regioes_ativas": ativas,
        "regioes_stackset": regioes_stackset,
        "trails": trails,
        "cloudtrail_por_regiao": por_regiao,
        "pronto_para_deploy": not sem,
        "recomendacoes": recomendacoes,
    }
