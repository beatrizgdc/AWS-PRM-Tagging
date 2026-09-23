"""Funções puras (sem boto3) de `resource_discovery.py`.

O resto do módulo é testado via LocalStack (`test/localstack/`), não aqui —
essas duas funções foram extraídas especificamente para virarem testáveis
sem rede: `_filter_load_balancers_for_cluster` como parte da correção que
para de relistar os load balancers da região a cada cluster EKS
(`_list_region_load_balancers_with_tags` continua só testável via
LocalStack, por depender de boto3).
"""
from __future__ import annotations

from aws_prm_tagging import resource_discovery


def test_chunk_divide_em_lotes_do_tamanho_pedido():
    assert resource_discovery._chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_chunk_lista_vazia_devolve_lista_vazia():
    assert resource_discovery._chunk([], 100) == []


def test_filtro_load_balancer_por_cluster_usa_tag_do_load_balancer_controller():
    todos_os_lbs = [
        ("arn:lb-cluster-a", {"elbv2.k8s.aws/cluster": "cluster-a"}),
        ("arn:lb-cluster-b", {"kubernetes.io/cluster/cluster-b": "owned"}),
        ("arn:lb-sem-cluster", {"algum-outro-marcador": "valor"}),
    ]
    assert resource_discovery._filter_load_balancers_for_cluster(todos_os_lbs, "cluster-a") == [
        ("arn:lb-cluster-a", {"elbv2.k8s.aws/cluster": "cluster-a"})
    ]


def test_filtro_load_balancer_aceita_shared_alem_de_owned():
    todos_os_lbs = [("arn:lb-compartilhado", {"kubernetes.io/cluster/cluster-x": "shared"})]
    assert resource_discovery._filter_load_balancers_for_cluster(todos_os_lbs, "cluster-x") == todos_os_lbs


def test_filtro_load_balancer_nenhum_match_devolve_lista_vazia():
    todos_os_lbs = [("arn:lb-outro-cluster", {"elbv2.k8s.aws/cluster": "outro-cluster"})]
    assert resource_discovery._filter_load_balancers_for_cluster(todos_os_lbs, "cluster-x") == []
