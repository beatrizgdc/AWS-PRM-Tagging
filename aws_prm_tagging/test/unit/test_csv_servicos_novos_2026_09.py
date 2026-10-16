"""Serviços que entraram no CSV em 2026-09-30 (reference/FONTE-CSV.md).

Formatos de ARN construídos confirmados pelos patterns de ARN dos modelos
botocore (scn, rtbfabric) ou pela documentação do modelo (ses). Nenhum
destes eventos foi capturado em sandbox ainda — ver roteiro em
docs/plano-finalizacao.md."""
from __future__ import annotations

import pytest

from aws_prm_tagging import event_parser
from aws_prm_tagging.services import classify_arn, load_services

R, C = "us-east-1", "000000000000"


def _extrai(factory, source, name, **overrides):
    return [r.arn for r in event_parser.parse_creation_event(factory(event_source=source, event_name=name, detail_overrides=overrides))]


def test_csv_tem_94_linhas_e_os_8_servicos_novos():
    services = load_services()
    assert len(services) == 94
    nomes = {s.name for s in services}
    for novo in (
        "Amazon Connect Decisions",
        "Amazon DynamoDB Accelerator (DAX)",
        "Amazon Elastic VMware Service (Amazon EVS)",
        "AWS End User Messaging",
        "Amazon GameLift Streams",
        "AWS RTB Fabric",
        "Amazon Simple Email Service (SES)",
        "Amazon WorkSpaces Core Managed Instances",
    ):
        assert novo in nomes


def test_evs_nao_rouba_o_rotulo_de_ec2():
    """EVS compartilha o código AmazonEC2 — um recurso EC2 continua sendo
    rotulado \"Amazon EC2\" (primeira linha com o código no CSV)."""
    svc = classify_arn(f"arn:aws:ec2:{R}:{C}:instance/i-0abc", load_services())
    assert svc.name == "Amazon EC2"


@pytest.mark.parametrize(
    "arn,codigo",
    [
        (f"arn:aws:scn:{R}:{C}:instance/11111111-2222-3333-4444-555555555555", "AWSSupplyChain"),
        (f"arn:aws:dax:{R}:{C}:cache/meu-cluster", "AmazonDAX"),
        (f"arn:aws:sms-voice:{R}:{C}:pool/pool-1", "AmazonPinpoint"),
        (f"arn:aws:mobiletargeting:{R}:{C}:apps/abc", "AmazonPinpoint"),
        (f"arn:aws:gameliftstreams:{R}:{C}:streamgroup/sg-1", "AmazonGameLiftStreams"),
        (f"arn:aws:rtbfabric:{R}:{C}:gateway/gw-1", "AWSRTBFabric"),
        (f"arn:aws:ses:{R}:{C}:identity/exemplo.com", "AmazonSES"),
    ],
)
def test_etapa1_classifica_namespaces_novos(arn, codigo):
    assert classify_arn(arn, load_services()).product_service_code == codigo


def test_supply_chain_constroi_arn_pelo_instance_id(cloudtrail_event_factory):
    iid = "11111111-2222-3333-4444-555555555555"
    arns = _extrai(cloudtrail_event_factory, "scn.amazonaws.com", "CreateInstance", responseElements={"instance": {"instanceId": iid}})
    assert arns == [f"arn:aws:scn:{R}:{C}:instance/{iid}"]


def test_dax_create_cluster(cloudtrail_event_factory):
    arn = f"arn:aws:dax:{R}:{C}:cache/c1"
    assert _extrai(cloudtrail_event_factory, "dax.amazonaws.com", "CreateCluster", responseElements={"cluster": {"clusterArn": arn}}) == [arn]


@pytest.mark.parametrize(
    "evento,campo",
    [("CreateConfigurationSet", "ConfigurationSetArn"), ("CreatePool", "PoolArn"), ("RequestPhoneNumber", "PhoneNumberArn"), ("RequestSenderId", "SenderIdArn")],
)
def test_end_user_messaging_sms_voice(cloudtrail_event_factory, evento, campo):
    arn = f"arn:aws:sms-voice:{R}:{C}:x/y"
    assert _extrai(cloudtrail_event_factory, "sms-voice.amazonaws.com", evento, responseElements={campo: arn}) == [arn]


@pytest.mark.parametrize("fonte", ["mobiletargeting.amazonaws.com", "pinpoint.amazonaws.com"])
@pytest.mark.parametrize("resposta", ["com_wrapper", "sem_wrapper"])
def test_pinpoint_create_app_nas_duas_fontes_e_formatos(cloudtrail_event_factory, fonte, resposta):
    arn = f"arn:aws:mobiletargeting:{R}:{C}:apps/abc"
    corpo = {"ApplicationResponse": {"Arn": arn}} if resposta == "com_wrapper" else {"Arn": arn}
    assert _extrai(cloudtrail_event_factory, fonte, "CreateApp", responseElements=corpo) == [arn]


@pytest.mark.parametrize("evento", ["CreateStreamGroup", "CreateApplication"])
def test_gamelift_streams(cloudtrail_event_factory, evento):
    arn = f"arn:aws:gameliftstreams:{R}:{C}:streamgroup/sg-1"
    assert _extrai(cloudtrail_event_factory, "gameliftstreams.amazonaws.com", evento, responseElements={"arn": arn}) == [arn]


@pytest.mark.parametrize("evento", ["CreateRequesterGateway", "CreateResponderGateway"])
def test_rtb_fabric_gateway(cloudtrail_event_factory, evento):
    assert _extrai(cloudtrail_event_factory, "rtbfabric.amazonaws.com", evento, responseElements={"gatewayId": "rtb-gw-1"}) == [
        f"arn:aws:rtbfabric:{R}:{C}:gateway/rtb-gw-1"
    ]


@pytest.mark.parametrize(
    "evento,req,sufixo",
    [
        ("CreateEmailIdentity", {"emailIdentity": "exemplo.com"}, "identity/exemplo.com"),
        ("VerifyEmailIdentity", {"emailAddress": "a@exemplo.com"}, "identity/a@exemplo.com"),
        ("VerifyDomainIdentity", {"domain": "exemplo.com"}, "identity/exemplo.com"),
        ("CreateConfigurationSet", {"configurationSetName": "cs-v2"}, "configuration-set/cs-v2"),
        ("CreateConfigurationSet", {"configurationSet": {"name": "cs-v1"}}, "configuration-set/cs-v1"),
    ],
    ids=["v2_identity", "v1_email", "v1_dominio", "v2_config_set", "v1_config_set"],
)
def test_ses_v1_e_v2(cloudtrail_event_factory, evento, req, sufixo):
    arns = _extrai(cloudtrail_event_factory, "ses.amazonaws.com", evento, requestParameters=req)
    assert arns == [f"arn:aws:ses:{R}:{C}:{sufixo}"]
    assert event_parser.parse_creation_event(
        cloudtrail_event_factory(event_source="ses.amazonaws.com", event_name=evento, detail_overrides={"requestParameters": req})
    )[0].servico == "Amazon Simple Email Service (SES)"


def test_workspaces_instances_mapeado_com_arn_da_referencia_oficial():
    from aws_prm_tagging import event_mapping

    assert "AmazonWorkSpacesInstances" in event_mapping.mapped_services()
