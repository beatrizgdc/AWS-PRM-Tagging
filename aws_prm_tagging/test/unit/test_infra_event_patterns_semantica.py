"""Semântica dos event patterns: um evento REAL casa com alguma regra?

Os testes de `test_infra_event_patterns.py` garantem que o template bate
com os arquivos gerados — não que o pattern casa com o evento que o
CloudTrail de fato produz. Foi exatamente essa lacuna que deixou o Lambda
fora da Etapa 3 (o CloudTrail grava `CreateFunction20150331`). Aqui o
casamento é avaliado pelo matcher de patterns do `moto` (implementa `$or`,
`exists` etc.), 100% offline. Pula se `moto` não estiver instalado.
"""
from __future__ import annotations

import json

import pytest

moto_events = pytest.importorskip("moto.events.models")

from aws_prm_tagging import event_mapping  # noqa: E402

_BASE = {
    "id": "1",
    "account": "000000000000",
    "time": "2026-09-30T00:00:00Z",
    "region": "us-east-1",
    "resources": [],
    "version": "0",
}


def _ct(event_source: str, event_name: str, error_code: str | None = None, source: str | None = None) -> dict:
    detail = {"eventSource": event_source, "eventName": event_name}
    if error_code:
        detail["errorCode"] = error_code
    return {
        **_BASE,
        "source": source or f"aws.{event_source.split('.')[0]}",
        "detail-type": "AWS API Call via CloudTrail",
        "detail": detail,
    }


def _casa_alguma_regra(evento: dict) -> bool:
    patterns = [moto_events.EventPattern.load(json.dumps(p)) for p in event_mapping.build_event_patterns()]
    return any(p.matches_event(evento) for p in patterns)


@pytest.mark.parametrize(
    "evento",
    [
        _ct("lambda.amazonaws.com", "CreateFunction20150331"),  # nome REAL do CloudTrail
        _ct("cloudfront.amazonaws.com", "CreateDistributionWithTags2020_05_31"),
        _ct("s3.amazonaws.com", "CreateBucket"),
        _ct("rds.amazonaws.com", "RestoreDBInstanceToPointInTime"),
        _ct("rds.amazonaws.com", "CreateDBInstanceReadReplica"),
        _ct("dynamodb.amazonaws.com", "RestoreTableFromBackup"),
        _ct("elasticache.amazonaws.com", "CreateReplicationGroup"),
        _ct("ec2.amazonaws.com", "CreateNatGateway"),
    ],
    ids=lambda e: e["detail"]["eventName"],
)
def test_evento_de_criacao_casa_com_alguma_regra(evento):
    assert _casa_alguma_regra(evento)


@pytest.mark.parametrize(
    "evento,motivo",
    [
        (_ct("s3.amazonaws.com", "CreateBucket", error_code="AccessDenied"), "chamada que falhou"),
        (_ct("ec2.amazonaws.com", "RunInstances"), "instância vem pelo evento nativo, não CloudTrail"),
        (_ct("lambda.amazonaws.com", "CreateFunction20150331", source="aws.s3"), "source e eventName descorrelacionados"),
    ],
    ids=["falhou", "run_instances", "source_trocado"],
)
def test_evento_que_nao_deve_casar(evento, motivo):
    assert not _casa_alguma_regra(evento), motivo


@pytest.mark.parametrize("estado,esperado", [("running", True), ("pending", False), ("stopped", False)])
def test_regra_nativa_ec2_so_casa_running(estado, esperado):
    pattern = moto_events.EventPattern.load(json.dumps(event_mapping.NATIVE_EVENT_PATTERNS["ec2_instance_running"]))
    evento = {
        **_BASE,
        "source": "aws.ec2",
        "detail-type": "EC2 Instance State-change Notification",
        "detail": {"instance-id": "i-0abc", "state": estado},
    }
    assert pattern.matches_event(evento) is esperado
