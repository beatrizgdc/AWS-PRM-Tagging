"""Decisão de tagueamento (Etapa 2a) a partir do relatório da Etapa 1.

Módulo puro: sem rede, sem boto3, sem I/O (quem lê e grava arquivos é `main.py`).

Cada recurso recebe uma decisão; a primeira regra que casar vale:

1. `ja_ok` — `aws-apn-id` já tem o valor esperado.
2. `pular_iac` — gerenciado por IaC (Terraform/OpenTofu, CloudFormation,
   CDK, Pulumi), com ou sem tag. Nunca é tagueado: o próximo apply desfaria
   a tag. Vai para a lista do cliente.
3. `sobrescrever` — `aws-apn-id` com outro valor (outro parceiro). O valor
   anterior fica em `estado_anterior`.
4. `taguear_com_tag_similar` — sem `aws-apn-id`, mas com chave de grafia
   parecida (ex.: `AWS-APN-ID`, que a AWS não reconhece). A nossa é
   adicionada ao lado; a parecida fica como está e vai para `estado_anterior`.
5. `taguear` — sem tag (IaC "desconhecido" conta como não detectado).

O status da tag é recalculado aqui contra o `expected_tag_value` desta
chamada, e não lido do `status_tag` da Etapa 1: assim um mesmo mapeamento
serve a contratos diferentes.

`_em_escopo` exclui sub-recursos de EKS e Bedrock fora dos tipos tagueáveis;
eles só entram na contagem `total_excluidos_fora_de_escopo`.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .iac_detection import IAC_DETECTADO, TODOS_OS_TIPOS_IAC
from .tag_status import TAG_KEY, STATUS_CONFLITO, STATUS_OK, STATUS_SEM_TAG, get_tag_status

DECISAO_TAGUEAR = "taguear"
DECISAO_TAGUEAR_COM_TAG_SIMILAR = "taguear_com_tag_similar"
DECISAO_SOBRESCREVER = "sobrescrever"
DECISAO_PULAR_IAC = "pular_iac"
DECISAO_JA_OK = "ja_ok"

# Decisões que geram escrita (Etapas 2b/2c).
DECISOES_QUE_TAGUEIAM = frozenset({DECISAO_TAGUEAR, DECISAO_TAGUEAR_COM_TAG_SIMILAR, DECISAO_SOBRESCREVER})

# Decisões do MVP1, mantidas só para `tag_execution` recusar relatórios antigos.
DECISAO_LEGADA_CONFLITO = "conflito"
DECISAO_LEGADA_REVISAR_TAG_SIMILAR = "revisar_tag_similar"
DECISOES_LEGADAS = frozenset({DECISAO_LEGADA_CONFLITO, DECISAO_LEGADA_REVISAR_TAG_SIMILAR})

_TODAS_AS_DECISOES = (
    DECISAO_TAGUEAR,
    DECISAO_TAGUEAR_COM_TAG_SIMILAR,
    DECISAO_SOBRESCREVER,
    DECISAO_PULAR_IAC,
    DECISAO_JA_OK,
)
_TODOS_OS_STATUS_IAC = TODOS_OS_TIPOS_IAC
_IAC_DETECTADO = IAC_DETECTADO

# Nomes exatamente como no CSV oficial.
_SERVICO_EKS = "Amazon EKS"
_SERVICO_BEDROCK = "Amazon Bedrock"

_EKS_TIPOS_EM_ESCOPO = {"cluster", "node_group", "node", "ebs_volume", "load_balancer"}
_BEDROCK_TIPOS_EM_ESCOPO = {"application_inference_profile"}


def _em_escopo(servico: str, tipo_recurso: str | None) -> bool:
    """Só EKS e Bedrock têm sub-recursos fora de escopo; os demais serviços entram inteiros."""
    if servico == _SERVICO_EKS:
        return tipo_recurso in _EKS_TIPOS_EM_ESCOPO
    if servico == _SERVICO_BEDROCK:
        return tipo_recurso in _BEDROCK_TIPOS_EM_ESCOPO
    return True


def _validar_recurso(resource: Any) -> str | None:
    """Mensagem de erro se o registro estiver malformado; `None` se estiver ok."""
    if not isinstance(resource, dict):
        return "entrada não é um objeto de recurso válido (esperado dict)"
    if not isinstance(resource.get("arn"), str) or not resource["arn"]:
        return "campo 'arn' ausente ou inválido"
    if not isinstance(resource.get("servico"), str) or not resource["servico"]:
        return "campo 'servico' ausente ou inválido"
    if not isinstance(resource.get("regiao"), str) or not resource["regiao"]:
        return "campo 'regiao' ausente ou inválido"
    status_tag = resource.get("status_tag")
    if status_tag not in (STATUS_SEM_TAG, STATUS_OK, STATUS_CONFLITO):
        return f"campo 'status_tag' ausente ou com valor inesperado: {status_tag!r}"
    iac = resource.get("iac")
    iac_tipo = iac.get("tipo") if isinstance(iac, dict) else None
    if iac_tipo not in _TODOS_OS_STATUS_IAC:
        return f"campo 'iac.tipo' ausente ou com valor inesperado: {iac_tipo!r}"
    return None


def _decidir(
    valor_tag_atual: str | None,
    expected_tag_value: str,
    iac_tipo: str,
    tag_similar_encontrada: bool,
    ferramenta_aws: str | None = None,
    ferramenta_iac: str | None = None,
) -> tuple[str, str]:
    """Aplica a precedência do módulo e devolve `(decisao, motivo)`.

    `ferramenta_aws`: stack gerada por Elastic Beanstalk, Control Tower,
    Service Catalog ou eksctl — o motivo aponta para configurar a tag na
    própria ferramenta."""
    tags_reconstruidas = {} if valor_tag_atual is None else {TAG_KEY: valor_tag_atual}
    status_tag, _ = get_tag_status(tags_reconstruidas, expected_tag_value)

    if status_tag == STATUS_OK:
        return DECISAO_JA_OK, "tag aws-apn-id já presente com o valor esperado"

    if iac_tipo in _IAC_DETECTADO:
        situacao = (
            "tag aws-apn-id ausente"
            if status_tag == STATUS_SEM_TAG
            else f"tag aws-apn-id com outro valor ({valor_tag_atual!r})"
        )
        if ferramenta_aws:
            return (
                DECISAO_PULAR_IAC,
                f"{situacao}; recurso gerenciado internamente por {ferramenta_aws} via "
                f"CloudFormation — configure a tag nas opções/config do {ferramenta_aws}; "
                "a automação não tagueia recurso de IaC",
            )
        ferramenta = ferramenta_iac or iac_tipo
        return (
            DECISAO_PULAR_IAC,
            f"{situacao}; recurso gerenciado por IaC ({ferramenta}) — o cliente deve "
            f"adicionar aws-apn-id={expected_tag_value} no código do IaC; a automação "
            "não tagueia recurso de IaC",
        )

    if status_tag == STATUS_CONFLITO:
        return (
            DECISAO_SOBRESCREVER,
            f"tag aws-apn-id com outro valor ({valor_tag_atual!r}) — sobrescrita com o "
            "valor esperado (permitido pela AWS); valor anterior registrado",
        )
    if tag_similar_encontrada:
        return (
            DECISAO_TAGUEAR_COM_TAG_SIMILAR,
            "tag aws-apn-id ausente, mas há chave de grafia parecida (a AWS só reconhece "
            "aws-apn-id minúsculo) — aws-apn-id adicionada; a chave parecida fica como "
            "estava e é listada no relatório",
        )
    return (
        DECISAO_TAGUEAR,
        "tag aws-apn-id ausente; recurso não gerenciado por IaC — elegível para tagueamento via API",
    )


def estado_anterior_do_recurso(resource: dict) -> dict | None:
    """Estado da tag antes da nossa escrita, para o relatório do cliente e
    o SNS. `None` quando não havia nada (nem aws-apn-id nem chave parecida)."""
    valor = resource.get("valor_tag_encontrado")
    chaves = list(resource.get("tag_similar_chaves") or [])
    valores = resource.get("tag_similar_valores") or {}
    if valor is None and not chaves:
        return None
    return {
        TAG_KEY: valor,
        "chaves_similares": {k: valores.get(k) for k in chaves},
    }


def classify_resource(resource: Any, expected_tag_value: str) -> dict | None:
    """Classifica um recurso da Etapa 1.

    Devolve `None` (fora de escopo), `{"arn", "servico", "erro"}` (malformado)
    ou o dict de decisão completo."""
    erro = _validar_recurso(resource)
    if erro is not None:
        arn = resource.get("arn") if isinstance(resource, dict) else None
        servico = resource.get("servico") if isinstance(resource, dict) else None
        return {"arn": arn, "servico": servico, "erro": erro}

    servico = resource["servico"]
    tipo_recurso = resource.get("tipo_recurso")
    if not _em_escopo(servico, tipo_recurso):
        return None

    iac = resource["iac"]
    valor_tag_atual = resource.get("valor_tag_encontrado")
    tag_similar_encontrada = bool(resource.get("tag_similar_encontrada", False))
    ferramenta_aws = iac.get("gerenciado_por_ferramenta_aws")
    decisao, motivo = _decidir(
        valor_tag_atual,
        expected_tag_value,
        iac["tipo"],
        tag_similar_encontrada,
        ferramenta_aws,
        iac.get("ferramenta"),
    )

    return {
        "arn": resource["arn"],
        "servico": servico,
        "regiao": resource["regiao"],
        "tipo_recurso": tipo_recurso,
        "valor_tag_atual": valor_tag_atual,
        "decisao": decisao,
        "motivo": motivo,
        "tag_similar_encontrada": tag_similar_encontrada,
        "tag_similar_chaves": list(resource.get("tag_similar_chaves", [])),
        "estado_anterior": None if decisao == DECISAO_JA_OK else estado_anterior_do_recurso(resource),
        # Informativos (não afetam a decisão).
        "servico_faturamento": resource.get("servico_faturamento") or servico,
        "cluster_eks": resource.get("cluster_eks"),
        "iac": {
            "tipo": iac["tipo"],
            "detectado": iac["tipo"] in _IAC_DETECTADO,
            "stack_name": iac.get("stack_name"),
            "gerenciado_por_ferramenta_aws": ferramenta_aws,
            "ferramenta": iac.get("ferramenta"),
            "evidencia": iac.get("evidencia"),
        },
    }


def build_decision_report(etapa1_report: dict, expected_tag_value: str) -> dict:
    """Relatório de decisão (entrada das Etapas 2b/2c).

    `descoberta_executada_em` guarda quando a Etapa 1 rodou, para o `apply`
    recusar descobertas velhas demais (`max_decision_age_hours`)."""
    resources = etapa1_report.get("recursos") or []

    decisoes: list[dict] = []
    erros: list[dict] = []
    total_excluidos = 0

    for resource in resources:
        resultado = classify_resource(resource, expected_tag_value)
        if resultado is None:
            total_excluidos += 1
        elif "erro" in resultado:
            erros.append(resultado)
        else:
            decisoes.append(resultado)

    decisao_counts = Counter(d["decisao"] for d in decisoes)
    servico_counts = Counter(d["servico"] for d in decisoes)
    iac_counts = Counter(d["iac"]["tipo"] for d in decisoes)

    return {
        "conta_id": etapa1_report.get("conta_id"),
        "executado_em": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "descoberta_executada_em": etapa1_report.get("executado_em"),
        "valor_tag_esperado": expected_tag_value,
        "resumo": {
            "total_recursos_avaliados": len(decisoes),
            "total_erros": len(erros),
            "total_excluidos_fora_de_escopo": total_excluidos,
            "por_decisao": {d: decisao_counts.get(d, 0) for d in _TODAS_AS_DECISOES},
            "por_servico": dict(sorted(servico_counts.items())),
            "por_status_iac": {t: iac_counts.get(t, 0) for t in _TODOS_OS_STATUS_IAC},
        },
        "recursos": decisoes,
        "erros": erros,
    }
