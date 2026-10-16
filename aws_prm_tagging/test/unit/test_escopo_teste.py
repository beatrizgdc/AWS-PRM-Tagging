"""FiltroEscopoTeste — proteção para sandbox compartilhada."""
from __future__ import annotations

import pytest

from aws_prm_tagging import escopo_teste, handler_continuous_tagging, handler_drift

EV_FULANO = {"detail": {"userIdentity": {"arn": "arn:aws:iam::1:user/fulano"}}}
EV_OUTRO = {"detail": {"userIdentity": {"arn": "arn:aws:iam::1:user/colega"}}}
EV_NATIVO = {"detail-type": "ECS Task State Change", "detail": {}}


@pytest.mark.parametrize(
    "filtro,evento,arn,esperado",
    [
        ("", EV_OUTRO, "arn:aws:s3:::qualquer", True),  # produção: sem filtro
        ("user/fulano", EV_FULANO, "arn:aws:s3:::qualquer", True),
        ("user/fulano", EV_OUTRO, "arn:aws:s3:::do-colega", False),
        ("user/fulano,prm-val", EV_NATIVO, "arn:aws:ecs:us-east-1:1:task/prm-val/abc", True),
        ("user/fulano,prm-val", EV_NATIVO, "arn:aws:ecs:us-east-1:1:task/prod/abc", False),
    ],
)
def test_dentro_do_escopo(monkeypatch, filtro, evento, arn, esperado):
    monkeypatch.setenv("FILTRO_ESCOPO", filtro)
    assert escopo_teste.dentro_do_escopo(evento, arn) is esperado


def test_etapa3_nao_toca_recurso_de_colega(monkeypatch, cloudtrail_event_factory):
    monkeypatch.setenv("FILTRO_ESCOPO", "user/fulano")
    monkeypatch.setenv("EXPECTED_TAG_VALUE", "pc:x")
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:000000000000:t")
    evento = cloudtrail_event_factory(event_source="s3.amazonaws.com", event_name="CreateBucket",
                                      detail_overrides={"requestParameters": {"bucketName": "do-colega"},
                                                        "userIdentity": {"arn": "arn:aws:iam::000000000000:user/colega"}})
    chamado = []
    monkeypatch.setattr(handler_continuous_tagging.tag_execution, "run_tagging_execution", lambda *a, **k: chamado.append(1))
    handler_continuous_tagging.handler(evento, session=object(), sns_client=object())
    assert chamado == [], "recurso de outra pessoa não pode ser processado com o filtro ativo"


def test_drift_ignora_recurso_de_colega(monkeypatch):
    monkeypatch.setenv("FILTRO_ESCOPO", "user/fulano")
    monkeypatch.setenv("EXPECTED_TAG_VALUE", "pc:x")
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:t")
    ev = {"detail": {"eventName": "DeleteBucketTagging", "eventSource": "s3.amazonaws.com", "awsRegion": "us-east-1",
                     "recipientAccountId": "1", "requestParameters": {"bucketName": "do-colega"},
                     "userIdentity": {"arn": "arn:aws:iam::1:user/colega"}}}
    assert handler_drift.handler(ev, session=object(), sns_client=object())["alertas"] == 0
