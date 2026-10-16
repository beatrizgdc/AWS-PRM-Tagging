"""MVP2, item 5 — garantias de Well-Architected nos templates (travadas
por teste para não regredirem)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

INFRA = Path(__file__).resolve().parents[2] / "infra"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_multi_constructor("!", lambda loader, suffix, node: {
    "tag": suffix,
    "valor": loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode)
    else loader.construct_sequence(node, deep=True) if isinstance(node, yaml.SequenceNode)
    else loader.construct_mapping(node, deep=True),
})


def _t(nome):
    return yaml.load((INFRA / nome).read_text(encoding="utf-8"), Loader=_Loader)


@pytest.mark.parametrize("nome", ["template-hub.yaml", "template-regional.yaml"])
def test_toda_funcao_tem_log_group_com_retencao_apagado_com_a_stack(nome):
    t = _t(nome)
    res = t["Resources"]
    funcoes = [k for k, v in res.items() if v["Type"] == "AWS::Serverless::Function"]
    assert funcoes
    for f in funcoes:
        lg = res[f]["Properties"]["LoggingConfig"]["LogGroup"]["valor"]
        grupo = res[lg]
        assert grupo["Type"] == "AWS::Logs::LogGroup"
        assert grupo["DeletionPolicy"] == "Delete"
        assert grupo["Properties"]["RetentionInDays"]["valor"] == "RetencaoLogsDias"


@pytest.mark.parametrize("nome", ["template-hub.yaml", "template-regional.yaml", "template-zabbix.yaml"])
def test_arm64_e_layer_para_as_duas_arquiteturas(nome):
    t = _t(nome)
    assert t["Globals"]["Function"]["Architectures"]
    layer = t["Resources"]["PrmSdkLayer"]["Properties"]
    assert set(layer["CompatibleArchitectures"]) == {"x86_64", "arm64"}


def test_topico_do_hub_criptografado():
    assert _t("template-hub.yaml")["Resources"]["PrmComplianceTopic"]["Properties"]["KmsMasterKeyId"] == "alias/aws/sns"


def test_filas_criptografadas():
    assert _t("template-regional.yaml")["Resources"]["Etapa3EventsDlq"]["Properties"]["SqsManagedSseEnabled"] is True
    assert _t("template-zabbix.yaml")["Resources"]["ZabbixDlq"]["Properties"]["SqsManagedSseEnabled"] is True
    central = _t("template-central.yaml")["Resources"]
    assert central["TopicoCentral"]["Properties"]["KmsMasterKeyId"]
    assert central["FilaConsumidor"]["Properties"]["KmsMasterKeyId"]


def test_drift_tem_dlq_de_funcao():
    props = _t("template-regional.yaml")["Resources"]["Etapa4DriftFunction"]["Properties"]
    assert props["DeadLetterQueue"]["Type"] == "SQS"


def test_politicas_publicas_exigem_organizacao_e_tls():
    central = _t("template-central.yaml")["Resources"]
    pub, tls = central["TopicoCentralPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    assert "aws:PrincipalOrgID" in pub["Condition"]["StringEquals"]
    assert tls["Effect"] == "Deny" and tls["Condition"]["Bool"]["aws:SecureTransport"] == "false"
    chave = central["ChaveTopico"]["Properties"]["KeyPolicy"]["Statement"]
    org = next(s for s in chave if s["Sid"] == "PublicacaoPelasContasDaOrganizacao")
    assert "aws:PrincipalOrgID" in org["Condition"]["StringEquals"]
    assert central["ChaveTopico"]["Properties"]["EnableKeyRotation"] is True


@pytest.mark.parametrize("nome", ["template-hub.yaml", "template-regional.yaml"])
def test_escrita_de_tag_em_recurso_curinga_sempre_restrita_a_aws_apn_id(nome):
    texto = (INFRA / nome).read_text(encoding="utf-8")
    t = _t(nome)
    for logical, r in t["Resources"].items():
        if r["Type"] != "AWS::IAM::Role":
            continue
        for p in r["Properties"].get("Policies", []):
            for st in p["PolicyDocument"]["Statement"]:
                if not isinstance(st, dict) or "Action" not in st:
                    continue
                acoes = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
                escreve_tag = any(
                    ("Tag" in a.split(":")[1] and not a.split(":")[1].startswith(("List", "Get", "Describe")))
                    or a.endswith((":AddTags", ":CreateTags")) or a == "apigateway:PUT"
                    for a in acoes
                )
                if escreve_tag:
                    cond = st.get("Condition", {}).get("ForAllValues:StringEquals", {})
                    assert cond.get("aws:TagKeys") == ["aws-apn-id"], (logical, p["PolicyName"], acoes[:5])
    assert "apigateway:TagResource" not in texto and "cloudhsm:AddTagsToResource" not in texto
