"""`resource_discovery.discover_*` — visibilidade de falha de API no
retorno da função, não só no log.

Antes, uma falha de `ClientError` (ex.: `AccessDenied`, throttling
esgotado, erro no meio da paginação) era capturada dentro de cada
`discover_*` e só ia para o log — nunca propagava para `main.py`, que só
conseguia ver falha via `except Exception` (bug de programação, não falha
de API esperada). Como `falhas_descoberta` é o que a Etapa 4/dashboard lê
(nunca o log da execução), essas falhas ficavam invisíveis no relatório na
prática. Agora cada `discover_*` devolve `(recursos, falhas)` e `main.py`
só propaga as duas listas.

Usa fakes simples (sem moto/LocalStack), no mesmo espírito de
`test_tag_execution_execucao.py` — suficiente para cobrir o roteamento de
falha -> `falhas_descoberta`, não a integração real com a AWS (isso
continua em `test/localstack/`).
"""
from __future__ import annotations

from botocore.exceptions import ClientError

from aws_prm_tagging import resource_discovery, services


def _service_list():
    return services.load_services()


class _FakeSession:
    def __init__(self, clients: dict):
        self._clients = clients

    def client(self, service_name: str, region_name: str | None = None):
        return self._clients[service_name]


# ---------------------------------------------------------------------------
# discover_generic_resources
# ---------------------------------------------------------------------------


class _FakeTaggingClientErro:
    def get_resources(self, **kwargs):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "sem permissão"}}, "GetResources")


def test_discover_generic_resources_falha_vira_falha_descoberta():
    session = _FakeSession({"resourcegroupstaggingapi": _FakeTaggingClientErro()})
    recursos, falhas = resource_discovery.discover_generic_resources(
        session, "us-east-1", _service_list(), "pc:teste"
    )
    assert recursos == []
    assert len(falhas) == 1
    assert falhas[0] == {
        "regiao": "us-east-1",
        "etapa": "generico",
        "erro": falhas[0]["erro"],
    }
    assert "AccessDeniedException" in falhas[0]["erro"]


# ---------------------------------------------------------------------------
# discover_bedrock_resources
# ---------------------------------------------------------------------------


class _FakeBedrockClientTagFalha:
    def list_inference_profiles(self, **kwargs):
        return {"inferenceProfileSummaries": [{"inferenceProfileArn": "arn:bedrock:profile/1"}]}

    def list_tags_for_resource(self, resourceARN):
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "sem permissão de leitura de tags"}},
            "ListTagsForResource",
        )


def test_discover_bedrock_resources_falha_ao_ler_tags_vira_falha_mas_mantem_recurso():
    """O recurso não pode ficar invisível (ver limitação do Resource
    Explorer em melhorias-futuras.md), mas o relatório de compliance não
    pode mais dizer "sem_tag" com certeza quando a leitura falhou — a falha
    agora fica registrada lado a lado."""
    session = _FakeSession({"bedrock": _FakeBedrockClientTagFalha()})
    recursos, falhas = resource_discovery.discover_bedrock_resources(
        session, "us-east-1", _service_list(), "pc:teste"
    )
    assert len(recursos) == 1
    assert recursos[0]["arn"] == "arn:bedrock:profile/1"
    assert recursos[0]["status_tag"] == "sem_tag"
    assert len(falhas) == 1
    assert falhas[0]["etapa"] == "bedrock"
    assert "arn:bedrock:profile/1" in falhas[0]["erro"]


class _FakeBedrockClientAccessDeniedGeral:
    def list_inference_profiles(self, **kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "sem acesso ao bedrock"}}, "ListInferenceProfiles"
        )


def test_discover_bedrock_resources_access_denied_geral_tambem_vira_falha_descoberta():
    """Regressão do bug corrigido: uma versão anterior tratava
    `AccessDeniedException`/`UnrecognizedClientException` ao listar profiles
    como "região sem Bedrock, esperado" e NÃO registrava falha — testado e
    comprovado errado (ver melhorias-futuras.md): `AccessDenied` tipicamente
    é falta de permissão IAM, não ausência regional do serviço, e as duas
    coisas não dão pra distinguir com segurança só pelo código do erro. Sem
    registrar a falha, uma role sem `bedrock:ListInferenceProfiles` produzia
    "0 profiles" com confiança total em toda região, escondendo um problema
    de permissão real na conta inteira."""
    session = _FakeSession({"bedrock": _FakeBedrockClientAccessDeniedGeral()})
    recursos, falhas = resource_discovery.discover_bedrock_resources(
        session, "us-east-1", _service_list(), "pc:teste"
    )
    assert recursos == []
    assert len(falhas) == 1
    assert falhas[0]["etapa"] == "bedrock"
    assert "AccessDeniedException" in falhas[0]["erro"]


class _FakeBedrockClientErroGenerico:
    def list_inference_profiles(self, **kwargs):
        raise ClientError({"Error": {"Code": "ValidationException", "Message": "parâmetro inválido"}}, "ListInferenceProfiles")


def test_discover_bedrock_resources_erro_nao_access_denied_vira_falha_descoberta():
    session = _FakeSession({"bedrock": _FakeBedrockClientErroGenerico()})
    recursos, falhas = resource_discovery.discover_bedrock_resources(
        session, "us-east-1", _service_list(), "pc:teste"
    )
    assert recursos == []
    assert len(falhas) == 1
    assert falhas[0]["etapa"] == "bedrock"


# ---------------------------------------------------------------------------
# discover_eks_resources
# ---------------------------------------------------------------------------


class _FakeEksClientListClustersErro:
    def list_clusters(self, **kwargs):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "sem permissão eks"}}, "ListClusters")


def test_discover_eks_resources_falha_ao_listar_clusters_vira_falha_descoberta():
    """`ec2`/`autoscaling`/`elbv2` propositalmente NÃO stubados (`object()`
    simples) — se o código tentasse usá-los depois dessa falha, o teste
    quebraria com `AttributeError` em vez de mascarar o bug."""
    session = _FakeSession(
        {
            "eks": _FakeEksClientListClustersErro(),
            "ec2": object(),
            "autoscaling": object(),
            "elbv2": object(),
        }
    )
    recursos, falhas = resource_discovery.discover_eks_resources(session, "us-east-1", "000000000000", "pc:teste")
    assert recursos == []
    assert len(falhas) == 1
    assert falhas[0]["etapa"] == "eks"
    assert "list_clusters" in falhas[0]["erro"]


class _FakeEksClientDescribeClusterErro:
    def list_clusters(self, **kwargs):
        return {"clusters": ["cluster-1"]}

    def describe_cluster(self, name):
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "sem permissão de describe"}}, "DescribeCluster"
        )


class _FakeElbv2ClientSemLoadBalancers:
    def describe_load_balancers(self, **kwargs):
        return {"LoadBalancers": []}


def test_discover_eks_resources_falha_ao_descrever_cluster_vira_falha_e_pula_cluster():
    session = _FakeSession(
        {
            "eks": _FakeEksClientDescribeClusterErro(),
            "ec2": object(),
            "autoscaling": object(),
            "elbv2": _FakeElbv2ClientSemLoadBalancers(),
        }
    )
    recursos, falhas = resource_discovery.discover_eks_resources(session, "us-east-1", "000000000000", "pc:teste")
    assert recursos == []
    assert len(falhas) == 1
    assert falhas[0]["etapa"] == "eks"
    assert "cluster-1" in falhas[0]["erro"]


class _FakeElbv2ClientErro:
    def describe_load_balancers(self, **kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "sem permissão elbv2"}}, "DescribeLoadBalancers"
        )


class _FakeEksClientNenhumClusterDetalhado:
    def list_clusters(self, **kwargs):
        return {"clusters": ["cluster-1"]}

    def describe_cluster(self, name):
        return {"cluster": {"arn": f"arn:eks:cluster/{name}", "tags": {}}}

    def list_nodegroups(self, clusterName, **kwargs):
        return {"nodegroups": []}


class _FakeEc2ClientVazio:
    def describe_instances(self, **kwargs):
        return {"Reservations": []}


def test_discover_eks_resources_falha_ao_listar_load_balancers_vira_falha_mas_nao_aborta():
    """A falha de ELB é "larga" (região inteira), mas não pode impedir o
    resto da descoberta EKS (cluster/nodegroups) de continuar."""
    session = _FakeSession(
        {
            "eks": _FakeEksClientNenhumClusterDetalhado(),
            "ec2": _FakeEc2ClientVazio(),
            "autoscaling": object(),
            "elbv2": _FakeElbv2ClientErro(),
        }
    )
    recursos, falhas = resource_discovery.discover_eks_resources(session, "us-east-1", "000000000000", "pc:teste")
    assert len(recursos) == 1  # o cluster em si ainda é descoberto
    assert recursos[0]["tipo_recurso"] == resource_discovery.TIPO_RECURSO_CLUSTER
    assert len(falhas) == 1
    assert falhas[0]["etapa"] == "eks"
    assert "describe_load_balancers" in falhas[0]["erro"]
