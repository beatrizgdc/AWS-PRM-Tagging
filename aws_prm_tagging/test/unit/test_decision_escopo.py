"""Filtragem de escopo de `decision.classify_resource`/`_em_escopo` (Etapa 2a).

Cobre os dois serviços com sub-recursos que precisam de checagem extra por
`tipo_recurso` (EKS e Bedrock — ver docstring de `decision.py`): os 5
sub-tipos de EKS que a Etapa 1 produz devem receber decisões independentes,
e os casos fora do escopo desta automação (EKS Fargate, Bedrock que não é
application inference profile) devem ser excluídos do relatório de decisão,
não aparecer em nenhuma categoria.
"""
from __future__ import annotations

from aws_prm_tagging import decision

EXPECTED = "pc:5ugbbrmu7ud3u5hsipfzug61p"


def test_eks_cluster_node_group_node_volume_lb_decisoes_independentes(recurso_factory):
    """Cada um dos 5 tipos de sub-recurso de EKS que `resource_discovery.py`
    produz (cluster, node_group, node, ebs_volume, load_balancer) é uma
    entidade separada para fins de tagueamento — cada uma recebe sua própria
    decisão, mesmo com o mesmo `servico` ("Amazon EKS")."""
    tipos = ["cluster", "node_group", "node", "ebs_volume", "load_balancer"]
    recursos = [
        recurso_factory(
            arn=f"arn:aws:eks:us-east-1:000000000000:{tipo}/x-{tipo}",
            servico="Amazon EKS",
            tipo_recurso=tipo,
            status_tag="sem_tag",
            iac_tipo="desconhecido",
        )
        for tipo in tipos
    ]
    resultados = [decision.classify_resource(r, EXPECTED) for r in recursos]
    assert all(r is not None for r in resultados)
    assert [r["tipo_recurso"] for r in resultados] == tipos
    assert all(r["decisao"] == decision.DECISAO_TAGUEAR for r in resultados)


def test_eks_fargate_fora_de_escopo_excluido(recurso_factory):
    """EKS Fargate não é suportado e deve ficar fora do escopo desta
    automação.

    `resource_discovery.py` nunca emite Fargate na prática — é excluído já
    na descoberta, por construção (Fargate on EKS não gera instâncias EC2).
    `"fargate_profile"` aqui é ilustrativo: simula qualquer `tipo_recurso`
    de EKS fora da lista suportada chegando na Etapa 2a (relatório
    sintético/malformado, ou uma mudança futura na Etapa 1) — é exatamente
    esse cenário que `_em_escopo` protege de propósito."""
    recurso = recurso_factory(servico="Amazon EKS", tipo_recurso="fargate_profile")
    assert decision.classify_resource(recurso, EXPECTED) is None


def test_bedrock_nao_application_inference_profile_excluido(recurso_factory):
    """Bedrock só é tagueável em application inference profiles — qualquer
    outro tipo de recurso Bedrock (aqui simulado com `tipo_recurso=None`,
    o valor que apareceria se a Etapa 1 um dia descobrisse Bedrock pelo
    passo genérico) fica fora do escopo desta automação."""
    recurso = recurso_factory(servico="Amazon Bedrock", tipo_recurso=None)
    assert decision.classify_resource(recurso, EXPECTED) is None


def test_bedrock_application_inference_profile_participa_normalmente(recurso_factory):
    """O único tipo de recurso Bedrock em escopo participa das 4 categorias
    de decisão normalmente, como qualquer outro serviço."""
    recurso = recurso_factory(
        arn="arn:aws:bedrock:us-east-1:000000000000:application-inference-profile/abc",
        servico="Amazon Bedrock",
        tipo_recurso="application_inference_profile",
        status_tag="ok",
        valor_tag_encontrado=EXPECTED,
    )
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado is not None
    assert resultado["decisao"] == decision.DECISAO_JA_OK
