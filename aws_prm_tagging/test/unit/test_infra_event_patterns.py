"""Guarda de deriva entre `infra/template.yaml` e os arquivos
`infra/event_pattern.*.generated.json` (gerados por `event_mapping.py`).

Não há como o CloudFormation incluir um JSON externo dentro de
`AWS::Events::Rule.EventPattern` (ao contrário de `AWS::Serverless::
StateMachine`, que tem `DefinitionUri`) — os blocos `EventPattern` em
`template.yaml` são colados manualmente a partir dos arquivos gerados. Este
teste é o que garante que essa cópia manual não diverge silenciosamente."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_INFRA_DIR = Path(__file__).parent.parent.parent / "infra"


class _PermissiveLoader(yaml.SafeLoader):
    """Trata qualquer tag CloudFormation (`!Ref`, `!Sub`, `!GetAtt`, ...)
    como um valor opaco em vez de falhar — este teste só precisa inspecionar
    `EventPattern`, que não usa nenhuma dessas tags."""


def _construct_any(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


_PermissiveLoader.add_multi_constructor("!", _construct_any)


def _load_template() -> dict:
    with open(_INFRA_DIR / "template.yaml", encoding="utf-8") as f:
        return yaml.load(f, Loader=_PermissiveLoader)


def test_toda_regra_de_evento_do_template_bate_com_o_arquivo_gerado():
    template = _load_template()
    arquivos_gerados = sorted(_INFRA_DIR.glob("event_pattern.*.generated.json"))
    assert arquivos_gerados, "nenhum infra/event_pattern.*.generated.json encontrado"

    for i, arquivo in enumerate(arquivos_gerados):
        with open(arquivo, encoding="utf-8") as f:
            esperado = json.load(f)
        nome_recurso = f"Etapa3EventRule{i}"
        recurso = template["Resources"].get(nome_recurso)
        assert recurso is not None, (
            f"{arquivo.name} existe, mas não há recurso '{nome_recurso}' em template.yaml — "
            "o número de regras mudou e o template não foi atualizado (ver infra/README.md)."
        )
        pattern_no_template = recurso["Properties"]["EventPattern"]
        assert pattern_no_template == esperado, f"EventPattern de '{nome_recurso}' diverge de {arquivo.name}"

    # E o inverso: nenhuma regra "a mais" sem arquivo gerado correspondente.
    indices_de_regra = [
        nome for nome in template["Resources"] if nome.startswith("Etapa3EventRule")
    ]
    assert len(indices_de_regra) == len(arquivos_gerados), (
        f"template.yaml tem {len(indices_de_regra)} regra(s) Etapa3EventRule*, mas há "
        f"{len(arquivos_gerados)} arquivo(s) gerado(s) — remova a(s) regra(s) extra(s) ou regenere."
    )
