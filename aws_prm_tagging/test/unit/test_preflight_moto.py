"""Preflight (achado A3 + passo 2 do item 7) contra AWS simulada (moto)."""
from __future__ import annotations

import pytest

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")

from aws_prm_tagging import preflight  # noqa: E402

R = "us-east-1"


@pytest.fixture
def aws(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"), ("AWS_DEFAULT_REGION", R)):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(preflight, "get_active_regions", lambda s, b: ["sa-east-1", "us-east-2"])
    with moto.mock_aws():
        yield boto3.Session(region_name=R)


def _trail(s, multi=True, logging=True, somente_leitura=False):
    s.client("s3").create_bucket(Bucket="logs-trail")
    ct = s.client("cloudtrail", region_name=R)
    ct.create_trail(Name="trail-prm", S3BucketName="logs-trail", IsMultiRegionTrail=multi)
    if somente_leitura:
        ct.put_event_selectors(TrailName="trail-prm", EventSelectors=[{"ReadWriteType": "ReadOnly", "IncludeManagementEvents": True}])
    if logging:
        ct.start_logging(Name="trail-prm")


def test_us_east_1_sempre_entra_no_stackset(aws):
    r = preflight.run_preflight(aws)
    assert r["regioes_stackset"] == ["sa-east-1", "us-east-1", "us-east-2"]


def test_sem_trail_nao_esta_pronto(aws):
    r = preflight.run_preflight(aws)
    assert not r["pronto_para_deploy"]
    assert set(r["cloudtrail_por_regiao"].values()) == {"sem_trail"}
    assert r["recomendacoes"]


def test_trail_multi_regiao_ativa_cobre_tudo(aws):
    _trail(aws)
    r = preflight.run_preflight(aws)
    assert r["pronto_para_deploy"]
    assert set(r["cloudtrail_por_regiao"].values()) == {"ok"}


def test_trail_parada_nao_conta(aws):
    _trail(aws, logging=False)
    assert not preflight.run_preflight(aws)["pronto_para_deploy"]


def test_trail_so_de_leitura_nao_conta(aws):
    _trail(aws, somente_leitura=True)
    assert not preflight.run_preflight(aws)["pronto_para_deploy"]


def test_trail_de_uma_regiao_so_cobre_a_propria(aws):
    _trail(aws, multi=False)
    por_regiao = preflight.run_preflight(aws)["cloudtrail_por_regiao"]
    assert por_regiao["us-east-1"] == "ok"
    assert por_regiao["sa-east-1"] == "sem_trail"
