"""Testes de `publish.py` — montagem do payload e chamada de `sns:Publish`,
sem rede real."""
from __future__ import annotations

import json

from aws_prm_tagging.publish import build_outcome_payload, publish_outcome


class _FakeSnsClient:
    def __init__(self):
        self.calls: list[dict] = []

    def publish(self, TopicArn, Message, Subject):
        self.calls.append({"TopicArn": TopicArn, "Message": Message, "Subject": Subject})


def test_build_outcome_payload_inclui_todos_os_campos_esperados():
    payload = build_outcome_payload(
        etapa="3",
        conta_id="000000000000",
        arn="arn:aws:s3:::bucket",
        servico="Amazon S3",
        regiao="us-east-1",
        tipo_recurso=None,
        categoria_final="tagueado_sucesso",
        origem="execucao",
        estrategia_api="generico_tag_resources",
        resultado="tagueado_sucesso",
        detalhe_erro=None,
        motivo=None,
        event_id="abc-123",
        event_name="CreateBucket",
        event_source="s3.amazonaws.com",
    )
    assert payload["etapa"] == "3"
    assert payload["arn"] == "arn:aws:s3:::bucket"
    assert payload["categoria_final"] == "tagueado_sucesso"
    assert payload["event_id"] == "abc-123"
    assert "publicado_em" in payload


def test_publish_outcome_serializa_e_publica_no_topico():
    sns_client = _FakeSnsClient()
    payload = build_outcome_payload(
        etapa="3",
        conta_id="000000000000",
        arn="arn:aws:s3:::bucket",
        servico="Amazon S3",
        regiao="us-east-1",
        tipo_recurso=None,
        categoria_final="ja_ok",
        origem="revalidacao",
    )
    publish_outcome(sns_client, "arn:aws:sns:us-east-1:000000000000:topico", payload)

    assert len(sns_client.calls) == 1
    call = sns_client.calls[0]
    assert call["TopicArn"] == "arn:aws:sns:us-east-1:000000000000:topico"
    assert json.loads(call["Message"]) == payload
    assert "3" in call["Subject"]
    assert "ja_ok" in call["Subject"]
