"""Recursos achados só pela listagem nativa têm as tags lidas na API do
serviço antes da decisão (`native_discovery.confirmar_tags`).

Sem isso, um recurso que o `GetResources` não devolveu (API falhou, índice
atrasado, LocalStack) entrava como `sem_tag` mesmo tendo tags: um bucket de
Terraform ou de outro parceiro parecia livre para taguear. Offline (moto).
"""
from __future__ import annotations

import pytest

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")

from botocore.exceptions import ClientError  # noqa: E402

from aws_prm_tagging import native_discovery, pipeline, resource_discovery  # noqa: E402
from aws_prm_tagging.services import load_services  # noqa: E402
from aws_prm_tagging.test.unit.test_native_discovery_moto import _criar_pares  # noqa: E402

R = "us-east-1"
VALOR = "pc:abc123"


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", R)
    monkeypatch.setenv("MOTO_EC2_LOAD_DEFAULT_AMIS", "false")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with moto.mock_aws():
        yield boto3.Session(region_name=R)


def _por_arn(recursos):
    return {r["arn"]: r for r in recursos}


def test_bucket_tagueado_que_o_getresources_nao_devolveu_nao_vira_sem_tag(aws, monkeypatch):
    """Cenário do LocalStack: GetResources não devolve buckets."""
    s3 = aws.client("s3")
    for b, tags in (("b-ok", {"aws-apn-id": VALOR}), ("b-tf", {"managed-by": "terraform"}),
                    ("b-outro", {"aws-apn-id": "pc:outro"}), ("b-sem", None)):
        s3.create_bucket(Bucket=b)
        if tags:
            s3.put_bucket_tagging(Bucket=b, Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]})
    monkeypatch.setattr(resource_discovery, "discover_generic_resources", lambda *a, **k: ([], []))

    recursos, falhas = pipeline.descobrir_regioes(
        aws, "123456789012", [R], load_services(), VALOR, historico_cloudtrail=False
    )
    por = {r["arn"].rsplit(":", 1)[-1]: r for r in recursos if r["arn"].startswith("arn:aws:s3")}
    assert por["b-ok"]["status_tag"] == "ok"
    assert por["b-tf"]["iac"]["ferramenta"] == "Terraform"
    assert por["b-outro"]["status_tag"] == "conflito" and por["b-outro"]["valor_tag_encontrado"] == "pc:outro"
    assert por["b-sem"]["status_tag"] == "sem_tag"
    assert all(r["tags_confirmadas"] for r in por.values())
    assert not [f for f in falhas if f["etapa"] == "nativo:confirmar_tags"]


def _criar_tagueados(s):
    """Um recurso com aws-apn-id=VALOR em cada serviço que o moto simula com
    leitura de tags."""
    c = s.client
    t = [{"Key": "aws-apn-id", "Value": VALOR}]
    iam = "arn:aws:iam::123456789012:role/r"
    c("ecr").create_repository(repositoryName="repo", tags=t)
    c("kinesis").create_stream(StreamName="k", ShardCount=1)
    c("kinesis").add_tags_to_stream(StreamName="k", Tags={"aws-apn-id": VALOR})
    c("glue").create_job(Name="j", Role="r", Command={"Name": "glueetl"}, Tags={"aws-apn-id": VALOR})
    zona = c("route53").create_hosted_zone(Name="a.com", CallerReference="x")["HostedZone"]["Id"].split("/")[-1]
    c("route53").change_tags_for_resource(ResourceType="hostedzone", ResourceId=zona, AddTags=t)
    c("acm").request_certificate(DomainName="a.com", Tags=t)
    c("backup").create_backup_vault(BackupVaultName="v", BackupVaultTags={"aws-apn-id": VALOR})
    c("elasticache").create_cache_cluster(CacheClusterId="cc", Engine="redis", CacheNodeType="cache.t3.micro",
                                          NumCacheNodes=1, Tags=t)
    c("emr").run_job_flow(Name="e", Instances={"MasterInstanceType": "m5.xlarge", "InstanceCount": 1,
                                               "KeepJobFlowAliveWhenNoSteps": True},
                          JobFlowRole="r", ServiceRole="r", Tags=t)
    c("s3").create_bucket(Bucket="dst")
    c("s3").put_bucket_tagging(Bucket="dst", Tagging={"TagSet": t})
    c("firehose").create_delivery_stream(DeliveryStreamName="f", Tags=t, S3DestinationConfiguration={
        "RoleARN": iam, "BucketARN": "arn:aws:s3:::dst"})
    c("mq").create_broker(BrokerName="b", EngineType="ACTIVEMQ", EngineVersion="5.17", HostInstanceType="mq.t3.micro",
                          DeploymentMode="SINGLE_INSTANCE", PubliclyAccessible=False, AutoMinorVersionUpgrade=True,
                          Users=[{"Username": "u", "Password": "p12345678901"}], Tags={"aws-apn-id": VALOR})
    c("sagemaker").create_notebook_instance(NotebookInstanceName="nb", InstanceType="ml.t2.medium", RoleArn=iam, Tags=t)
    c("opensearch").create_domain(DomainName="dom", TagList=t)
    c("memorydb").create_cluster(ClusterName="m", NodeType="db.t4g.small", ACLName="open-access", Tags=t)
    c("elasticbeanstalk").create_application(ApplicationName="a")
    c("elasticbeanstalk").create_environment(ApplicationName="a", EnvironmentName="env1",
                                             SolutionStackName="64bit Amazon Linux 2", Tags=t)
    c("sqs").create_queue(QueueName="q", tags={"aws-apn-id": VALOR})
    c("sns").create_topic(Name="tp", Tags=t)
    c("logs").create_log_group(logGroupName="lg", tags={"aws-apn-id": VALOR})
    c("dynamodb").create_table(TableName="tb", KeySchema=[{"AttributeName": "k", "KeyType": "HASH"}],
                               AttributeDefinitions=[{"AttributeName": "k", "AttributeType": "S"}],
                               BillingMode="PAY_PER_REQUEST", Tags=t)
    kms = c("kms").create_key()["KeyMetadata"]["KeyId"]
    c("kms").tag_resource(KeyId=kms, Tags=[{"TagKey": "aws-apn-id", "TagValue": VALOR}])


def test_leitores_leem_as_tags_reais_dos_servicos_simulados(aws):
    _criar_pares(aws)
    _criar_tagueados(aws)
    recursos, _ = native_discovery.discover_native_resources(aws, R, "123456789012", load_services(), VALOR)
    a_confirmar = [r for r in recursos if not r["tags_confirmadas"]]
    confirmados, falhas = native_discovery.confirmar_tags(aws, a_confirmar, VALOR)
    assert falhas == []
    assert len(confirmados) == len(a_confirmar)
    ok = {r["arn"].split(":")[2] for r in confirmados if r["status_tag"] == "ok"}
    esperados = {"ecr", "kinesis", "glue", "route53", "acm", "backup", "elasticache", "elasticmapreduce", "s3",
                 "firehose", "mq", "sagemaker", "es", "memorydb", "elasticbeanstalk", "sqs", "sns", "logs",
                 "dynamodb", "kms"}
    assert esperados <= ok, esperados - ok
    assert all(r["tags_confirmadas"] for r in confirmados)


@pytest.mark.parametrize("client,operacao,entrada,saida", [
    ("transfer", "ListTagsForResource", "Arn", "Tags"),
    ("network-firewall", "ListTagsForResource", "ResourceArn", "Tags"),
    ("workspaces", "DescribeTags", "ResourceId", "TagList"),
    ("elbv2", "DescribeTags", "ResourceArns", "TagDescriptions"),
    ("elb", "DescribeTags", "LoadBalancerNames", "TagDescriptions"),
    ("cloudfront", "ListTagsForResource", "Resource", "Tags"),
    ("codepipeline", "ListTagsForResource", "resourceArn", "tags"),
    ("dax", "ListTags", "ResourceName", "Tags"),
    ("codebuild", "BatchGetProjects", "names", "projects"),
    ("stepfunctions", "ListTagsForResource", "resourceArn", "tags"),
    ("ecs", "ListTagsForResource", "resourceArn", "tags"),
])
def test_leitores_sem_simulacao_batem_com_o_modelo_do_botocore(client, operacao, entrada, saida):
    """Serviços que o moto não simula: confere operação e campos no botocore."""
    import botocore.session

    op = botocore.session.get_session().get_service_model(client).operation_model(operacao)
    assert entrada in op.input_shape.members and saida in op.output_shape.members


def test_todo_enumerador_sem_tags_na_listagem_tem_leitor():
    for enum in native_discovery._ENUMERADORES:
        if enum.tags is None:
            amostra = {"s3_buckets": "s3", "opensearch_dominios": "es", "emr_clusters": "elasticmapreduce",
                       "step_functions": "states", "cloudwatch_log_groups": "logs", "elbv2": "elasticloadbalancing",
                       "elb_classico": "elasticloadbalancing", "elastic_beanstalk_ambientes": "elasticbeanstalk",
                       "dynamodb_tabelas": "dynamodb", "dax_clusters": "dax"}.get(enum.nome, enum.client)
            assert amostra in native_discovery._LEITORES_TAGS, enum.nome
    for ns in ("kms", "ecs"):
        assert ns in native_discovery._LEITORES_TAGS


class _ClientNegado:
    def __init__(self, codigo):
        self.codigo = codigo

    def get_bucket_tagging(self, **_):
        raise ClientError({"Error": {"Code": self.codigo, "Message": "x"}}, "GetBucketTagging")


class _Sessao:
    def __init__(self, codigo):
        self.codigo = codigo

    def client(self, *a, **k):
        return _ClientNegado(self.codigo)


def _bucket(nome):
    r = native_discovery._montar(f"arn:aws:s3:::{nome}", "Amazon S3", R, {}, VALOR)
    assert r["tags_confirmadas"] is False
    return r


def test_falha_de_leitura_tira_o_recurso_e_vira_falha_agregada():
    recursos = [_bucket(f"b{i}") for i in range(7)]
    saida, falhas = native_discovery.confirmar_tags(_Sessao("AccessDenied"), recursos, VALOR)
    assert saida == []
    assert len(falhas) == 1
    assert falhas[0]["etapa"] == "nativo:confirmar_tags"
    assert "7 recurso(s)" in falhas[0]["erro"] and "AccessDenied" in falhas[0]["erro"] and "..." in falhas[0]["erro"]


def test_recurso_apagado_antes_da_leitura_some_sem_falha():
    saida, falhas = native_discovery.confirmar_tags(_Sessao("NoSuchBucket"), [_bucket("b")], VALOR)
    assert saida == [] and falhas == []


def test_recurso_com_tags_da_listagem_nao_e_relido():
    r = native_discovery._montar("arn:aws:ec2:us-east-1:123456789012:volume/vol-1", "Amazon EC2", R,
                                 {"aws-apn-id": VALOR}, VALOR, confirmadas=True)
    saida, falhas = native_discovery.confirmar_tags(_Sessao("AccessDenied"), [r], VALOR)
    assert saida == [r] and falhas == []
