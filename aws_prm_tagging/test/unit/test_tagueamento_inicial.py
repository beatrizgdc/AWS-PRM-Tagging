"""Item 8 — tagueamento inicial assíncrono (stack hub). Offline."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from aws_prm_tagging import handler_initial_tagging as h

INFRA = Path(__file__).parent.parent.parent / "infra"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_multi_constructor("!", lambda loader, tag, node: None)


def _template(nome):
    return yaml.load((INFRA / nome).read_text(encoding="utf-8"), Loader=_Loader)


# ---------------------------------------------------------------------------
# Custom Resource
# ---------------------------------------------------------------------------


class _Opener:
    def __init__(self):
        self.respostas = []

    def __call__(self, req, timeout=None):
        self.respostas.append(json.loads(req.data))


class _Sfn:
    def __init__(self, falha=None):
        self.chamadas, self.falha = [], falha

    def start_execution(self, **kw):
        if self.falha:
            raise self.falha
        self.chamadas.append(kw)
        return {"executionArn": "arn:aws:states:us-east-1:1:execution:sm:x"}


def _evento(tipo, modo="dry-run", reexecutar="1", antigo=None):
    e = {
        "RequestType": tipo, "ResponseURL": "https://cfn/resposta", "StackId": "s", "RequestId": "r",
        "LogicalResourceId": "TagueamentoInicial", "ResourceProperties": {"Modo": modo, "Reexecutar": reexecutar},
    }
    if antigo is not None:
        e["OldResourceProperties"] = antigo
    return e


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:aws:states:us-east-1:1:stateMachine:sm")


def test_create_inicia_execucao_e_responde_sucesso():
    sfn, op = _Sfn(), _Opener()
    h.custom_resource(_evento("Create"), sfn_client=sfn, opener=op)
    assert json.loads(sfn.chamadas[0]["input"]) == {"modo": "dry-run"}
    assert op.respostas[0]["Status"] == "SUCCESS" and op.respostas[0]["Data"]["Iniciado"] == "true"


def test_create_desligado_nao_inicia():
    sfn, op = _Sfn(), _Opener()
    h.custom_resource(_evento("Create", modo="desligado"), sfn_client=sfn, opener=op)
    assert sfn.chamadas == [] and op.respostas[0]["Status"] == "SUCCESS"


def test_update_sem_mudar_reexecutar_nao_inicia_e_mudando_inicia():
    sfn, op = _Sfn(), _Opener()
    h.custom_resource(_evento("Update", modo="live", antigo={"Modo": "dry-run", "Reexecutar": "1"}), sfn_client=sfn, opener=op)
    assert sfn.chamadas == [], "trocar só o modo não pode disparar escrita — exige mudar Reexecutar"
    h.custom_resource(_evento("Update", modo="live", reexecutar="2", antigo={"Modo": "live", "Reexecutar": "1"}), sfn_client=sfn, opener=op)
    assert json.loads(sfn.chamadas[0]["input"]) == {"modo": "live"}


def test_delete_nunca_inicia_nem_falha():
    sfn, op = _Sfn(falha=RuntimeError("não deveria ser chamado")), _Opener()
    h.custom_resource(_evento("Delete"), sfn_client=sfn, opener=op)
    assert op.respostas[0]["Status"] == "SUCCESS"


def test_create_com_erro_ao_iniciar_responde_failed():
    op = _Opener()
    h.custom_resource(_evento("Create"), sfn_client=_Sfn(falha=RuntimeError("AccessDenied")), opener=op)
    assert op.respostas[0]["Status"] == "FAILED" and "AccessDenied" in op.respostas[0]["Reason"]


def test_modo_invalido_responde_failed():
    op = _Opener()
    h.custom_resource(_evento("Create", modo="tudo"), sfn_client=_Sfn(), opener=op)
    assert op.respostas[0]["Status"] == "FAILED"


# ---------------------------------------------------------------------------
# State machine e template
# ---------------------------------------------------------------------------


def _asl():
    texto = (INFRA / "statemachine" / "tagueamento_inicial.asl.json").read_text(encoding="utf-8")
    for k in ("ListarRegioesArn", "ProcessarRegiaoArn", "TopicArn"):
        texto = texto.replace("${" + k + "}", f"arn:aws:x:{k}")
    return json.loads(texto)


def test_asl_map_por_regiao_com_falha_isolada_publicada():
    d = _asl()
    mapa = d["States"]["PorRegiao"]
    assert mapa["Type"] == "Map" and mapa["ItemsPath"] == "$.regioes" and mapa["MaxConcurrency"] >= 1
    interno = mapa["ItemProcessor"]["States"]
    captura = interno["ProcessarRegiao"]["Catch"][0]
    assert captura["ErrorEquals"] == ["States.ALL"]
    assert interno[captura["Next"]]["Resource"] == "arn:aws:states:::sns:publish"
    assert interno[captura["Next"]].get("End") is True  # falha de uma região não interrompe as outras


def test_substituicoes_do_asl_existem_no_template():
    sm = _template("template-hub.yaml")["Resources"]["TagueamentoInicialStateMachine"]["Properties"]
    texto = (INFRA / "statemachine" / "tagueamento_inicial.asl.json").read_text(encoding="utf-8")
    import re

    assert set(re.findall(r"\$\{(\w+)\}", texto)) == set(sm["DefinitionSubstitutions"])


def _acoes(template, role, politica):
    pols = {p["PolicyName"]: p for p in template["Resources"][role]["Properties"]["Policies"]}
    acao = pols[politica]["PolicyDocument"]["Statement"][0]["Action"]
    return sorted(acao if isinstance(acao, list) else [acao])


def test_hub_escreve_com_as_mesmas_acoes_da_etapa3():
    """Guarda de deriva: um serviço novo mapeado na Etapa 3 precisa também
    poder ser tagueado pelo tagueamento inicial."""
    hub, reg = _template("template-hub.yaml"), _template("template-regional.yaml")
    assert _acoes(hub, "TagueamentoInicialRole", "PrmTagueamentoInicialEscritaNativa") == _acoes(
        reg, "Etapa3ExecutionRole", "PrmEtapa3NativeTagWrite"
    )


def test_hub_le_tudo_que_a_descoberta_exige():
    from aws_prm_tagging import native_discovery

    hub = _template("template-hub.yaml")
    leitura = set(_acoes(hub, "TagueamentoInicialRole", "PrmTagueamentoInicialLeitura"))
    faltando = sorted(set(native_discovery.required_iam_actions()) - leitura)
    assert faltando == [], f"role do tagueamento inicial sem: {faltando}"


def test_live_nunca_e_o_default():
    params = _template("template-hub.yaml")["Parameters"]
    assert params["ModoTagueamentoInicial"]["Default"] == "dry-run"


# ---------------------------------------------------------------------------
# processar_regiao ponta a ponta contra AWS simulada
# ---------------------------------------------------------------------------

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")


@pytest.fixture
def aws(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"), ("AWS_DEFAULT_REGION", "us-east-1"),
                 ("MOTO_EC2_LOAD_DEFAULT_AMIS", "false"), ("EXPECTED_TAG_VALUE", "pc:abc123"),
                 ("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:prm")):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with moto.mock_aws():
        s = boto3.Session(region_name="us-east-1")
        s.client("s3").create_bucket(Bucket="nunca-teve-tag")
        yield s


class _Sns:
    def __init__(self):
        self.msgs = []

    def publish(self, **kw):
        self.msgs.append(kw)
        return {"MessageId": "1"}


@pytest.mark.parametrize("modo,deve_ter_tag", [("dry-run", False), ("live", True)])
def test_processar_regiao_dry_run_nao_escreve_e_live_escreve(aws, modo, deve_ter_tag):
    sns = _Sns()
    saida = h.processar_regiao({"regiao": "us-east-1", "modo": modo}, session=aws, sns_client=sns)
    resumo = json.loads(sns.msgs[0]["Message"])
    assert resumo["etapa"] == "tagueamento_inicial" and resumo["modo"] == modo
    assert "us-east-1" in sns.msgs[0]["Subject"] and sns.msgs[0]["Subject"].isascii()
    assert saida["por_categoria"] and resumo["total_descobertos_so_via_api_nativa"] >= 1
    try:
        tags = aws.client("s3").get_bucket_tagging(Bucket="nunca-teve-tag")["TagSet"]
    except aws.client("s3").exceptions.ClientError:
        tags = []
    assert ({"Key": "aws-apn-id", "Value": "pc:abc123"} in tags) is deve_ter_tag


# ---------------------------------------------------------------------------
# Etapa 4 — varredura recorrente (mesma state machine, origem diferente)
# ---------------------------------------------------------------------------


def test_listar_regioes_propaga_origem_com_default_inicial(monkeypatch):
    monkeypatch.setattr(h.regions, "get_active_regions", lambda s: ["us-east-1"])
    assert h.listar_regioes({"modo": "dry-run"}, session=object())["origem"] == "tagueamento_inicial"
    assert h.listar_regioes({"modo": "live", "origem": "varredura_recorrente"}, session=object())["origem"] == "varredura_recorrente"


def test_asl_repassa_origem_para_cada_regiao():
    assert _asl()["States"]["PorRegiao"]["ItemSelector"]["origem.$"] == "$.origem"


@pytest.fixture
def aws_limpa(monkeypatch):
    """Conta simulada em estado estável: um bucket já com a tag certa."""
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"), ("AWS_DEFAULT_REGION", "us-east-1"),
                 ("MOTO_EC2_LOAD_DEFAULT_AMIS", "false"), ("EXPECTED_TAG_VALUE", "pc:abc123"),
                 ("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:prm"), ("CONTRATO_CONFIRMADO", "true")):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("PUBLICAR_SEM_NOVIDADE", raising=False)
    # O pipeline é fixado num resumo "estado estável" — o que está em teste
    # aqui é a regra de publicação e a leitura de saúde regional (SQS real
    # do moto), não a descoberta (coberta em outros testes). Sem isso, as
    # lacunas do simulador (Bedrock, ElastiCache Serverless não
    # implementados) apareceriam como falha de descoberta.
    monkeypatch.setattr(
        h.pipeline, "executar_regiao",
        lambda *a, **k: {"conta_id": "123456789012", "regiao": "us-east-1", "modo": "dry-run", "total_recursos": 3,
                         "por_categoria": {"ja_ok": 2, "pulado_iac": 1}, "total_descobertos_so_via_api_nativa": 0,
                         "total_falhas_descoberta": 0, "falhas_descoberta_amostra": [], "falhas_escrita_amostra": [],
                         "total_falhas_escrita": 0},
    )
    with moto.mock_aws():
        yield boto3.Session(region_name="us-east-1")


def _varredura(sessao, sns):
    return h.processar_regiao({"regiao": "us-east-1", "modo": "dry-run", "origem": "varredura_recorrente"}, session=sessao, sns_client=sns)


def test_varredura_sem_novidade_nao_publica(aws_limpa):
    aws_limpa.client("sqs").create_queue(QueueName="prm-etapa3-dlq")
    sns = _Sns()
    saida = _varredura(aws_limpa, sns)
    assert saida["publicado"] is False and sns.msgs == []


def test_varredura_publica_quando_ha_mensagem_na_dlq(aws_limpa):
    url = aws_limpa.client("sqs").create_queue(QueueName="prm-etapa3-dlq")["QueueUrl"]
    aws_limpa.client("sqs").send_message(QueueUrl=url, MessageBody="{}")
    sns = _Sns()
    _varredura(aws_limpa, sns)
    resumo = json.loads(sns.msgs[0]["Message"])
    assert resumo["dlq_mensagens"] == 1 and resumo["etapa"] == "varredura_recorrente"


def test_varredura_publica_quando_regiao_nao_tem_stack_regional(aws_limpa):
    sns = _Sns()
    _varredura(aws_limpa, sns)
    assert json.loads(sns.msgs[0]["Message"])["stack_regional_detectada"] is False


def test_varredura_publica_quando_contrato_nao_confirmado(aws_limpa, monkeypatch):
    aws_limpa.client("sqs").create_queue(QueueName="prm-etapa3-dlq")
    monkeypatch.setenv("CONTRATO_CONFIRMADO", "false")
    sns = _Sns()
    _varredura(aws_limpa, sns)
    assert json.loads(sns.msgs[0]["Message"])["contrato_confirmado"] is False


def test_varredura_com_heartbeat_publica_sempre(aws_limpa, monkeypatch):
    aws_limpa.client("sqs").create_queue(QueueName="prm-etapa3-dlq")
    monkeypatch.setenv("PUBLICAR_SEM_NOVIDADE", "true")
    sns = _Sns()
    _varredura(aws_limpa, sns)
    assert len(sns.msgs) == 1


def test_tagueamento_inicial_publica_sempre_mesmo_sem_novidade(aws_limpa):
    aws_limpa.client("sqs").create_queue(QueueName="prm-etapa3-dlq")
    sns = _Sns()
    h.processar_regiao({"regiao": "us-east-1", "modo": "dry-run"}, session=aws_limpa, sns_client=sns)
    assert len(sns.msgs) == 1


def test_schedule_so_existe_se_varredura_nao_estiver_desligada():
    r = _template("template-hub.yaml")["Resources"]
    assert r["VarreduraRecorrenteSchedule"]["Condition"] == "VarreduraAgendada"
    assert _template("template-hub.yaml")["Parameters"]["ModoVarreduraRecorrente"]["Default"] == "dry-run"


@pytest.mark.parametrize(
    "ajuste,esperado",
    [
        ({}, False),
        ({"por_categoria": {"ja_ok": 5, "simulado_sucesso": 1}}, True),
        ({"por_categoria": {"conflito": 1}}, True),
        ({"total_falhas_descoberta": 1}, True),
        ({"dlq_mensagens": 3}, True),
        ({"stack_regional_detectada": False}, True),
        ({"stack_regional_detectada": None}, False),  # leitura falhou: não é novidade por si só
        ({"contrato_confirmado": False}, True),
    ],
)
def test_regra_de_novidade(ajuste, esperado):
    base = {"por_categoria": {"ja_ok": 2, "pulado_iac": 1}, "total_falhas_descoberta": 0, "dlq_mensagens": 0,
            "stack_regional_detectada": True, "contrato_confirmado": True}
    assert h._tem_novidade({**base, **ajuste}) is esperado


def test_regioes_varredura_restringe_a_lista(monkeypatch):
    monkeypatch.setattr(h.regions, "get_active_regions", lambda s: ["us-east-1", "us-east-2", "sa-east-1"])
    monkeypatch.setenv("REGIOES_PERMITIDAS", "us-east-2")
    assert h.listar_regioes({"modo": "dry-run"}, session=object())["regioes"] == ["us-east-2"]
    monkeypatch.setenv("REGIOES_PERMITIDAS", "")
    assert len(h.listar_regioes({"modo": "dry-run"}, session=object())["regioes"]) == 3
