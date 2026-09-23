"""Publicação de resultados individuais no tópico SNS central — o modelo de
"reporte por push" já descrito em
[docs/arquitetura-multicliente.md](docs/arquitetura-multicliente.md), mas
sem nenhuma implementação no repositório antes deste módulo (`report.py`,
Etapa 1, só grava JSON local; `tag_execution.py`, Etapas 2b/2c, só devolve
um dict — nenhum dos dois publica nada).

Construído como núcleo compartilhado (mesmo nível de `report.py`/
`decision.py`), não acoplado à Etapa 3 de propósito: a Etapa 4 (varredura
recorrente, fora do escopo deste projeto por ora) vai precisar publicar
exatamente esta mesma forma de evento — reaproveitar isso depois evita
duplicar o código de publicação entre as duas etapas.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .retry import with_backoff

logger = logging.getLogger(__name__)


@with_backoff()
def _sns_publish(sns_client, topic_arn: str, message: str, subject: str) -> None:
    sns_client.publish(TopicArn=topic_arn, Message=message, Subject=subject[:100])


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
    subject = f"PRM Etapa {payload.get('etapa', '?')} - {payload.get('categoria_final', 'resultado')}"
    _sns_publish(sns_client, topic_arn, json.dumps(payload, ensure_ascii=False), subject)
