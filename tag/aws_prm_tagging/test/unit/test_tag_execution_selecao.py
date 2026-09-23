"""Garantia estrutural de `tag_execution.select_taggable` e roteamento de
API (`tag_execution.route_strategy`) — Etapa 2b.

`select_taggable` é o único ponto de entrada aceito pelo resto do módulo:
cobre aqui que ele filtra estritamente por `decisao == "taguear"` e que o
tipo devolvido (`TaggableResource`) não carrega o campo `decisao` — a
garantia estrutural descrita no docstring de `tag_execution.py` (não dá
para repassar um recurso `pular_iac`/`ja_ok`/`conflito` adiante por engano,
porque o tipo nunca teve esse campo)."""
from __future__ import annotations

import dataclasses

from aws_prm_tagging import tag_execution
from aws_prm_tagging.decision import (
    DECISAO_CONFLITO,
    DECISAO_JA_OK,
    DECISAO_PULAR_IAC,
    DECISAO_TAGUEAR,
)


def test_taggable_resource_nao_tem_campo_decisao():
    """Checagem de tipo, não de comportamento: `TaggableResource` nunca
    carregou `decisao` — é estruturalmente impossível vazar um recurso da
    categoria errada para o executor via este tipo."""
    campos = {f.name for f in dataclasses.fields(tag_execution.TaggableResource)}
    assert "decisao" not in campos


def test_select_taggable_inclui_apenas_decisao_taguear(decisao_factory, relatorio_decisao_factory):
    recursos = [
        decisao_factory(arn="arn:taguear", decisao=DECISAO_TAGUEAR),
        decisao_factory(arn="arn:pular-iac", decisao=DECISAO_PULAR_IAC),
        decisao_factory(arn="arn:ja-ok", decisao=DECISAO_JA_OK),
        decisao_factory(arn="arn:conflito", decisao=DECISAO_CONFLITO),
    ]
    relatorio = relatorio_decisao_factory(recursos)

    selecionados = tag_execution.select_taggable(relatorio)

    assert [r.arn for r in selecionados] == ["arn:taguear"]


def test_select_taggable_relatorio_vazio(relatorio_decisao_factory):
    assert tag_execution.select_taggable(relatorio_decisao_factory([])) == []


def test_select_taggable_recurso_sem_campo_decisao_e_excluido(relatorio_decisao_factory):
    """Um dict sem `decisao` (ex.: uma entrada de `erros` que por engano
    fosse parar em `recursos`) nunca é interpretado como `taguear` por
    omissão — `.get("decisao")` devolve `None`, que não bate com
    `DECISAO_TAGUEAR`."""
    relatorio = relatorio_decisao_factory([{"arn": "arn:sem-decisao", "servico": "Amazon EC2", "regiao": "us-east-1"}])
    assert tag_execution.select_taggable(relatorio) == []


def test_route_strategy_generico_por_default(decisao_factory):
    recurso = tag_execution.select_taggable(
        {"recursos": [decisao_factory(servico="Amazon S3", tipo_recurso=None)]}
    )[0]
    assert tag_execution.route_strategy(recurso) is tag_execution.ApiStrategy.GENERICO


def test_route_strategy_eks_cluster_dedicado(decisao_factory):
    recurso = tag_execution.select_taggable(
        {"recursos": [decisao_factory(servico="Amazon EKS", tipo_recurso="cluster")]}
    )[0]
    assert tag_execution.route_strategy(recurso) is tag_execution.ApiStrategy.EKS_CLUSTER


def test_route_strategy_eks_node_group_dedicado(decisao_factory):
    recurso = tag_execution.select_taggable(
        {"recursos": [decisao_factory(servico="Amazon EKS", tipo_recurso="node_group")]}
    )[0]
    assert tag_execution.route_strategy(recurso) is tag_execution.ApiStrategy.EKS_NODE_GROUP


def test_route_strategy_eks_load_balancer_dedicado(decisao_factory):
    recurso = tag_execution.select_taggable(
        {"recursos": [decisao_factory(servico="Amazon EKS", tipo_recurso="load_balancer")]}
    )[0]
    assert tag_execution.route_strategy(recurso) is tag_execution.ApiStrategy.ELB_LOAD_BALANCER


def test_route_strategy_eks_node_e_ebs_volume_usam_caminho_generico(decisao_factory):
    """Nodes e volumes EBS do EKS são instâncias/volumes EC2 comuns por
    baixo do capô — não entram na tabela de estratégia dedicada, ao
    contrário de cluster/node_group/load_balancer (ver docstring do
    módulo)."""
    node = tag_execution.select_taggable({"recursos": [decisao_factory(servico="Amazon EKS", tipo_recurso="node")]})[0]
    volume = tag_execution.select_taggable(
        {"recursos": [decisao_factory(servico="Amazon EKS", tipo_recurso="ebs_volume")]}
    )[0]
    assert tag_execution.route_strategy(node) is tag_execution.ApiStrategy.GENERICO
    assert tag_execution.route_strategy(volume) is tag_execution.ApiStrategy.GENERICO


def test_route_strategy_bedrock_application_inference_profile_dedicado(decisao_factory):
    recurso = tag_execution.select_taggable(
        {"recursos": [decisao_factory(servico="Amazon Bedrock", tipo_recurso="application_inference_profile")]}
    )[0]
    assert tag_execution.route_strategy(recurso) is tag_execution.ApiStrategy.BEDROCK_PROFILE
