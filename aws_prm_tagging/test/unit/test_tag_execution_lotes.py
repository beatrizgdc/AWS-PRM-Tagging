"""Agrupamento em lotes (Etapas 2b/2c) — `tag_execution.run_tagging_execution`.

Cobre que o caminho genérico agrupa por região em lotes de até 20 ARNs
(limite documentado de `tag:TagResources`), que regiões diferentes nunca são
misturadas no mesmo lote, e que os caminhos dedicados (EKS/Bedrock/ELB)
nunca são agrupados — sempre 1 recurso por chamada de escrita, conforme
`tag_execution._ESTRATEGIA_DEDICADA`.

Todos os testes aqui usam `revalidate=False` para isolar o comportamento de
agrupamento da lógica de revalidação (coberta em
`test_tag_execution_execucao.py`), e um `Executor` espião em vez de
`DryRunExecutor` para inspecionar exatamente quais lotes o orquestrador
monta.
"""
from __future__ import annotations

from aws_prm_tagging import tag_execution

TAG_VALUE = "pc:5ugbbrmu7ud3u5hsipfzug61p"


class _SpyExecutor:
    """Registra cada chamada em vez de logar/executar — permite inspecionar
    exatamente como `run_tagging_execution` agrupou os recursos, sem
    depender de parsing de log."""

    def __init__(self):
        self.chamadas_lote: list[tuple[str, list[str]]] = []
        self.chamadas_single: list[tuple[tag_execution.ApiStrategy, str]] = []

    def tag_generic_batch(self, session, regiao, arns, tag_key, tag_value):
        self.chamadas_lote.append((regiao, list(arns)))
        return {arn: tag_execution.ResourceOutcome(resultado=tag_execution.RESULTADO_SIMULADO_OK) for arn in arns}

    def tag_single(self, session, estrategia, resource, tag_key, tag_value):
        self.chamadas_single.append((estrategia, resource.arn))
        return tag_execution.ResourceOutcome(resultado=tag_execution.RESULTADO_SIMULADO_OK)


def _relatorio_generico(quantidade: int, regiao: str = "us-east-1") -> dict:
    return {
        "conta_id": "000000000000",
        "recursos": [
            {
                "arn": f"arn:aws:s3:::bucket-{i}",
                "servico": "Amazon S3",
                "regiao": regiao,
                "tipo_recurso": None,
                "decisao": "taguear",
            }
            for i in range(quantidade)
        ],
    }


def test_lote_generico_25_arns_vira_dois_lotes_20_mais_5():
    spy = _SpyExecutor()
    tag_execution.run_tagging_execution(
        _relatorio_generico(25), session=None, expected_tag_value=TAG_VALUE, revalidate=False, executor=spy
    )
    tamanhos = sorted(len(arns) for _, arns in spy.chamadas_lote)
    assert tamanhos == [5, 20]


def test_lote_generico_exatamente_20_arns_vira_um_unico_lote():
    spy = _SpyExecutor()
    tag_execution.run_tagging_execution(
        _relatorio_generico(20), session=None, expected_tag_value=TAG_VALUE, revalidate=False, executor=spy
    )
    assert len(spy.chamadas_lote) == 1
    assert len(spy.chamadas_lote[0][1]) == 20


def test_regioes_diferentes_nunca_misturadas_no_mesmo_lote():
    relatorio = {
        "conta_id": "000000000000",
        "recursos": [
            {"arn": "arn:us-east-1:a", "servico": "Amazon S3", "regiao": "us-east-1", "tipo_recurso": None, "decisao": "taguear"},
            {"arn": "arn:sa-east-1:b", "servico": "Amazon S3", "regiao": "sa-east-1", "tipo_recurso": None, "decisao": "taguear"},
        ],
    }
    spy = _SpyExecutor()
    tag_execution.run_tagging_execution(relatorio, session=None, expected_tag_value=TAG_VALUE, revalidate=False, executor=spy)

    regioes_por_lote = {regiao for regiao, _ in spy.chamadas_lote}
    assert regioes_por_lote == {"us-east-1", "sa-east-1"}
    assert len(spy.chamadas_lote) == 2


def test_recursos_dedicados_nunca_agrupados_um_por_chamada(decisao_factory, relatorio_decisao_factory):
    """EKS cluster, node group e Bedrock: cada um vira uma chamada
    `tag_single` isolada, nunca `tag_generic_batch` — reflete que nenhuma
    dessas APIs aceita mais de 1 ARN por chamada de escrita (ver docstring
    do módulo)."""
    recursos = [
        decisao_factory(arn="arn:cluster-1", servico="Amazon EKS", tipo_recurso="cluster"),
        decisao_factory(arn="arn:cluster-2", servico="Amazon EKS", tipo_recurso="cluster"),
        decisao_factory(arn="arn:profile-1", servico="Amazon Bedrock", tipo_recurso="application_inference_profile"),
    ]
    spy = _SpyExecutor()
    tag_execution.run_tagging_execution(
        relatorio_decisao_factory(recursos), session=None, expected_tag_value=TAG_VALUE, revalidate=False, executor=spy
    )

    assert spy.chamadas_lote == []
    assert {arn for _, arn in spy.chamadas_single} == {"arn:cluster-1", "arn:cluster-2", "arn:profile-1"}
