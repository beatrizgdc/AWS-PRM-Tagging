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

_TEMPLATE_PATH = Path(__file__).parent.parent.parent / "infra" / "template.yaml"


def test_template_nao_tem_findings_do_cfn_lint():
    conteudo = _TEMPLATE_PATH.read_text(encoding="utf-8")
    matches = cfnlint_api.lint_all(conteudo)
    mensagens = [str(m) for m in matches]
    assert matches == [], "cfn-lint encontrou problema(s) em infra/template.yaml:\n" + "\n".join(mensagens)
