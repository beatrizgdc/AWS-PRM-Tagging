"""Regressões dos achados da validação na sandbox real (2026-10-02, e-mails
da varredura recorrente): S3 na Lambda, revalidação de EKS no hub e regiões
bloqueadas por SCP. Offline."""
from __future__ import annotations

from pathlib import Path

import yaml
from botocore.exceptions import ClientError

from aws_prm_tagging import handler_initial_tagging, native_discovery, pipeline
from aws_prm_tagging.services import load_services

V = "pc:abc123"


# ------------------------------ S3 sem BucketRegion ------------------------------

class _S3SemBucketRegion:
    """Imita o boto3 antigo do runtime do Lambda: ListBuckets sem BucketRegion."""

    def __init__(self, locais: dict, negados=()):
        self.locais, self.negados = locais, set(negados)

    def can_paginate(self, op):
        return False

    def list_buckets(self):
        return {"Buckets": [{"Name": n} for n in self.locais]}

    def get_bucket_location(self, Bucket):
        if Bucket in self.negados:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "negado"}}, "GetBucketLocation")
        return {"LocationConstraint": self.locais[Bucket]}


class _Sessao:
    def __init__(self, client):
        self.c = client

    def client(self, *a, **k):
        return self.c


def test_s3_um_bucket_negado_nao_derruba_os_outros():
    s3 = _S3SemBucketRegion({"bloqueado": "us-east-2", "a": "us-east-2", "b": None, "c": "EU"}, negados={"bloqueado"})
    recursos, falhas = native_discovery.discover_s3_buckets_por_regiao(
        _Sessao(s3), {"us-east-2", "us-east-1", "eu-west-1"}, "1", load_services(), V
    )
    por_arn = {r["arn"]: r["regiao"] for r in recursos}
    assert por_arn == {"arn:aws:s3:::a": "us-east-2", "arn:aws:s3:::b": "us-east-1", "arn:aws:s3:::c": "eu-west-1"}
    assert len(falhas) == 1 and "bloqueado" in falhas[0]["erro"] and "1 bucket(s)" in falhas[0]["erro"]


def test_s3_cada_regiao_so_os_proprios_buckets():
    s3 = _S3SemBucketRegion({"leste1": None, "leste2": "us-east-2"})
    recursos, _ = native_discovery.discover_s3_buckets_por_regiao(_Sessao(s3), {"us-east-1"}, "1", load_services(), V)
    assert [r["arn"] for r in recursos] == ["arn:aws:s3:::leste1"]


def test_s3_permissao_get_bucket_location_declarada():
    assert "s3:GetBucketLocation" in native_discovery.required_iam_actions()


# ------------------------------ região bloqueada por SCP ------------------------------

_ERRO_SCP = ("get_resources (página 1): AccessDeniedException: User: x is not authorized to perform: "
             "tag:GetResources with an explicit deny in a service control policy: arn:aws:organizations::1:policy/p")


def test_regiao_negada_por_scp_vira_uma_entrada_e_nada_mais_roda(monkeypatch):
    chamadas = []
    rd = pipeline.resource_discovery
    monkeypatch.setattr(rd, "discover_generic_resources", lambda s, r, sl, v: ([], [{"regiao": r, "etapa": "generico", "erro": _ERRO_SCP}]))
    monkeypatch.setattr(rd, "discover_bedrock_resources", lambda *a: chamadas.append("bedrock") or ([], []))
    monkeypatch.setattr(rd, "discover_eks_resources", lambda *a: chamadas.append("eks") or ([], []))
    monkeypatch.setattr(native_discovery, "discover_native_resources", lambda *a, **k: chamadas.append("nativo") or ([], []))
    monkeypatch.setattr(native_discovery, "discover_s3_buckets_por_regiao", lambda *a, **k: chamadas.append("s3") or ([], []))
    recursos, falhas = pipeline.descobrir_regioes(object(), "1", ["ap-south-1"], load_services(), V)
    assert chamadas == [], "nada além do GetResources deve rodar numa região negada por SCP"
    assert [f["etapa"] for f in falhas] == ["regiao_negada_por_scp"]


def test_access_denied_que_nao_e_scp_segue_o_fluxo_normal(monkeypatch):
    rd = pipeline.resource_discovery
    monkeypatch.setattr(rd, "discover_generic_resources", lambda s, r, sl, v: ([], [{"regiao": r, "etapa": "generico", "erro": "AccessDeniedException: no identity-based policy allows"}]))
    monkeypatch.setattr(rd, "discover_bedrock_resources", lambda *a: ([], []))
    monkeypatch.setattr(rd, "discover_eks_resources", lambda *a: ([], []))
    monkeypatch.setattr(native_discovery, "discover_s3_buckets_por_regiao", lambda *a, **k: ([], []))
    _, falhas = pipeline.descobrir_regioes(object(), "1", ["us-east-2"], load_services(), V)
    assert "regiao_negada_por_scp" not in [f["etapa"] for f in falhas]


def test_regiao_bloqueada_por_scp_nao_e_novidade():
    base = {"por_categoria": {}, "total_falhas_descoberta": 1, "dlq_mensagens": None,
            "stack_regional_detectada": False, "contrato_confirmado": True}
    assert handler_initial_tagging._tem_novidade({**base, "regiao_bloqueada_por_scp": True}) is False
    assert handler_initial_tagging._tem_novidade({**base, "regiao_bloqueada_por_scp": False}) is True


# ------------------------------ hub relê como a Etapa 3 ------------------------------

class _L(yaml.SafeLoader):
    pass


_L.add_multi_constructor("!", lambda loader, tag, node: None)


def _acoes(nome, role):
    t = yaml.load((Path(__file__).parent.parent.parent / "infra" / nome).read_text(encoding="utf-8"), Loader=_L)
    return {
        p["PolicyName"]: set(a if isinstance(a, list) else [a])
        for p in t["Resources"][role]["Properties"]["Policies"]
        for a in [p["PolicyDocument"]["Statement"][0]["Action"]]
    }


def test_hub_tem_as_mesmas_leituras_de_revalidacao_da_etapa3():
    """eks:ListTagsForResource faltava no hub: 2 recursos EKS 'falhou' na
    varredura da sandbox."""
    regional = _acoes("template-regional.yaml", "Etapa3ExecutionRole")["PrmEtapa3RevalidationRead"]
    hub = set().union(*_acoes("template-hub.yaml", "TagueamentoInicialRole").values())
    assert regional <= hub, f"hub sem: {sorted(regional - hub)}"
