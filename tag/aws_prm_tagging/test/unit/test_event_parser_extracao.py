"""Testes de `event_parser.py` (Etapa 3) — extração pura, sem AWS/boto3.

Cobre: extractors dedicados de alta confiança (um por serviço do "lote
inicial"), o fallback genérico best-effort para eventos mapeados sem
extractor dedicado, e os casos defensivos (payload insuficiente, evento
fora do mapeamento)."""
from __future__ import annotations

from aws_prm_tagging.event_parser import parse_creation_event


def test_ec2_run_instances_extrai_multiplas_instancias(cloudtrail_event_factory):
    """RunInstances pode criar mais de uma instância numa única chamada —
    `responseElements.instancesSet.items` é uma lista; o evento inteiro deve
    virar uma lista de recursos, não só o primeiro."""
    event = cloudtrail_event_factory(
        event_source="ec2.amazonaws.com",
        event_name="RunInstances",
        detail_overrides={
            "responseElements": {"instancesSet": {"items": [{"instanceId": "i-aaa"}, {"instanceId": "i-bbb"}]}}
        },
    )
    resultado = parse_creation_event(event)
    assert [r.arn for r in resultado] == [
        "arn:aws:ec2:us-east-1:000000000000:instance/i-aaa",
        "arn:aws:ec2:us-east-1:000000000000:instance/i-bbb",
    ]
    assert all(r.servico == "Amazon EC2" for r in resultado)
    assert all(r.tipo_recurso is None for r in resultado)


def test_s3_create_bucket_extrai_arn_sem_regiao_ou_conta(cloudtrail_event_factory):
    event = cloudtrail_event_factory(
        event_source="s3.amazonaws.com",
        event_name="CreateBucket",
        detail_overrides={"requestParameters": {"bucketName": "meu-bucket-x"}},
    )
    resultado = parse_creation_event(event)
    assert len(resultado) == 1
    assert resultado[0].arn == "arn:aws:s3:::meu-bucket-x"
    assert resultado[0].servico == "Amazon S3"


def test_lambda_create_function_extrai_arn_direto(cloudtrail_event_factory):
    event = cloudtrail_event_factory(
        event_source="lambda.amazonaws.com",
        event_name="CreateFunction20150331v2",
        detail_overrides={
            "responseElements": {"functionArn": "arn:aws:lambda:us-east-1:000000000000:function:minha-funcao"}
        },
    )
    resultado = parse_creation_event(event)
    assert len(resultado) == 1
    assert resultado[0].arn == "arn:aws:lambda:us-east-1:000000000000:function:minha-funcao"
    assert resultado[0].servico == "AWS Lambda"


def test_eks_create_cluster_marca_tipo_recurso_cluster(cloudtrail_event_factory):
    event = cloudtrail_event_factory(
        event_source="eks.amazonaws.com",
        event_name="CreateCluster",
        detail_overrides={
            "responseElements": {"cluster": {"arn": "arn:aws:eks:us-east-1:000000000000:cluster/meu-cluster"}}
        },
    )
    resultado = parse_creation_event(event)
    assert len(resultado) == 1
    assert resultado[0].servico == "Amazon EKS"
    assert resultado[0].tipo_recurso == "cluster"


def test_bedrock_create_inference_profile_marca_tipo_recurso(cloudtrail_event_factory):
    event = cloudtrail_event_factory(
        event_source="bedrock.amazonaws.com",
        event_name="CreateInferenceProfile",
        detail_overrides={
            "responseElements": {
                "inferenceProfileArn": "arn:aws:bedrock:us-east-1:000000000000:application-inference-profile/abc"
            }
        },
    )
    resultado = parse_creation_event(event)
    assert len(resultado) == 1
    assert resultado[0].tipo_recurso == "application_inference_profile"


def test_sqs_create_queue_constroi_arn_a_partir_do_nome(cloudtrail_event_factory):
    """CreateQueue não devolve ARN na resposta — só QueueUrl — o ARN precisa
    ser construído a partir do nome pedido na requisição."""
    event = cloudtrail_event_factory(
        event_source="sqs.amazonaws.com",
        event_name="CreateQueue",
        detail_overrides={"requestParameters": {"queueName": "minha-fila"}},
    )
    resultado = parse_creation_event(event)
    assert resultado[0].arn == "arn:aws:sqs:us-east-1:000000000000:minha-fila"


def test_fallback_generico_encontra_arn_por_sufixo_de_chave(cloudtrail_event_factory):
    """AppSync está mapeado em event_mapping.py mas não tem extractor
    dedicado — deve cair no fallback genérico, que procura uma chave
    terminando em "arn" dentro de responseElements."""
    event = cloudtrail_event_factory(
        event_source="appsync.amazonaws.com",
        event_name="CreateGraphqlApi",
        detail_overrides={
            "responseElements": {"graphqlApi": {"arn": "arn:aws:appsync:us-east-1:000000000000:apis/abc123"}}
        },
    )
    resultado = parse_creation_event(event)
    assert len(resultado) == 1
    assert resultado[0].arn == "arn:aws:appsync:us-east-1:000000000000:apis/abc123"
    assert resultado[0].servico == "AWS AppSync"


def test_fallback_generico_sem_nenhuma_chave_arn_devolve_lista_vazia(cloudtrail_event_factory):
    """Fallback nunca inventa um ARN — se não encontrar nada plausível,
    devolve lista vazia (o chamador trata isso como falha de extração)."""
    event = cloudtrail_event_factory(
        event_source="appsync.amazonaws.com",
        event_name="CreateGraphqlApi",
        detail_overrides={"responseElements": {"graphqlApi": {"name": "minha-api"}}},
    )
    assert parse_creation_event(event) == []


def test_evento_fora_do_mapeamento_devolve_lista_vazia(cloudtrail_event_factory):
    """Evento que não está em nenhuma entrada de event_mapping.py — não
    deveria nem chegar aqui (o pattern do EventBridge já filtraria), mas o
    parser é defensivo mesmo assim."""
    event = cloudtrail_event_factory(event_source="quicksight.amazonaws.com", event_name="CreateDashboard")
    assert parse_creation_event(event) == []


def test_evento_sem_conta_ou_regiao_devolve_lista_vazia(cloudtrail_event_factory):
    event = cloudtrail_event_factory(event_source="ec2.amazonaws.com", event_name="RunInstances")
    del event["detail"]["recipientAccountId"]
    del event["account"]
    assert parse_creation_event(event) == []


def test_evento_sem_detail_devolve_lista_vazia():
    assert parse_creation_event({}) == []


def test_rds_create_db_cluster_usa_fallback_de_identificador(cloudtrail_event_factory):
    """Quando o responseElements não traz o ARN direto (ex.: payload
    incompleto), o extractor de RDS cai para construir a partir do
    identificador pedido na requisição."""
    event = cloudtrail_event_factory(
        event_source="rds.amazonaws.com",
        event_name="CreateDBCluster",
        detail_overrides={"requestParameters": {"dBClusterIdentifier": "meu-cluster-aurora"}},
    )
    resultado = parse_creation_event(event)
    assert resultado[0].arn == "arn:aws:rds:us-east-1:000000000000:cluster:meu-cluster-aurora"
    assert resultado[0].servico == "Amazon Relational Database Service (RDS)"
