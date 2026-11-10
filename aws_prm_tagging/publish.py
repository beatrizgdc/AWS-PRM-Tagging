"""Publicação de resultados no tópico SNS (reporte por push).

Núcleo compartilhado pelas Etapas 3 e 4 e pelo tagueamento inicial/varredura.
Toda mensagem leva o atributo `prm_tipo`, que as assinaturas de e-mail usam
como filtro:

- `alerta`: pede ação agora (falha, conflito com outro parceiro, execução
  com falhas). Sempre vai para o e-mail.
- `resumo`: resumo de uma execução (tagueamento inicial ou varredura).
- `resultado`: um recurso tratado sem problema (tagueado, já ok, IaC).
- `resumo_diario`: o consolidado do dia (stack central).

Com a stack central, o e-mail recebe só `alerta` e `resumo_diario`; o resto
entra no resumo diário.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .retry import with_backoff

logger = logging.getLogger(__name__)


TIPO_ALERTA = "alerta"
TIPO_RESUMO = "resumo"
TIPO_RESULTADO = "resultado"
TIPO_RESUMO_DIARIO = "resumo_diario"
ATRIBUTO_TIPO = "prm_tipo"

_CATEGORIAS_ALERTA = frozenset({"falhou", "erro_classificacao"})


def tipo_mensagem(payload: dict[str, Any]) -> str:
    """Classifica a mensagem para o filtro das assinaturas de e-mail."""
    if "por_categoria" in payload:  # resumo de execução por região
        falhas_descoberta = payload.get("total_falhas_descoberta") and not payload.get("regiao_bloqueada_por_scp")
        problema = (
            falhas_descoberta
            or payload.get("total_falhas_escrita")
            or payload.get("dlq_mensagens")
            or payload.get("stack_regional_detectada") is False
        )
        return TIPO_ALERTA if problema else TIPO_RESUMO
    categoria = str(payload.get("categoria_final") or "")
    if categoria in _CATEGORIAS_ALERTA:
        return TIPO_ALERTA
    if categoria.startswith("drift_"):
        return TIPO_ALERTA if payload.get("possivel_disputa_com_outro_parceiro") else TIPO_RESULTADO
    if payload.get("acao") == "sobrescrita":  # havia valor de outro parceiro
        return TIPO_ALERTA
    return TIPO_RESULTADO


@with_backoff()
def _sns_publish(sns_client, topic_arn: str, message: str, subject: str, tipo: str | None = None) -> None:
    kwargs = {}
    if tipo:
        kwargs["MessageAttributes"] = {ATRIBUTO_TIPO: {"DataType": "String", "StringValue": tipo}}
    sns_client.publish(TopicArn=topic_arn, Message=message, Subject=subject[:100], **kwargs)


def build_outcome_payload(
    *,
    etapa: str,
    conta_id: str | None,
    arn: str | None,
    servico: str | None,
    regiao: str | None,
    tipo_recurso: str | None,
    categoria_final: str | None,
    origem: str | None,
    estrategia_api: str | None = None,
    resultado: str | None = None,
    detalhe_erro: dict | None = None,
    motivo: str | None = None,
    estado_anterior: dict | None = None,
    acao: str | None = None,
    iac: dict | None = None,
    event_id: str | None = None,
    event_name: str | None = None,
    event_source: str | None = None,
) -> dict[str, Any]:
    """Monta o payload publicado no tópico — mesmos campos que
    `tag_execution.build_execution_report` já usa em cada entrada de
    `recursos[]` (`arn`/`servico`/`regiao`/`tipo_recurso`/`categoria_final`/
    `origem`/`estrategia_api`/`resultado`/`detalhe_erro`/`motivo` — mesma
    taxonomia, para que o consumidor do lado do dashboard nunca precise
    tratar Etapa 2c e Etapa 3 como coisas diferentes), mais os campos
    próprios da Etapa 3: `etapa`, identificadores do evento de origem
    (`event_id`/`event_name`/`event_source`) e `publicado_em`."""
    return {
        "etapa": etapa,
        "conta_id": conta_id,
        "arn": arn,
        "servico": servico,
        "regiao": regiao,
        "tipo_recurso": tipo_recurso,
        "categoria_final": categoria_final,
        "origem": origem,
        "estrategia_api": estrategia_api,
        "resultado": resultado,
        "detalhe_erro": detalhe_erro,
        "motivo": motivo,
        # O que havia antes (valor de outro parceiro / chave parecida), o que a
        # escrita fez e, para IaC, a ferramenta e a evidência.
        "estado_anterior": estado_anterior,
        "acao": acao,
        "iac": iac,
        "event_id": event_id,
        "event_name": event_name,
        "event_source": event_source,
        "publicado_em": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def publish_outcome(sns_client, topic_arn: str, payload: dict[str, Any]) -> None:
    """Publica um único resultado no tópico SNS central.

    `retry.with_backoff` absorve throttling; qualquer outro erro (ex.:
    tópico sem permissão, ARN inválido) propaga para o chamador — publicar
    o resultado de compliance não é opcional, então uma falha aqui deve
    ficar visível (log de erro da Lambda / execução do Step Functions
    marcada como falha), nunca engolida silenciosamente."""
    tipo = tipo_mensagem(payload)
    prefixo = "PRM ALERTA" if tipo == TIPO_ALERTA else "PRM"
    subject = f"{prefixo} Etapa {payload.get('etapa', '?')} - {payload.get('categoria_final', 'resultado')}"
    _sns_publish(sns_client, topic_arn, json.dumps(payload, ensure_ascii=False), subject, tipo)
