"""`main._validate_expected_tag_value` — validação do formato `pc:<código>`.

Antes só emitia um aviso (`logger.warning`) e ainda mencionava um formato
alternativo (`ra-...`) já descartado para este projeto. Como o formato está
fechado (`pc:<product-code>`), um valor fora do padrão é quase certamente
engano de quem digitou o comando — vira erro de verdade agora, não aviso.
"""
from __future__ import annotations

from aws_prm_tagging import main


def test_valor_valido_aceito():
    assert main._validate_expected_tag_value("pc:5ugbbrmu7ud3u5hsipfzug61p") is True


def test_valor_sem_prefixo_pc_rejeitado():
    assert main._validate_expected_tag_value("5ugbbrmu7ud3u5hsipfzug61p") is False


def test_formato_ra_descartado_rejeitado():
    """`ra-...` já foi descartado como formato válido para este projeto —
    não deveria mais passar nem como aviso."""
    assert main._validate_expected_tag_value("ra-1234567890123") is False


def test_valor_vazio_apos_prefixo_rejeitado():
    assert main._validate_expected_tag_value("pc:") is False


def test_caracter_invalido_apos_prefixo_rejeitado():
    assert main._validate_expected_tag_value("pc:valor com espaço") is False
