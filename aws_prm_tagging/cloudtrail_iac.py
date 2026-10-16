"""Detecção de IaC pelo histórico do CloudTrail (MVP2).

O Terraform não deixa nenhuma tag no recurso por padrão, então a heurística
por tags (`iac_detection.detect_iac`) perde boa parte dos recursos do
Terraform. Mas toda chamada do provider AWS do Terraform/OpenTofu/Pulumi se
identifica no `userAgent` gravado pelo CloudTrail.

Este módulo consulta o **Event history** do CloudTrail
(`cloudtrail:LookupEvents`): gratuito, somente leitura, últimos 90 dias de
eventos de gerenciamento, sem precisar de trail configurado. Para cada
recurso candidato a tagueamento, procura eventos de ESCRITA sobre ele e
aplica `iac_detection.detect_iac_from_cloudtrail` a cada um. Basta um
evento de escrita feito por IaC (criação, alteração, tag) para tratar o
recurso como gerenciado por IaC. Eventos de leitura são ignorados: um
data source do Terraform lê recursos que ele não gerencia.

Limites conhecidos:
- `LookupEvents` aceita 2 chamadas/s por conta e região. Por isso há um
  orçamento de tempo por execução (`orcamento_segundos`); o que não couber
  fica `nao_verificado` no relatório (cai na heurística de tags).
- Recurso criado há mais de 90 dias e sem alteração recente não tem
  evento: fica `sem_sinal` e vale a heurística de tags.
- O nome usado pelo CloudTrail em `Resources` varia por serviço (ID, nome
  ou ARN). Tentamos o ARN e depois o identificador final do ARN.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

import boto3
from botocore.exceptions import ClientError

from .iac_detection import IAC_DESCONHECIDO, detect_iac_from_cloudtrail
from .retry import with_backoff

logger = logging.getLogger(__name__)

HISTORICO_DETECTADO = "detectado"
HISTORICO_SEM_SINAL = "sem_sinal"
HISTORICO_NAO_VERIFICADO = "nao_verificado"
HISTORICO_ERRO = "erro"

_MAX_PAGINAS = 3  # até 150 eventos mais recentes por nome consultado


def _lookup_bruto(client, nome: str, token: str | None):
    kwargs = {"LookupAttributes": [{"AttributeKey": "ResourceName", "AttributeValue": nome}], "MaxResults": 50}
    if token:
        kwargs["NextToken"] = token
    return client.lookup_events(**kwargs)


# Mapeamento (sem pressa): insiste mais no throttling. Etapa 3 (dentro de
# uma Lambda com timeout): desiste rápido e cai na heurística de tags.
_lookup = with_backoff(max_attempts=8, base_delay=1.0)(_lookup_bruto)
_lookup_rapido = with_backoff(max_attempts=4, base_delay=0.5, max_delay=4.0)(_lookup_bruto)


def nomes_para_consulta(arn: str) -> list[str]:
    """ARN completo e o identificador final (ex.: `i-0abc`, nome do bucket,
    nome da função), sem repetição."""
    nomes = [arn]
    resto = arn.split(":", 5)[5] if arn.count(":") >= 5 else arn
    final = resto.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if final and final not in nomes:
        nomes.append(final)
    return nomes


def iac_por_historico(client, arn: str, rapido: bool = False) -> dict | None:
    """Primeiro sinal de IaC num evento de escrita sobre o recurso, ou None.
    Propaga ClientError (o chamador decide o que fazer). `rapido=True`
    (Etapa 3): menos tentativas no throttling e só a página mais recente."""
    consulta = _lookup_rapido if rapido else _lookup
    for nome in nomes_para_consulta(arn):
        token = None
        for _ in range(1 if rapido else _MAX_PAGINAS):
            resp = consulta(client, nome, token)
            for ev in resp.get("Events", []):
                if str(ev.get("ReadOnly", "")).lower() == "true":
                    continue
                try:
                    detalhe = json.loads(ev.get("CloudTrailEvent") or "{}")
                except ValueError:
                    continue
                if detalhe.get("readOnly") is True:
                    continue
                achado = detect_iac_from_cloudtrail(detalhe)
                if achado:
                    achado["evidencia"] = f"{ev.get('EventName')} em {ev.get('EventTime')}: {achado['evidencia']}"
                    return achado
            token = resp.get("NextToken")
            if not token:
                break
    return None


def enriquecer_iac(
    session: boto3.Session,
    recursos: list[dict],
    orcamento_segundos: float | None = None,
    relogio: Callable[[], float] = time.monotonic,
) -> dict:
    """Consulta o histórico só para quem a automação TAGUEARIA: IaC ainda
    `desconhecido` e tag ausente ou com outro valor. Altera `recurso["iac"]`
    no lugar quando acha IaC e grava `recurso["iac_historico"]`. Devolve
    estatísticas para o relatório (inclusive falhas por região)."""
    inicio = relogio()
    stats = {"consultados": 0, "detectados": 0, "nao_verificados": 0, "erros": 0, "falhas": []}
    clientes: dict[str, object] = {}
    regioes_com_erro: set[str] = set()
    for r in recursos:
        iac = r.get("iac") or {}
        if iac.get("tipo") != IAC_DESCONHECIDO or r.get("status_tag") == "ok":
            continue
        regiao = r.get("regiao")
        estourou = orcamento_segundos is not None and relogio() - inicio > orcamento_segundos
        if estourou or not regiao or regiao in regioes_com_erro:
            r["iac_historico"] = HISTORICO_NAO_VERIFICADO
            stats["nao_verificados"] += 1
            continue
        client = clientes.get(regiao)
        if client is None:
            client = clientes[regiao] = session.client("cloudtrail", region_name=regiao)
        try:
            achado = iac_por_historico(client, r["arn"])
        except ClientError as exc:
            erro = exc.response.get("Error", {})
            codigo = erro.get("Code", "")
            # Permissão negada/SCP: inútil insistir na região.
            if "AccessDenied" in codigo or codigo == "UnauthorizedOperation":
                regioes_com_erro.add(regiao)
            stats["erros"] += 1
            stats["falhas"].append(
                {"regiao": regiao, "etapa": "historico_cloudtrail", "erro": f"{codigo}: {erro.get('Message', '')}"}
            )
            r["iac_historico"] = HISTORICO_ERRO
            continue
        except Exception as exc:  # noqa: BLE001 — enriquecimento nunca derruba a descoberta
            logger.exception("Falha inesperada no histórico do CloudTrail em %s — região ignorada", regiao)
            regioes_com_erro.add(regiao)
            stats["erros"] += 1
            stats["falhas"].append({"regiao": regiao, "etapa": "historico_cloudtrail", "erro": f"{type(exc).__name__}: {exc}"})
            r["iac_historico"] = HISTORICO_ERRO
            continue
        stats["consultados"] += 1
        if achado:
            r["iac"] = {**iac, **achado, "stack_name": iac.get("stack_name"),
                        "gerenciado_por_ferramenta_aws": iac.get("gerenciado_por_ferramenta_aws")}
            r["iac_historico"] = HISTORICO_DETECTADO
            stats["detectados"] += 1
        else:
            r["iac_historico"] = HISTORICO_SEM_SINAL
    if stats["nao_verificados"]:
        logger.warning(
            "Histórico do CloudTrail: %d recurso(s) não verificados (orçamento de tempo ou erro na região) — "
            "para eles vale só a heurística de tags.",
            stats["nao_verificados"],
        )
    logger.info(
        "Histórico do CloudTrail: %d consultado(s), %d com IaC detectado, %d erro(s)",
        stats["consultados"], stats["detectados"], stats["erros"],
    )
    # Uma entrada de falha por região basta no relatório.
    vistas: set[str] = set()
    stats["falhas"] = [f for f in stats["falhas"] if not (f["regiao"] in vistas or vistas.add(f["regiao"]))]
    return stats
