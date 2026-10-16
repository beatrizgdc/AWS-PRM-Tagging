"""boto3/botocore atuais empacotados numa Lambda Layer."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parents[3]
PKG = RAIZ / "aws_prm_tagging"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _template(nome):
    return yaml.load((PKG / "infra" / nome).read_text(encoding="utf-8"), Loader=_Loader)


def _requisitos():
    texto = (PKG / "requirements-lambda.txt").read_text(encoding="utf-8")
    return dict(re.findall(r"^([a-z0-9-]+)==(\S+)", texto, flags=re.M)), texto


def test_requirements_fixa_versao_e_hash_de_tudo():
    pins, texto = _requisitos()
    assert {"boto3", "botocore", "s3transfer", "jmespath", "urllib3", "python-dateutil", "six"} <= set(pins)
    assert pins["boto3"] == pins["botocore"]  # par de versões casado
    assert texto.count("--hash=sha256:") == len(pins)


def test_makefile_monta_a_layer_com_require_hashes():
    mk = (RAIZ / "Makefile").read_text(encoding="utf-8")
    alvo = mk.split("build-PrmSdkLayer:", 1)[1]
    assert "--require-hashes" in alvo and "requirements-lambda.txt" in alvo
    assert '$(ARTIFACTS_DIR)/python' in alvo
    assert "--python-version 3.12" in alvo


def test_templates_usam_a_layer_em_todas_as_funcoes():
    for nome in ("template-hub.yaml", "template-regional.yaml"):
        t = _template(nome)
        assert t["Resources"]["PrmSdkLayer"]["Type"] == "AWS::Serverless::LayerVersion"
        assert t["Resources"]["PrmSdkLayer"]["Metadata"]["BuildMethod"] == "makefile"
        assert "Layers" in t["Globals"]["Function"]
        for res in t["Resources"].values():
            if res.get("Type") == "AWS::Serverless::Function":
                assert "Layers" not in (res.get("Properties") or {}), "Layers por função sobrescreveria o Global"
                assert (res.get("Properties") or {}).get("Runtime", t["Globals"]["Function"].get("Runtime")) in (
                    None, "python3.12"
                )

