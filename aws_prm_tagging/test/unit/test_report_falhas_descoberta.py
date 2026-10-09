"""`report.build_report` — visibilidade de falhas de descoberta no JSON.

Antes, uma falha de descoberta (ex.: `AccessDenied` numa região) só ia para
o log — o relatório em si não tinha como distinguir "0 recursos porque a
conta realmente não tem nada elegível" de "0 recursos porque a descoberta
falhou aqui". Como a Etapa 4/dashboard só consome este JSON (nunca o log da
execução), essa ambiguidade escondia falhas reais em escala. Agora
`falhas_descoberta` torna isso visível no único artefato que importa.
"""
from __future__ import annotations

from aws_prm_tagging import report


def _recurso(arn: str = "arn:1") -> dict:
    return {
        "arn": arn,
        "servico": "Amazon S3",
        "regiao": "us-east-1",
        "tipo_recurso": None,
        "status_tag": "sem_tag",
        "valor_tag_encontrado": None,
        "tag_similar_encontrada": False,
        "tag_similar_chaves": [],
        "iac": {"tipo": "desconhecido", "stack_name": None},
    }


def test_sem_falhas_lista_fica_vazia_e_contador_zero():
    relatorio = report.build_report("000000000000", "pc:teste", [_recurso()], ou_tree=None)
    assert relatorio["falhas_descoberta"] == []
    assert relatorio["resumo"]["total_falhas_descoberta"] == 0


def test_falhas_descoberta_aparecem_no_relatorio_e_no_resumo():
    falhas = [
        {"regiao": "us-east-1", "etapa": "generico", "erro": "AccessDenied"},
        {"regiao": "sa-east-1", "etapa": "eks", "erro": "Throttling esgotado"},
    ]
    relatorio = report.build_report(
        "000000000000", "pc:teste", [_recurso()], ou_tree=None, falhas_descoberta=falhas
    )
    assert relatorio["falhas_descoberta"] == falhas
    assert relatorio["resumo"]["total_falhas_descoberta"] == 2


def test_zero_recursos_com_falha_e_zero_recursos_sem_falha_sao_distinguiveis():
    """O caso central que motivou este campo: duas execuções com
    `total_recursos == 0` não são mais indistinguíveis."""
    relatorio_sem_falha = report.build_report("000000000000", "pc:teste", [], ou_tree=None)
    relatorio_com_falha = report.build_report(
        "000000000000",
        "pc:teste",
        [],
        ou_tree=None,
        falhas_descoberta=[{"regiao": "us-east-1", "etapa": "generico", "erro": "AccessDenied"}],
    )
    assert relatorio_sem_falha["resumo"]["total_recursos"] == 0
    assert relatorio_sem_falha["resumo"]["total_falhas_descoberta"] == 0
    assert relatorio_com_falha["resumo"]["total_recursos"] == 0
    assert relatorio_com_falha["resumo"]["total_falhas_descoberta"] == 1
