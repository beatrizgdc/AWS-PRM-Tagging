"""Testes de ponta a ponta (com sessão/clients boto3 falsos, sem rede real)
do handler da Etapa 3 — cobre os 5 cenários centrais descritos no plano de
design: recurso em escopo sem IaC (tagueia), recurso gerenciado por IaC
(pula e sinaliza), recurso já tagueado corretamente (ja_ok, sem erro),
evento duplicado (idempotência) e evento fora do escopo do CSV (ignorado).
"""
from __future__ import annotations

import json

import pytest

from aws_prm_tagging.handler_continuous_tagging import handler

_EXPECTED = "pc:5ugbbrmu7ud3u5hsipfzug61p"


class _FakeGenericRwClient:
    """Cobre tanto a leitura (`single_resource.py` e a revalidação de
    `tag_execution.py`, ambas via `get_resources`) quanto a escrita
    (`tag_resources`) — o mesmo objeto de client é devolvido pela sessão
    falsa em toda chamada a `session.client("resourcegroupstaggingapi", ...)`,
    então precisa suportar as duas pontas para o fluxo completo do handler
    funcionar de ponta a ponta neste teste."""

    def __init__(self, tags_por_arn: dict[str, dict[str, str]]):
        self.tags_por_arn = tags_por_arn
        self.tag_resources_calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def get_resources(self, ResourceARNList=None, ResourcesPerPage=None, PaginationToken=None):
        # Dois chamadores, duas assinaturas: `single_resource.py` filtra por
        # `ResourceARNList`; a revalidação de `tag_execution.py` pagina a
        # região inteira sem esse filtro (`ResourcesPerPage`). Mesma
        # limitação real da API nos dois casos: um ARN sem nenhuma tag não
        # aparece na resposta (ver single_resource.py/resource_discovery.py).
        arns = ResourceARNList if ResourceARNList is not None else list(self.tags_por_arn)
        mappings = [
            {"ResourceARN": arn, "Tags": [{"Key": k, "Value": v} for k, v in self.tags_por_arn[arn].items()]}
            for arn in arns
            if self.tags_por_arn.get(arn)
        ]
        return {"ResourceTagMappingList": mappings}

    def tag_resources(self, ResourceARNList, Tags):
        self.tag_resources_calls.append((tuple(ResourceARNList), dict(Tags)))
        for arn in ResourceARNList:
            self.tags_por_arn.setdefault(arn, {}).update(Tags)
        return {"FailedResourcesMap": {}}


class _FakeSnsClient:
    def __init__(self):
        self.published: list[dict] = []

    def publish(self, TopicArn, Message, Subject):
        self.published.append({"TopicArn": TopicArn, "Message": Message, "Subject": Subject})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EXPECTED_TAG_VALUE", _EXPECTED)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:000000000000:prm-etapa3")


def _s3_event(cloudtrail_event_factory, bucket_name: str) -> dict:
    return cloudtrail_event_factory(
        event_source="s3.amazonaws.com",
        event_name="CreateBucket",
        detail_overrides={"requestParameters": {"bucketName": bucket_name}},
    )


def test_recurso_em_escopo_sem_iac_e_tagueado(cloudtrail_event_factory, fake_session_factory):
    arn = "arn:aws:s3:::bucket-novo"
    generic_client = _FakeGenericRwClient({arn: {}})
    sns_client = _FakeSnsClient()
    session = fake_session_factory({"resourcegroupstaggingapi": generic_client})

    handler(_s3_event(cloudtrail_event_factory, "bucket-novo"), session=session, sns_client=sns_client)

    assert generic_client.tag_resources_calls == [((arn,), {"aws-apn-id": _EXPECTED})]
    assert len(sns_client.published) == 1
    payload = json.loads(sns_client.published[0]["Message"])
    assert payload["categoria_final"] == "tagueado_sucesso"
    assert payload["arn"] == arn
    assert payload["etapa"] == "3"


def test_recurso_gerenciado_por_iac_e_pulado_e_sinalizado(cloudtrail_event_factory, fake_session_factory):
    arn = "arn:aws:s3:::bucket-iac"
    generic_client = _FakeGenericRwClient({arn: {"aws:cloudformation:stack-name": "minha-stack"}})
    sns_client = _FakeSnsClient()
    session = fake_session_factory({"resourcegroupstaggingapi": generic_client})

    handler(_s3_event(cloudtrail_event_factory, "bucket-iac"), session=session, sns_client=sns_client)

    assert generic_client.tag_resources_calls == []
    payload = json.loads(sns_client.published[0]["Message"])
    assert payload["categoria_final"] == "pulado_iac"
    assert payload["motivo"]


def test_recurso_ja_tagueado_corretamente_da_ja_ok_sem_erro(cloudtrail_event_factory, fake_session_factory):
    """Cobre o caso de outro processo ter tagueado o recurso corretamente
    entre o evento de criação e o processamento deste evento pela Etapa 3."""
    arn = "arn:aws:s3:::bucket-ja-ok"
    generic_client = _FakeGenericRwClient({arn: {"aws-apn-id": _EXPECTED}})
    sns_client = _FakeSnsClient()
    session = fake_session_factory({"resourcegroupstaggingapi": generic_client})

    handler(_s3_event(cloudtrail_event_factory, "bucket-ja-ok"), session=session, sns_client=sns_client)

    assert generic_client.tag_resources_calls == []
    payload = json.loads(sns_client.published[0]["Message"])
    assert payload["categoria_final"] == "ja_ok"
    assert payload["detalhe_erro"] is None


def test_evento_duplicado_e_idempotente(cloudtrail_event_factory, fake_session_factory):
    """EventBridge entrega at-least-once — processar o mesmo evento duas
    vezes nunca deve gerar uma segunda chamada de escrita."""
    arn = "arn:aws:s3:::bucket-duplicado"
    generic_client = _FakeGenericRwClient({arn: {}})
    sns_client = _FakeSnsClient()
    session = fake_session_factory({"resourcegroupstaggingapi": generic_client})
    event = _s3_event(cloudtrail_event_factory, "bucket-duplicado")

    handler(event, session=session, sns_client=sns_client)
    handler(event, session=session, sns_client=sns_client)

    assert len(generic_client.tag_resources_calls) == 1
    categorias = [json.loads(m["Message"])["categoria_final"] for m in sns_client.published]
    assert categorias == ["tagueado_sucesso", "ja_ok"]


def test_evento_fora_do_escopo_do_csv_e_ignorado(cloudtrail_event_factory, fake_session_factory):
    """Um serviço fora do CSV (ou sem mapeamento de evento definido) nunca
    deveria nem chegar à Lambda — o event pattern do EventBridge já filtra
    — mas o handler é defensivo: nenhuma chamada de API, nenhuma publicação."""
    sns_client = _FakeSnsClient()
    session = fake_session_factory({})
    event = cloudtrail_event_factory(event_source="quicksight.amazonaws.com", event_name="CreateDashboard")

    handler(event, session=session, sns_client=sns_client)

    assert sns_client.published == []


class _ClientQueBrasaComErroInesperado:
    """Simula um bug real (não um `ClientError` da AWS) durante a leitura
    inicial — ex.: os `AttributeError` que existiam em `event_parser.py`
    antes de corrigidos. `_process_one_resource` só captura `ClientError`
    explicitamente; qualquer outra exceção é o caso "inesperado" que o
    `except Exception` do laço em `handler()` precisa cobrir."""

    def get_resources(self, ResourceARNList=None, ResourcesPerPage=None, PaginationToken=None):
        raise RuntimeError("bug simulado, não é ClientError")


def test_falha_inesperada_ainda_publica_no_sns(cloudtrail_event_factory, fake_session_factory):
    """Bug real encontrado em code review: antes desta correção, uma falha
    inesperada (qualquer exceção que não seja `ClientError` na leitura
    inicial) só aparecia no CloudWatch Logs — nunca no tópico SNS. Isso
    contraria o princípio que `publish.py` declara ("publicar não é
    opcional"). A execução do Step Functions continua marcada como falha
    (`RuntimeError` propagado no final), mas agora o recurso também aparece
    no relatório de compliance."""
    arn = "arn:aws:s3:::bucket-vai-quebrar"
    sns_client = _FakeSnsClient()
    session = fake_session_factory({"resourcegroupstaggingapi": _ClientQueBrasaComErroInesperado()})

    with pytest.raises(RuntimeError, match="falharam de forma inesperada"):
        handler(_s3_event(cloudtrail_event_factory, "bucket-vai-quebrar"), session=session, sns_client=sns_client)

    assert len(sns_client.published) == 1
    payload = json.loads(sns_client.published[0]["Message"])
    assert payload["arn"] == arn
    assert payload["categoria_final"] == "falhou"
    assert payload["resultado"] == "erro_inesperado"
    assert payload["detalhe_erro"]["codigo"] == "RuntimeError"
