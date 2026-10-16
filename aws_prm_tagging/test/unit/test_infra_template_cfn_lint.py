"""`cfn-lint` contra `infra/template.yaml` — validação estática (sem AWS,
sem Docker) das regras de CloudFormation/SAM: tipos de recurso válidos,
propriedades obrigatórias, referências (`!Ref`/`!GetAtt`) que resolvem para
um recurso que existe, etc. Roda via `pytest.importorskip` — some
silenciosamente se `cfn-lint` não estiver instalado no ambiente (não é uma
dependência de runtime do projeto, só de desenvolvimento/CI)."""
from __future__ import annotations

from pathlib import Path

import pytest

cfnlint_api = pytest.importorskip("cfnlint.api")

_INFRA = Path(__file__).parent.parent.parent / "infra"


@pytest.mark.parametrize(
    "nome", ["template-regional.yaml", "template-hub.yaml", "template-central.yaml", "template-zabbix.yaml"]
)
def test_template_nao_tem_findings_do_cfn_lint(nome):
    conteudo = (_INFRA / nome).read_text(encoding="utf-8")
    # Só erros (E*) e avisos (W*) contam. Achados informativos (I*) — ex.:
    # I4010, introduzido em versões recentes do cfn-lint pedindo um bloco
    # Metadata de "contexto de arquitetura" — não indicam template inválido
    # e quebravam este teste só por atualização da ferramenta.
    matches = [m for m in cfnlint_api.lint_all(conteudo) if not m.rule.id.startswith("I")]
    mensagens = [str(m) for m in matches]
    assert matches == [], f"cfn-lint encontrou problema(s) em infra/{nome}:\n" + "\n".join(mensagens)
