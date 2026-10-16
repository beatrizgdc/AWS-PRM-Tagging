"""Lote 3 — mapeamentos do item 10 (tabela validada em 2026-09-30) e
reação a tasks ECS (item 14). Offline. Os caminhos `_direct` já são
conferidos contra o modelo real em test_event_parser_botocore.py; aqui
fica a garantia de ponta a ponta (evento -> ARN -> serviço do CSV)."""
from __future__ import annotations

import pytest

from aws_prm_tagging import event_mapping, event_parser, handler_continuous_tagging

R, C = "us-east-1", "000000000000"


@pytest.mark.parametrize(
    "fonte,evento,resposta,request_,arn,servico",
    [
        ("sagemaker.amazonaws.com", "CreateEndpoint", {"endpointArn": f"arn:aws:sagemaker:{R}:{C}:endpoint/e"}, None, f"arn:aws:sagemaker:{R}:{C}:endpoint/e", "Amazon SageMaker AI"),
        ("glue.amazonaws.com", "CreateJob", {"name": "etl"}, None, f"arn:aws:glue:{R}:{C}:job/etl", "AWS Glue"),
        ("glue.amazonaws.com", "CreateCrawler", None, {"name": "cr"}, f"arn:aws:glue:{R}:{C}:crawler/cr", "AWS Glue"),
        ("bedrock-agentcore.amazonaws.com", "CreateMemory", {"memory": {"arn": f"arn:aws:bedrock-agentcore:{R}:{C}:memory/m"}}, None, f"arn:aws:bedrock-agentcore:{R}:{C}:memory/m", "Amazon Bedrock AgentCore"),
        ("appstream.amazonaws.com", "CreateFleet", {"fleet": {"arn": f"arn:aws:appstream:{R}:{C}:fleet/f"}}, None, f"arn:aws:appstream:{R}:{C}:fleet/f", "Amazon AppStream"),
        ("datasync.amazonaws.com", "CreateTask", {"taskArn": f"arn:aws:datasync:{R}:{C}:task/task-1"}, None, f"arn:aws:datasync:{R}:{C}:task/task-1", "AWS DataSync"),
        ("omics.amazonaws.com", "CreateWorkflow", {"arn": f"arn:aws:omics:{R}:{C}:workflow/1"}, None, f"arn:aws:omics:{R}:{C}:workflow/1", "Amazon Omics"),
        ("comprehend.amazonaws.com", "CreateEndpoint", {"endpointArn": f"arn:aws:comprehend:{R}:{C}:document-classifier-endpoint/e"}, None, f"arn:aws:comprehend:{R}:{C}:document-classifier-endpoint/e", "Amazon Comprehend"),
        ("securityhub.amazonaws.com", "EnableSecurityHub", None, None, f"arn:aws:securityhub:{R}:{C}:hub/default", "AWS Security Hub"),
        ("quicksight.amazonaws.com", "RegisterUser", {"user": {"arn": f"arn:aws:quicksight:{R}:{C}:user/default/ana"}}, None, f"arn:aws:quicksight:{R}:{C}:user/default/ana", "Amazon QuickSight"),
        ("quicksight.amazonaws.com", "CreateDashboard", {"arn": f"arn:aws:quicksight:{R}:{C}:dashboard/d"}, None, f"arn:aws:quicksight:{R}:{C}:dashboard/d", "Amazon QuickSight"),
        ("iot.amazonaws.com", "CreateBillingGroup", {"billingGroupArn": f"arn:aws:iot:{R}:{C}:billinggroup/bg"}, None, f"arn:aws:iot:{R}:{C}:billinggroup/bg", "AWS IoT Core"),
        ("iotsitewise.amazonaws.com", "CreateAsset", {"assetArn": f"arn:aws:iotsitewise:{R}:{C}:asset/a"}, None, f"arn:aws:iotsitewise:{R}:{C}:asset/a", "AWS IoT SiteWise"),
    ],
    ids=lambda v: v if isinstance(v, str) and not v.startswith(("arn", "{")) else None,
)
def test_item10_evento_vira_arn_do_servico(cloudtrail_event_factory, fonte, evento, resposta, request_, arn, servico):
    overrides = {}
    if resposta is not None:
        overrides["responseElements"] = resposta
    if request_ is not None:
        overrides["requestParameters"] = request_
    resultado = event_parser.parse_creation_event(
        cloudtrail_event_factory(event_source=fonte, event_name=evento, detail_overrides=overrides)
    )
    assert [r.arn for r in resultado] == [arn]
    assert resultado[0].servico == servico


def test_item10_sem_mapeamento_por_decisao():
    """DRS: sem API de criação de source server; CodeStar: descontinuado.
    (Deadline e WorkSpaces Instances foram mapeados em 2026-09-30 com o
    formato de ARN da Service Authorization Reference.)"""
    sem = set(event_mapping.unmapped_service_codes())
    assert sem == {"AWSElasticDisasterRecovery", "AWSCodeStar"}


def _evento_task(task_arn, launch="FARGATE"):
    return {
        "detail-type": "ECS Task State Change",
        "source": "aws.ecs",
        "account": C,
        "region": R,
        "detail": {"taskArn": task_arn, "lastStatus": "RUNNING", "desiredStatus": "RUNNING", "launchType": launch},
    }


def test_task_ecs_formato_longo_vira_recurso_task():
    arn = f"arn:aws:ecs:{R}:{C}:task/app/0123abcd"
    resultado = event_parser.parse_creation_event(_evento_task(arn))
    assert [(r.arn, r.tipo_recurso, r.servico) for r in resultado] == [(arn, "task", "Amazon ECS")]


def test_task_ecs_formato_curto_e_ignorada():
    assert event_parser.parse_creation_event(_evento_task(f"arn:aws:ecs:{R}:{C}:task/0123abcd")) == []


def test_publicar_ja_ok_false_suprime_so_o_ja_ok(monkeypatch):
    """PublicarResultadoJaOk=false: `ja_ok` não é publicado; qualquer outra
    categoria continua sendo."""
    publicados = []
    monkeypatch.setattr(handler_continuous_tagging.publish, "publish_outcome", lambda c, t, p: publicados.append(p))
    for categoria, esperado in (("ja_ok", 0), ("tagueado_sucesso", 1)):
        publicados.clear()
        monkeypatch.setenv("PUBLICAR_JA_OK", "false")
        monkeypatch.setattr(
            handler_continuous_tagging.tag_execution,
            "run_tagging_execution",
            lambda *a, _c=categoria, **k: {"recursos": [{
                "arn": "arn:x", "servico": "Amazon ECS", "regiao": R, "tipo_recurso": "task",
                "categoria_final": _c, "origem": "o", "estrategia_api": "e", "resultado": "r",
                "detalhe_erro": None, "motivo": None,
            }]},
        )
        monkeypatch.setattr(
            handler_continuous_tagging.single_resource, "build_resource_from_arn",
            lambda *a, **k: {"arn": "arn:x", "servico": "Amazon ECS", "regiao": R, "tipo_recurso": "task",
                             "status_tag": "ok", "valor_tag_encontrado": "pc:x", "tag_similar_encontrada": False,
                             "tag_similar_chaves": [], "iac": {"detectado": False, "ferramenta": None}},
        )
        monkeypatch.setattr(handler_continuous_tagging.resource_expansion, "expand_instance_volumes", lambda s, e: ([], []))
        monkeypatch.setenv("EXPECTED_TAG_VALUE", "pc:x")
        monkeypatch.setenv("SNS_TOPIC_ARN", f"arn:aws:sns:{R}:{C}:t")
        handler_continuous_tagging.handler(
            _evento_task(f"arn:aws:ecs:{R}:{C}:task/app/1"), session=object(), sns_client=object()
        )
        assert len(publicados) == esperado, categoria


def test_deadline_farm_queue_fleet(cloudtrail_event_factory):
    def arns(nome, req, resp):
        return [r.arn for r in event_parser.parse_creation_event(
            cloudtrail_event_factory(event_source="deadline.amazonaws.com", event_name=nome,
                                     detail_overrides={"requestParameters": req, "responseElements": resp}))]
    f = "farm-" + "0" * 32
    assert arns("CreateFarm", {}, {"farmId": f}) == [f"arn:aws:deadline:{R}:{C}:farm/{f}"]
    assert arns("CreateQueue", {"farmId": f}, {"queueId": "queue-1"}) == [f"arn:aws:deadline:{R}:{C}:farm/{f}/queue/queue-1"]
    assert arns("CreateFleet", {"farmId": f}, {"fleetId": "fleet-1"}) == [f"arn:aws:deadline:{R}:{C}:farm/{f}/fleet/fleet-1"]
    assert arns("CreateQueue", {}, {"queueId": "queue-1"}) == []  # sem farmId não monta ARN parcial


def test_workspaces_core_managed_instance(cloudtrail_event_factory):
    r = event_parser.parse_creation_event(cloudtrail_event_factory(
        event_source="workspaces-instances.amazonaws.com", event_name="CreateWorkspaceInstance",
        detail_overrides={"responseElements": {"workspaceInstanceId": "wsinst-abc12345"}}))
    assert [x.arn for x in r] == [f"arn:aws:workspaces-instances:{R}:{C}:workspaceinstance/wsinst-abc12345"]
    assert r[0].servico == "Amazon WorkSpaces Core Managed Instances"
