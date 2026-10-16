"""MVP2, item 2 — rollout por StackSets: empacotamento (template expandido
sem transform, bucket por região) e a CLI deploy/stacksets/stackset.py
(publicar/implantar/remover) — com moto (S3/Organizations) e um
CloudFormation falso (StackSets não é coberto pelo moto)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

RAIZ = Path(__file__).resolve().parents[3]


def _carregar(nome, caminho):
    spec = importlib.util.spec_from_file_location(nome, caminho)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[nome] = mod
    spec.loader.exec_module(mod)
    return mod


empacotar = _carregar("prm_empacotar", RAIZ / "deploy/stacksets/empacotar.py")
stackset = _carregar("prm_stackset", RAIZ / "deploy/stacksets/stackset.py")


# ---------------------------------------------------------------------------
# empacotar
# ---------------------------------------------------------------------------


def _build_falso(tmp_path: Path, template: str) -> Path:
    """Simula a saída do `sam build` (sem precisar do SAM CLI): mesmo
    template, com CodeUri/ContentUri apontando para pastas locais."""
    build = tmp_path / f"build-{template}"
    (build / "codigo").mkdir(parents=True)
    (build / "codigo" / "x.py").write_text("print(1)\n")
    (build / "layer" / "python").mkdir(parents=True)
    (build / "layer" / "python" / "y.py").write_text("print(2)\n")
    tpl = empacotar.carregar_template(empacotar.TEMPLATES[template])
    for res in tpl["Resources"].values():
        props = res.get("Properties") or {}
        if res["Type"] == "AWS::Serverless::Function":
            props["CodeUri"] = "codigo"
        elif res["Type"] == "AWS::Serverless::LayerVersion":
            props["ContentUri"] = "layer"
        elif res["Type"] == "AWS::Serverless::StateMachine":
            props["DefinitionUri"] = str(
                (empacotar.TEMPLATES[template].parent / props["DefinitionUri"]).resolve()
            )
    (build / "template.yaml").write_text(yaml.safe_dump(tpl, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return build


@pytest.mark.parametrize("nome", ["prm-hub", "prm-regional"])
def test_template_expandido_sem_transform_e_com_bucket_por_regiao(tmp_path, nome):
    saida = tmp_path / "out"
    saida.mkdir()
    cfn, artefatos = empacotar.preparar(nome, _build_falso(tmp_path, nome), "9.9.9", saida)

    assert "Transform" not in cfn
    assert "ArtefatosBucketPrefixo" in cfn["Parameters"]
    funcoes = [r for r in cfn["Resources"].values() if r["Type"] == "AWS::Lambda::Function"]
    assert funcoes
    for f in funcoes:
        assert f["Properties"]["Code"]["S3Bucket"] == {"Fn::Sub": "${ArtefatosBucketPrefixo}-${AWS::Region}"}
        assert f["Properties"]["Code"]["S3Key"].startswith("prm-tagging/9.9.9/codigo-")
        assert f["Properties"]["Layers"]
    camadas = [r for r in cfn["Resources"].values() if r["Type"] == "AWS::Lambda::LayerVersion"]
    assert len(camadas) == 1
    # Mesmo código em todas as funções -> 1 zip de código + 1 da layer.
    assert len({a["chave"] for a in artefatos}) == 2
    for sm in (r for r in cfn["Resources"].values() if r["Type"] == "AWS::StepFunctions::StateMachine"):
        assert "DefinitionString" in sm["Properties"] and "Definition" not in sm["Properties"]
    corpo = json.dumps(cfn, separators=(",", ":"))
    assert len(corpo.encode()) < 51200  # cabe no TemplateBody do StackSet
    assert "__PRM_" not in corpo

    cfnlint_api = pytest.importorskip("cfnlint.api")
    achados = [m for m in cfnlint_api.lint_all(corpo) if not m.rule.id.startswith("I")]
    assert achados == [], "\n".join(str(m) for m in achados)


def test_regional_preserva_substituicao_numerica_do_debounce(tmp_path):
    saida = tmp_path / "out"
    saida.mkdir()
    cfn, _ = empacotar.preparar("prm-regional", _build_falso(tmp_path, "prm-regional"), "1", saida)
    sm = next(r for r in cfn["Resources"].values() if r["Type"] == "AWS::StepFunctions::StateMachine")
    assert '"Seconds": ${DebounceSeconds}' in sm["Properties"]["DefinitionString"]
    assert "DebounceSeconds" in sm["Properties"]["DefinitionSubstitutions"]


def test_zip_deterministico(tmp_path):
    origem = tmp_path / "o"
    (origem / "a").mkdir(parents=True)
    (origem / "a" / "m.py").write_text("x = 1\n")
    (origem / "b.txt").write_text("b")
    s1 = empacotar.zip_deterministico(origem, tmp_path / "1.zip")
    (origem / "b.txt").touch()  # muda só o mtime
    s2 = empacotar.zip_deterministico(origem, tmp_path / "2.zip")
    assert s1 == s2


# ---------------------------------------------------------------------------
# stackset.py — publicar (moto)
# ---------------------------------------------------------------------------


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist" / "2.0.0"
    for chave, conteudo in (("prm-tagging/2.0.0/codigo-aaa.zip", b"codigo"), ("prm-tagging/2.0.0/sdk-layer-bbb.zip", b"sdk")):
        p = dist / "artefatos" / chave
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(conteudo)
    (dist / "prm-hub.template.json").write_text('{"Resources": {}}')
    (dist / "prm-regional.template.json").write_text('{"Resources": {}}')
    (dist / "manifest.json").write_text(
        json.dumps(
            {
                "versao": "2.0.0",
                "boto3_layer": "1.43.113",
                "templates": {
                    "prm-hub": {"arquivo": "prm-hub.template.json"},
                    "prm-regional": {"arquivo": "prm-regional.template.json"},
                },
                "artefatos": [
                    {"chave": "prm-tagging/2.0.0/codigo-aaa.zip", "sha256": "aaa"},
                    {"chave": "prm-tagging/2.0.0/sdk-layer-bbb.zip", "sha256": "bbb"},
                ],
            }
        )
    )
    return dist


def _args(**kw):
    import argparse

    base = dict(profile=None, call_as="SELF", sim=True, somente_mostrar=False, intervalo=0)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def _aws_env(monkeypatch):
    for k, v in dict(AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x", AWS_DEFAULT_REGION="us-east-1").items():
        monkeypatch.setenv(k, v)
    # O moto atende em HTTP e aplica a política: o Deny de não-TLS bloquearia
    # tudo. A política completa é verificada à parte (test_politica_bucket).
    original = stackset.politica_bucket
    monkeypatch.setattr(
        stackset, "politica_bucket",
        lambda *a, **k: {**original(*a, **k), "Statement": original(*a, **k)["Statement"][:1]},
    )


def test_politica_bucket():
    pol = stackset.politica_bucket("b", "o-abc1234567")
    leitura, tls = pol["Statement"]
    assert leitura["Action"] == "s3:GetObject" and leitura["Resource"] == "arn:aws:s3:::b/prm-tagging/*"
    assert leitura["Condition"] == {"StringEquals": {"aws:PrincipalOrgID": "o-abc1234567"}}
    assert tls["Effect"] == "Deny" and tls["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


def test_publicar_cria_bucket_seguro_por_regiao_e_envia(tmp_path, _aws_env):
    dist = _dist(tmp_path)
    with mock_aws():
        boto3.client("organizations").create_organization(FeatureSet="ALL")
        org_id = boto3.client("organizations").describe_organization()["Organization"]["Id"]
        sessao = boto3.Session()
        args = _args(dist=str(dist), regioes="us-east-1,sa-east-1", prefixo=None, org_id=None)
        assert stackset.cmd_publicar(args, session=sessao) == 0
        conta = boto3.client("sts").get_caller_identity()["Account"]
        for regiao in ("us-east-1", "sa-east-1"):
            s3 = boto3.client("s3", region_name=regiao)
            b = f"prm-tagging-{conta}-{regiao}"
            pol = json.loads(s3.get_bucket_policy(Bucket=b)["Policy"])
            assert pol["Statement"][0]["Condition"]["StringEquals"]["aws:PrincipalOrgID"] == org_id
            assert s3.get_public_access_block(Bucket=b)["PublicAccessBlockConfiguration"]["BlockPublicPolicy"]
            assert s3.get_bucket_versioning(Bucket=b)["Status"] == "Enabled"
            assert s3.get_bucket_encryption(Bucket=b)["ServerSideEncryptionConfiguration"]["Rules"]
            head = s3.head_object(Bucket=b, Key="prm-tagging/2.0.0/codigo-aaa.zip")
            assert head["Metadata"]["sha256"] == "aaa"
        # Republicar não reenvia o que não mudou.
        assert stackset.cmd_publicar(args, session=sessao) == 0
        versoes = boto3.client("s3").list_object_versions(Bucket=f"prm-tagging-{conta}-us-east-1")["Versions"]
        assert len(versoes) == 2
        pub = json.loads((dist / "publicado.json").read_text())
        assert pub["regioes"] == ["us-east-1", "sa-east-1"]


def test_publicar_somente_mostrar_nao_cria_nada(tmp_path, _aws_env, capsys):
    dist = _dist(tmp_path)
    with mock_aws():
        boto3.client("organizations").create_organization(FeatureSet="ALL")
        args = _args(dist=str(dist), regioes="us-east-1", prefixo=None, org_id=None, somente_mostrar=True)
        assert stackset.cmd_publicar(args, session=boto3.Session()) == 0
        assert boto3.client("s3").list_buckets()["Buckets"] == []
    saida = capsys.readouterr().out
    assert "O QUE CRIA" in saida and "CUSTO" in saida and "REMOVER" in saida


# ---------------------------------------------------------------------------
# stackset.py — implantar / remover (CloudFormation falso)
# ---------------------------------------------------------------------------


class _Pag:
    def __init__(self, fn):
        self.fn = fn

    def paginate(self, **kw):
        yield self.fn(**kw)


class _FakeCfn:
    def __init__(self, acesso="ENABLED"):
        self.acesso = acesso
        self.stacksets: dict[str, dict] = {}
        self.instancias: dict[str, set] = {}
        self.chamadas: list[tuple[str, dict]] = []

    def _op(self, nome, **kw):
        self.chamadas.append((nome, kw))
        return {"OperationId": f"op-{len(self.chamadas)}"}

    def describe_organizations_access(self, CallAs):
        return {"Status": self.acesso}

    def activate_organizations_access(self, CallAs):
        self.chamadas.append(("activate", {}))
        self.acesso = "ENABLED"

    def describe_stack_set(self, StackSetName, CallAs):
        from botocore.exceptions import ClientError

        if StackSetName not in self.stacksets:
            raise ClientError({"Error": {"Code": "StackSetNotFoundException", "Message": "x"}}, "DescribeStackSet")
        return {"StackSet": self.stacksets[StackSetName]}

    def create_stack_set(self, **kw):
        self.chamadas.append(("create_stack_set", kw))
        self.stacksets[kw["StackSetName"]] = {"Description": kw["Description"]}
        self.instancias[kw["StackSetName"]] = set()

    def update_stack_set(self, **kw):
        return self._op("update_stack_set", **kw)

    def create_stack_instances(self, **kw):
        for ou in kw["DeploymentTargets"]["OrganizationalUnitIds"]:
            for r in kw["Regions"]:
                self.instancias[kw["StackSetName"]].add((ou, r))
        return self._op("create_stack_instances", **kw)

    def update_stack_instances(self, **kw):
        return self._op("update_stack_instances", **kw)

    def delete_stack_instances(self, **kw):
        for ou in kw["DeploymentTargets"]["OrganizationalUnitIds"]:
            for r in kw["Regions"]:
                self.instancias[kw["StackSetName"]].discard((ou, r))
        return self._op("delete_stack_instances", **kw)

    def delete_stack_set(self, StackSetName, CallAs):
        self.chamadas.append(("delete_stack_set", {"StackSetName": StackSetName}))
        del self.stacksets[StackSetName]

    def describe_stack_set_operation(self, **kw):
        return {"StackSetOperation": {"Status": "SUCCEEDED"}}

    def get_paginator(self, nome):
        if nome == "list_stack_set_operation_results":
            return _Pag(lambda **kw: {"Summaries": [{"Account": "1", "Region": "us-east-1", "Status": "SUCCEEDED"}]})
        return _Pag(
            lambda StackSetName, CallAs: {
                "Summaries": [
                    {"OrganizationalUnitId": ou, "Region": r, "Account": "111111111111"}
                    for ou, r in sorted(self.instancias.get(StackSetName, set()))
                ]
            }
        )


class _Sessao:
    def __init__(self, cfn):
        self.cfn = cfn

    def client(self, nome, region_name=None):
        assert nome == "cloudformation"
        return self.cfn


def _args_implantar(dist, **kw):
    base = dict(
        dist=str(dist), ou="ou-abcd-12345678", valor="pc:contratoA", hub_regiao="us-east-1",
        regioes="us-east-1,sa-east-1", prefixo="prm-tagging-999", email=None, topico_central_arn=None,
        kms_central_arn=None, modo_inicial="dry-run", modo_varredura="dry-run", contrato_confirmado=False,
        ativar_acesso_confiavel=False, max_concorrencia_pct=25, tolerancia_falha_pct=100,
    )
    base.update(kw)
    return _args(**base)


def test_implantar_cria_stacksets_service_managed_com_override_por_ou(tmp_path):
    dist = _dist(tmp_path)
    cfn = _FakeCfn()
    assert stackset.cmd_implantar(_args_implantar(dist), session=_Sessao(cfn)) == 0
    criados = {kw["StackSetName"]: kw for n, kw in cfn.chamadas if n == "create_stack_set"}
    assert set(criados) == {"prm-hub", "prm-regional"}
    for kw in criados.values():
        assert kw["PermissionModel"] == "SERVICE_MANAGED"
        assert kw["AutoDeployment"] == {"Enabled": True, "RetainStacksOnAccountRemoval": False}
        assert kw["Capabilities"] == ["CAPABILITY_IAM"]
        params = {p["ParameterKey"]: p["ParameterValue"] for p in kw["Parameters"]}
        assert params["ArtefatosBucketPrefixo"] == "prm-tagging-999"
    inst = {kw["StackSetName"]: kw for n, kw in cfn.chamadas if n == "create_stack_instances"}
    assert inst["prm-hub"]["Regions"] == ["us-east-1"]
    assert inst["prm-regional"]["Regions"] == ["us-east-1", "sa-east-1"]
    assert inst["prm-regional"]["DeploymentTargets"] == {"OrganizationalUnitIds": ["ou-abcd-12345678"]}
    over = {p["ParameterKey"]: p["ParameterValue"] for p in inst["prm-regional"]["ParameterOverrides"]}
    assert over == {"ExpectedTagValue": "pc:contratoA"}
    assert inst["prm-regional"]["OperationPreferences"]["FailureTolerancePercentage"] == 100


def test_implantar_de_novo_atualiza_em_vez_de_criar(tmp_path):
    dist = _dist(tmp_path)
    cfn = _FakeCfn()
    stackset.cmd_implantar(_args_implantar(dist), session=_Sessao(cfn))
    cfn.chamadas.clear()
    stackset.cmd_implantar(_args_implantar(dist, valor="pc:contratoB"), session=_Sessao(cfn))
    nomes = [n for n, _ in cfn.chamadas]
    assert "create_stack_set" not in nomes and "create_stack_instances" not in nomes
    assert nomes.count("update_stack_set") == 2 and nomes.count("update_stack_instances") == 2


def test_implantar_sem_acesso_confiavel_para_antes_de_escrever(tmp_path):
    cfn = _FakeCfn(acesso="DISABLED")
    assert stackset.cmd_implantar(_args_implantar(_dist(tmp_path)), session=_Sessao(cfn)) == 1
    assert cfn.chamadas == []


@pytest.mark.parametrize(
    "kw, msg",
    [
        ({"valor": "contrato"}, "--valor"),
        ({"ou": "minha-ou"}, "--ou"),
        ({"hub_regiao": "eu-west-1"}, "--hub-regiao"),
        ({"topico_central_arn": "arn:aws:sns:us-east-1:111111111111:t"}, "--kms-central-arn"),
    ],
)
def test_implantar_valida_entrada(tmp_path, kw, msg):
    with pytest.raises(SystemExit) as e:
        stackset.cmd_implantar(_args_implantar(_dist(tmp_path), **kw), session=_Sessao(_FakeCfn()))
    assert msg in str(e.value)


def test_remover_apaga_instancias_e_stackset_vazio(tmp_path):
    dist = _dist(tmp_path)
    cfn = _FakeCfn()
    stackset.cmd_implantar(_args_implantar(dist), session=_Sessao(cfn))
    cfn.chamadas.clear()
    args = _args(dist=None, ou="ou-abcd-12345678", regioes="us-east-1,sa-east-1", hub_regiao="us-east-1",
                 prefixo=None, apagar_artefatos=False, somente_artefatos=False)
    assert stackset.cmd_remover(args, session=_Sessao(cfn)) == 0
    dels = [kw for n, kw in cfn.chamadas if n == "delete_stack_instances"]
    assert all(kw["RetainStacks"] is False for kw in dels)
    assert [kw["StackSetName"] for n, kw in cfn.chamadas if n == "delete_stack_set"] == ["prm-regional", "prm-hub"]


def test_remover_artefatos_apaga_todas_as_versoes(tmp_path, _aws_env):
    dist = _dist(tmp_path)
    with mock_aws():
        boto3.client("organizations").create_organization(FeatureSet="ALL")
        sessao = boto3.Session()
        stackset.cmd_publicar(_args(dist=str(dist), regioes="us-east-1", prefixo=None, org_id=None), session=sessao)
        args = _args(dist=str(dist), ou=None, regioes=None, hub_regiao=None, prefixo=None,
                     apagar_artefatos=False, somente_artefatos=True)
        assert stackset.cmd_remover(args, session=sessao) == 0
        assert boto3.client("s3").list_buckets()["Buckets"] == []
