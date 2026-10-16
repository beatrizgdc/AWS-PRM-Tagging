"""Etapa 4 — auditar-org e verificar-csv. Offline."""
from __future__ import annotations

import json

import pytest

from aws_prm_tagging import csv_check, main, org_audit

V = "pc:abc123"

# ------------------------------- SCP -------------------------------


def test_scp_deny_amplo_bloqueia_tagueamento():
    achados = org_audit.analisar_scp({"Statement": [{"Effect": "Deny", "Action": "*", "Resource": "*"}]})
    assert achados[0]["achado"] == "deny_em_tagueamento" and "tag:TagResources" in achados[0]["acoes_alcancadas"]


def test_scp_deny_com_condicao_e_condicional_e_cita_a_tag():
    achados = org_audit.analisar_scp({"Statement": [{"Sid": "ProtegeTag", "Effect": "Deny", "Action": ["ec2:CreateTags", "tag:TagResources"],
        "Resource": "*", "Condition": {"ForAnyValue:StringEquals": {"aws:TagKeys": ["aws-apn-id"]}}}]})
    assert achados[0]["achado"] == "deny_condicional_em_tagueamento" and achados[0]["menciona_aws_apn_id"] is True


def test_scp_restricao_de_regiao_com_notaction_que_isenta_tag():
    """NotAction com tag:* isento: não bloqueia tag:TagResources; restrição de região sinalizada."""
    achados = org_audit.analisar_scp({"Statement": [{"Effect": "Deny", "NotAction": ["tag:*", "ec2:*", "s3:*", "rds:*", "elasticloadbalancing:*",
        "lambda:*", "eks:*", "ecs:*", "dynamodb:*"], "Resource": "*", "Condition": {"StringNotEquals": {"aws:RequestedRegion": ["us-east-1"]}}}]})
    assert [a["achado"] for a in achados] == ["restringe_regioes"]


def test_scp_allow_e_deny_sem_tag_sao_ignorados():
    assert org_audit.analisar_scp({"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"},
                                                 {"Effect": "Deny", "Action": "iam:CreateUser", "Resource": "*"}]}) == []


# ---------------------------- tag policy ----------------------------


@pytest.mark.parametrize(
    "regra,achado",
    [
        ({"tag_key": {"@@assign": "aws-apn-id"}}, "tag_policy_compativel"),
        ({"tag_key": {"@@assign": "AWS-APN-ID"}}, "tag_policy_nao_conforme"),
        ({"tag_key": {"@@assign": "AWS-APN-ID"}, "enforced_for": {"@@assign": ["ec2:instance"]}}, "tag_policy_bloqueia"),
        ({"tag_key": {"@@assign": "aws-apn-id"}, "tag_value": {"@@assign": ["pc:outro"]}, "enforced_for": {"@@assign": ["s3:bucket"]}}, "tag_policy_bloqueia"),
        ({"tag_key": {"@@assign": "aws-apn-id"}, "tag_value": {"@@assign": ["pc:*"]}}, "tag_policy_compativel"),
    ],
    ids=["ok", "caixa", "caixa_enforced", "valor_fora", "curinga"],
)
def test_tag_policy(regra, achado):
    assert org_audit.analisar_tag_policy({"tags": {"apn": regra}}, V)[0]["achado"] == achado


def test_tag_policy_de_outra_chave_e_ignorada():
    assert org_audit.analisar_tag_policy({"tags": {"cc": {"tag_key": {"@@assign": "CostCenter"}}}}, V) == []


moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")


def test_auditar_org_contra_organizations_simulado(monkeypatch, tmp_path):
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"), ("AWS_DEFAULT_REGION", "us-east-1")):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with moto.mock_aws():
        org = boto3.client("organizations", region_name="us-east-1")
        org.create_organization(FeatureSet="ALL")
        raiz = org.list_roots()["Roots"][0]["Id"]
        for tipo in ("SERVICE_CONTROL_POLICY", "TAG_POLICY"):
            try:
                org.enable_policy_type(RootId=raiz, PolicyType=tipo)
            except Exception:  # noqa: BLE001 — já habilitado no simulador
                pass
        scp = org.create_policy(Name="sem-tag", Type="SERVICE_CONTROL_POLICY", Description="d",
                                Content=json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Deny", "Action": "tag:*", "Resource": "*"}]}))
        org.attach_policy(PolicyId=scp["Policy"]["PolicySummary"]["Id"], TargetId=raiz)
        org.create_policy(Name="apn", Type="TAG_POLICY", Description="d",
                          Content=json.dumps({"tags": {"apn": {"tag_key": {"@@assign": "AWS-APN-ID"}}}}))
        saida = tmp_path / "a.json"
        codigo = main.run(["auditar-org", "--expected-tag-value", V, "--output", str(saida)])
        r = json.loads(saida.read_text())
    assert codigo == 2
    assert r["resumo"]["bloqueios_provaveis"] == 1
    assert r["scps"][0]["alvos"][0]["id"] == raiz
    assert r["tag_policies"][0]["achados"][0]["achado"] == "tag_policy_nao_conforme"


# ---------------------------- verificar-csv ----------------------------

_HTML = """<html><body><h1>Resource Tagging Included Services</h1>
<div class="table-container"><div class="table-contents"><table id="w1">
<thead><tr><th>Service Name</th><th>Product Service Code</th><th>Notes</th></tr></thead>
<tr><td tabindex="-1">Amazon API Gateway</td><td tabindex="-1">AmazonApiGateway</td><td tabindex="-1">None</td></tr>
<tr><td tabindex="-1">Amazon AppStream</td><td tabindex="-1">AmazonAppStream</td><td tabindex="-1">Excludes
   User Fees</td></tr>
<tr><td tabindex="-1">Amazon Servico Novo</td><td tabindex="-1">AmazonNovo</td><td tabindex="-1">None</td></tr>
</table></div></div></body></html>"""


def test_extrai_tabela_normalizando_espacos_e_none():
    assert csv_check.extrair_servicos_da_pagina(_HTML) == [
        ("Amazon API Gateway", "AmazonApiGateway", ""),
        ("Amazon AppStream", "AmazonAppStream", "Excludes User Fees"),
        ("Amazon Servico Novo", "AmazonNovo", ""),
    ]


def test_pagina_sem_a_tabela_falha_explicitamente():
    with pytest.raises(ValueError):
        csv_check.extrair_servicos_da_pagina("<html><table><tr><td>x</td></tr></table></html>")


def test_comparar_aponta_novo_removido_e_nota():
    local = [("Amazon API Gateway", "AmazonApiGateway", ""), ("Amazon AppStream", "AmazonAppStream", "Antiga"),
             ("Servico Saiu", "AmazonSaiu", "")]
    d = csv_check.comparar(csv_check.extrair_servicos_da_pagina(_HTML), local)
    assert [n["servico"] for n in d["novos_na_pagina"]] == ["Amazon Servico Novo"]
    assert d["codigos_novos_sem_mapeamento"] == ["AmazonNovo"]
    assert [r["servico"] for r in d["removidos_da_pagina"]] == ["Servico Saiu"]
    assert d["nota_alterada"][0]["agora"] == "Excludes User Fees" and d["tem_diferenca"]


def test_csv_empacotado_bate_consigo_mesmo():
    local = csv_check._csv_local()
    assert len(local) == 94 and not csv_check.comparar(local, local)["tem_diferenca"]


def test_cli_verificar_csv_codigo_3_quando_ha_diferenca(monkeypatch, tmp_path):
    monkeypatch.setattr(csv_check, "baixar", lambda url: _HTML)
    assert main.run(["verificar-csv", "--output", str(tmp_path / "v.json")]) == 3
