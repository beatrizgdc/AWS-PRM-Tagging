"""Menos e-mails: classificação `prm_tipo`, filtros das assinaturas e o
resumo diário da stack central. Offline (moto)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")

from aws_prm_tagging import handler_resumo_diario as resumo  # noqa: E402
from aws_prm_tagging import publish  # noqa: E402

INFRA = Path(__file__).resolve().parents[2] / "infra"
R = "us-east-1"


class _L(yaml.SafeLoader):
    pass


def _tag(loader, sufixo, node):
    if isinstance(node, yaml.SequenceNode):
        return {sufixo: loader.construct_sequence(node, deep=True)}
    if isinstance(node, yaml.MappingNode):
        return {sufixo: loader.construct_mapping(node, deep=True)}
    return {sufixo: loader.construct_scalar(node)}


_L.add_multi_constructor("!", _tag)


def _t(nome):
    return yaml.load((INFRA / nome).read_text(encoding="utf-8"), Loader=_L)


# --- classificação ----------------------------------------------------------

RESULTADO_OK = {"etapa": "3", "conta_id": "111111111111", "regiao": R, "categoria_final": "tagueado_sucesso",
                "arn": "arn:aws:s3:::b", "acao": "adicionada"}
FALHA = {"etapa": "3", "conta_id": "111111111111", "regiao": R, "categoria_final": "falhou",
         "arn": "arn:aws:s3:::x", "detalhe_erro": {"codigo": "AccessDenied", "mensagem": "negado"}}
SOBRESCRITA = {**RESULTADO_OK, "acao": "sobrescrita", "estado_anterior": {"valor_anterior_aws_apn_id": "pc:outro"}}
IAC = {**RESULTADO_OK, "categoria_final": "pulado_iac", "arn": "arn:aws:s3:::tf", "iac": {"ferramenta": "Terraform"}}
RESUMO_OK = {"etapa": "varredura_recorrente", "conta_id": "222222222222", "regiao": R, "modo": "dry-run",
             "por_categoria": {"simulado_sucesso": 3}, "total_falhas_descoberta": 0, "total_falhas_escrita": 0}


@pytest.mark.parametrize("payload,tipo", [
    (RESULTADO_OK, "resultado"),
    (IAC, "resultado"),
    (FALHA, "alerta"),
    ({**RESULTADO_OK, "categoria_final": "erro_classificacao"}, "alerta"),
    (SOBRESCRITA, "alerta"),
    ({"etapa": "4", "categoria_final": "drift_tag_removida"}, "resultado"),
    ({"etapa": "4", "categoria_final": "drift_tag_alterada", "possivel_disputa_com_outro_parceiro": True}, "alerta"),
    (RESUMO_OK, "resumo"),
    ({**RESUMO_OK, "total_falhas_escrita": 2}, "alerta"),
    ({**RESUMO_OK, "total_falhas_descoberta": 1, "regiao_bloqueada_por_scp": True}, "resumo"),
    ({**RESUMO_OK, "stack_regional_detectada": False}, "alerta"),
])
def test_tipo_mensagem(payload, tipo):
    assert publish.tipo_mensagem(payload) == tipo


def test_publish_outcome_envia_o_atributo_e_marca_o_assunto_do_alerta():
    enviados = []

    class Sns:
        def publish(self, **kw):
            enviados.append(kw)

    publish.publish_outcome(Sns(), "arn:aws:sns:us-east-1:1:t", FALHA)
    publish.publish_outcome(Sns(), "arn:aws:sns:us-east-1:1:t", RESULTADO_OK)
    assert enviados[0]["MessageAttributes"]["prm_tipo"]["StringValue"] == "alerta"
    assert enviados[0]["Subject"].startswith("PRM ALERTA")
    assert enviados[1]["MessageAttributes"]["prm_tipo"]["StringValue"] == "resultado"


# --- filtros nos templates ---------------------------------------------------


def test_email_do_hub_nao_recebe_resultados_individuais():
    sub = _t("template-hub.yaml")["Resources"]["PrmComplianceEmailSubscription"]["Properties"]
    assert set(sub["FilterPolicy"]["prm_tipo"]) == {"alerta", "resumo", "resumo_diario"}


def test_central_email_so_alertas_e_resumo_diario_e_fila_do_resumo_sem_laco():
    rec = _t("template-central.yaml")["Resources"]
    filtro = rec["AssinaturaEmail"]["Properties"]["FilterPolicy"]["prm_tipo"]["If"]
    assert filtro[1] == ["alerta", "resumo_diario"]
    fila = rec["AssinaturaFilaResumo"]["Properties"]
    assert "resumo_diario" not in fila["FilterPolicy"]["prm_tipo"]  # o resumo não volta para a fila
    assert fila["RawMessageDelivery"] is True
    assert rec["FilaResumo"]["Properties"]["KmsMasterKeyId"]
    agenda = rec["ResumoDiarioFunction"]["Properties"]["Events"]["Diario"]
    assert agenda["Type"] == "ScheduleV2"


def test_falha_de_regiao_da_state_machine_e_alerta():
    asl = json.loads((INFRA / "statemachine/tagueamento_inicial.asl.json").read_text(encoding="utf-8"))
    texto = json.dumps(asl)
    assert '"prm_tipo": {"DataType": "String", "StringValue": "alerta"}' in texto


# --- ponta a ponta com o SNS simulado ----------------------------------------


@pytest.fixture
def aws(monkeypatch):
    for k, v in dict(AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x", AWS_DEFAULT_REGION=R).items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with moto.mock_aws():
        sns, sqs = boto3.client("sns", R), boto3.client("sqs", R)
        topico = sns.create_topic(Name="prm-compliance-central")["TopicArn"]

        def fila(nome, filtro):
            url = sqs.create_queue(QueueName=nome)["QueueUrl"]
            arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
            sns.subscribe(TopicArn=topico, Protocol="sqs", Endpoint=arn, ReturnSubscriptionArn=True,
                          Attributes={"RawMessageDelivery": "true", "FilterPolicy": json.dumps({"prm_tipo": filtro})})
            return url

        # "email" = fila com o mesmo filtro da assinatura de e-mail da central
        email = fila("email", ["alerta", "resumo_diario"])
        acumulo = fila("resumo", ["alerta", "resumo", "resultado"])
        monkeypatch.setenv("FILA_RESUMO_URL", acumulo)
        monkeypatch.setenv("SNS_TOPIC_ARN", topico)
        yield sns, sqs, topico, email, acumulo


def _ler(sqs, url):
    msgs = []
    while True:
        r = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10)
        if not r.get("Messages"):
            return msgs
        for m in r["Messages"]:
            msgs.append(m["Body"])
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])


def test_um_dia_de_mensagens_vira_um_email_mais_os_alertas(aws):
    sns, sqs, topico, email, acumulo = aws
    for p in [RESULTADO_OK] * 40 + [IAC, RESUMO_OK, FALHA, SOBRESCRITA]:
        publish.publish_outcome(sns, topico, p)

    no_email = _ler(sqs, email)
    assert len(no_email) == 2  # só os 2 alertas chegam na hora

    out = resumo.handler({}, None, sqs=sqs, sns=sns)
    assert out["mensagens"] == 44

    no_email = _ler(sqs, email)
    assert len(no_email) == 1  # um único e-mail com o consolidado
    texto = no_email[0]
    assert "Contas com atividade: 2" in texto and "alertas: 2" in texto
    assert "tagueado_sucesso 40" in texto
    assert "Terraform | arn:aws:s3:::tf" in texto
    assert "sobrescreveu o valor de outro parceiro (pc:outro)" in texto
    assert "AccessDenied" in texto or "negado" in texto
    assert _ler(sqs, acumulo) == []  # fila esvaziada e o resumo não voltou para ela


def test_sem_atividade_ainda_manda_o_sinal_de_vida(aws):
    sns, sqs, topico, email, _ = aws
    resumo.handler({}, None, sqs=sqs, sns=sns)
    (texto,) = _ler(sqs, email)
    assert "Sem atividade no período" in texto


def test_se_a_publicacao_falhar_as_mensagens_ficam_para_o_dia_seguinte(aws, monkeypatch):
    sns, sqs, topico, _, acumulo = aws
    publish.publish_outcome(sns, topico, RESULTADO_OK)

    class SnsQuebrado:
        def publish(self, **kw):
            raise RuntimeError("fora do ar")

    monkeypatch.setattr(publish, "with_backoff", lambda *a, **k: (lambda f: f))
    with pytest.raises(RuntimeError):
        resumo.handler({}, None, sqs=sqs, sns=SnsQuebrado())
    attrs = sqs.get_queue_attributes(QueueUrl=acumulo, AttributeNames=["All"])["Attributes"]
    assert int(attrs["ApproximateNumberOfMessages"]) + int(attrs["ApproximateNumberOfMessagesNotVisible"]) == 1


def test_lista_longa_e_cortada():
    falhas = [{**FALHA, "arn": f"arn:aws:s3:::b{i}"} for i in range(100)]
    _, texto = resumo.montar(falhas, data="2026-11-10")
    assert "... e mais 70" in texto
