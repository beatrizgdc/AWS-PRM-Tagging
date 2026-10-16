"""Testes de `single_resource.py` (Etapa 3) — leitura do estado atual de UM
recurso a partir do ARN, nos 4 caminhos de API (genérico/EKS/Bedrock/ELB),
usando `fake_session_factory` (sem rede real, ver conftest.py)."""
from __future__ import annotations

from aws_prm_tagging.single_resource import build_resource_from_arn

_EXPECTED = "pc:5ugbbrmu7ud3u5hsipfzug61p"


class _FakeGenericClient:
    def __init__(self, tags_por_arn: dict[str, dict[str, str]]):
        self._tags_por_arn = tags_por_arn

    def get_resources(self, ResourceARNList):
        mappings = [
            {
                "ResourceARN": arn,
                "Tags": [{"Key": k, "Value": v} for k, v in self._tags_por_arn[arn].items()],
            }
            for arn in ResourceARNList
            if arn in self._tags_por_arn
        ]
        return {"ResourceTagMappingList": mappings}


class _FakeEksClient:
    def __init__(self, tags: dict[str, str]):
        self._tags = tags

    def list_tags_for_resource(self, resourceArn):
        return {"tags": self._tags}


class _FakeBedrockClient:
    def __init__(self, tags: dict[str, str]):
        self._tags = tags

    def list_tags_for_resource(self, resourceARN):
        return {"tags": [{"key": k, "value": v} for k, v in self._tags.items()]}


class _FakeElbClient:
    def __init__(self, tags: dict[str, str]):
        self._tags = tags

    def describe_tags(self, ResourceArns):
        return {
            "TagDescriptions": [
                {"ResourceArn": arn, "Tags": [{"Key": k, "Value": v} for k, v in self._tags.items()]}
                for arn in ResourceArns
            ]
        }


def test_caminho_generico_com_tag_esperada_ja_presente(fake_session_factory):
    arn = "arn:aws:ec2:us-east-1:000000000000:instance/i-abc123"
    session = fake_session_factory(
        {"resourcegroupstaggingapi": _FakeGenericClient({arn: {"aws-apn-id": _EXPECTED}})}
    )
    resource = build_resource_from_arn(session, arn, "Amazon EC2", "us-east-1", _EXPECTED)
    assert resource["status_tag"] == "ok"
    assert resource["valor_tag_encontrado"] == _EXPECTED
    assert resource["iac"]["tipo"] == "desconhecido"


def test_caminho_generico_recurso_ausente_da_resposta_vira_sem_tag(fake_session_factory):
    """Um ARN recém-criado, sem nenhuma tag ainda, não aparece na resposta
    de `get_resources` (mesma limitação documentada para a Etapa 1) — o
    resultado correto ainda assim é `sem_tag`, não um erro."""
    arn = "arn:aws:ec2:us-east-1:000000000000:instance/i-recente"
    session = fake_session_factory({"resourcegroupstaggingapi": _FakeGenericClient({})})
    resource = build_resource_from_arn(session, arn, "Amazon EC2", "us-east-1", _EXPECTED)
    assert resource["status_tag"] == "sem_tag"
    assert resource["valor_tag_encontrado"] is None


def test_caminho_generico_detecta_iac_por_tag_de_stack(fake_session_factory):
    arn = "arn:aws:lambda:us-east-1:000000000000:function:minha-funcao"
    session = fake_session_factory(
        {
            "resourcegroupstaggingapi": _FakeGenericClient(
                {arn: {"aws:cloudformation:stack-name": "minha-stack"}}
            )
        }
    )
    resource = build_resource_from_arn(session, arn, "AWS Lambda", "us-east-1", _EXPECTED)
    assert resource["status_tag"] == "sem_tag"
    assert resource["iac"]["tipo"] == "cloudformation"


def test_caminho_generico_detecta_tag_similar(fake_session_factory):
    arn = "arn:aws:s3:::meu-bucket"
    session = fake_session_factory(
        {"resourcegroupstaggingapi": _FakeGenericClient({arn: {"AWS-APN-ID": _EXPECTED}})}
    )
    resource = build_resource_from_arn(session, arn, "Amazon S3", "us-east-1", _EXPECTED)
    assert resource["status_tag"] == "sem_tag"
    assert resource["tag_similar_encontrada"] is True
    assert resource["tag_similar_chaves"] == ["AWS-APN-ID"]


def test_caminho_eks_cluster_usa_list_tags_for_resource(fake_session_factory):
    arn = "arn:aws:eks:us-east-1:000000000000:cluster/meu-cluster"
    session = fake_session_factory({"eks": _FakeEksClient({"aws-apn-id": _EXPECTED})})
    resource = build_resource_from_arn(
        session, arn, "Amazon EKS", "us-east-1", _EXPECTED, tipo_recurso="cluster"
    )
    assert resource["status_tag"] == "ok"
    assert resource["tipo_recurso"] == "cluster"


def test_caminho_bedrock_usa_list_tags_for_resource(fake_session_factory):
    arn = "arn:aws:bedrock:us-east-1:000000000000:application-inference-profile/abc"
    session = fake_session_factory({"bedrock": _FakeBedrockClient({})})
    resource = build_resource_from_arn(
        session,
        arn,
        "Amazon Bedrock",
        "us-east-1",
        _EXPECTED,
        tipo_recurso="application_inference_profile",
    )
    assert resource["status_tag"] == "sem_tag"


def test_caminho_elb_de_eks_usa_describe_tags(fake_session_factory):
    arn = "arn:aws:elasticloadbalancing:us-east-1:000000000000:loadbalancer/app/meu-lb/abc"
    session = fake_session_factory({"elbv2": _FakeElbClient({"aws-apn-id": "pc:outro-valor"})})
    resource = build_resource_from_arn(
        session, arn, "Amazon EKS", "us-east-1", _EXPECTED, tipo_recurso="load_balancer"
    )
    assert resource["status_tag"] == "conflito"
    assert resource["valor_tag_encontrado"] == "pc:outro-valor"
