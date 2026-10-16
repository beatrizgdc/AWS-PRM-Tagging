"""Etapa 4 — alerta de drift quase em tempo real. Offline (moto)."""
from __future__ import annotations

import json

import pytest

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")
moto_events = pytest.importorskip("moto.events.models")

from aws_prm_tagging import event_mapping, handler_drift  # noqa: E402

R, C, V = "us-east-1", "123456789012", "pc:abc123"


def _ct(source, name, req, erro=None):
    d = {"eventSource": source, "eventName": name, "awsRegion": R, "recipientAccountId": C, "requestParameters": req,
         "eventID": "e1", "eventTime": "2026-09-30T12:00:00Z", "userIdentity": {"arn": f"arn:aws:iam::{C}:user/fulano"}}
    if erro:
        d["errorCode"] = erro
    return {"id": "1", "account": C, "time": "2026-09-30T12:00:00Z", "region": R, "resources": [], "version": "0",
            "source": "aws." + source.split(".")[0], "detail-type": "AWS API Call via CloudTrail", "detail": d}


# ---------------------------- semântica do pattern ----------------------------

@pytest.mark.parametrize(
    "evento,esperado",
    [
        (_ct("lambda.amazonaws.com", "UntagResource20170331", {}), True),  # nome versionado
        (_ct("tagging.amazonaws.com", "UntagResources", {}), True),
        (_ct("rds.amazonaws.com", "RemoveTagsFromResource", {}), True),
        (_ct("ec2.amazonaws.com", "DeleteTags", {}), True),
        (_ct("s3.amazonaws.com", "PutBucketTagging", {}), True),
        (_ct("route53.amazonaws.com", "ChangeTagsForResource", {}), True),
        (_ct("ec2.amazonaws.com", "CreateTags", {}), False),
        (_ct("s3.amazonaws.com", "DeleteBucketTagging", {}, erro="AccessDenied"), False),
    ],
    ids=lambda v: v["detail"]["eventName"] + ("-erro" if v["detail"].get("errorCode") else "") if isinstance(v, dict) else None,
)
def test_pattern_de_drift(evento, esperado):
    p = moto_events.EventPattern.load(json.dumps(event_mapping.DRIFT_EVENT_PATTERN))
    assert p.matches_event(evento) is esperado


# ---------------------------- extração e relevância ----------------------------

def test_extrai_arns_dos_formatos_sem_arn():
    d = _ct("ec2.amazonaws.com", "DeleteTags", {"resourcesSet": {"items": [{"resourceId": "i-0abc"}, {"resourceId": "snap-1"}, {"resourceId": "sg-1"}]},
                                                "tagSet": {"items": [{"key": "aws-apn-id"}]}})["detail"]
    assert handler_drift.extrair_arns(d) == [f"arn:aws:ec2:{R}:{C}:instance/i-0abc", f"arn:aws:ec2:{R}::snapshot/snap-1"]
    assert handler_drift.extrair_arns(_ct("s3.amazonaws.com", "DeleteBucketTagging", {"bucketName": "b"})["detail"]) == ["arn:aws:s3:::b"]
    assert handler_drift.extrair_arns(_ct("sqs.amazonaws.com", "UntagQueue", {"queueUrl": f"https://sqs.{R}.amazonaws.com/{C}/fila"})["detail"]) == [f"arn:aws:sqs:{R}:{C}:fila"]


def test_relevancia():
    assert handler_drift.toca_a_tag_prm(_ct("lambda.amazonaws.com", "UntagResource20170331", {"resource": "arn:x", "tagKeys": ["AWS-APN-ID"]})["detail"])
    assert not handler_drift.toca_a_tag_prm(_ct("lambda.amazonaws.com", "UntagResource20170331", {"resource": "arn:x", "tagKeys": ["owner"]})["detail"])
    assert handler_drift.toca_a_tag_prm(_ct("s3.amazonaws.com", "PutBucketTagging", {"bucketName": "b"})["detail"])


# ---------------------------- handler contra AWS simulada ----------------------------

class _Sns:
    def __init__(self):
        self.msgs = []

    def publish(self, **kw):
        self.msgs.append(json.loads(kw["Message"]))
        return {}


@pytest.fixture
def aws(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"), ("AWS_DEFAULT_REGION", R),
                 ("EXPECTED_TAG_VALUE", V), ("SNS_TOPIC_ARN", f"arn:aws:sns:{R}:{C}:prm")):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with moto.mock_aws():
        yield boto3.Session(region_name=R)


def test_bucket_que_perdeu_a_tag_gera_alerta_com_autor(aws):
    s3 = aws.client("s3")
    s3.create_bucket(Bucket="perdeu")
    s3.put_bucket_tagging(Bucket="perdeu", Tagging={"TagSet": [{"Key": "outra", "Value": "x"}]})  # aws-apn-id sumiu
    sns = _Sns()
    out = handler_drift.handler(_ct("s3.amazonaws.com", "PutBucketTagging", {"bucketName": "perdeu"}), session=aws, sns_client=sns)
    assert out["alertas"] == 1
    msg = sns.msgs[0]
    assert msg["categoria_final"] == "drift_tag_removida" and msg["evento"]["principal"].endswith("user/fulano")
    assert msg["arn"] == "arn:aws:s3:::perdeu"


def test_bucket_que_continua_com_a_tag_nao_gera_alerta(aws):
    s3 = aws.client("s3")
    s3.create_bucket(Bucket="intacto")
    s3.put_bucket_tagging(Bucket="intacto", Tagging={"TagSet": [{"Key": "aws-apn-id", "Value": V}]})
    sns = _Sns()
    assert handler_drift.handler(_ct("s3.amazonaws.com", "PutBucketTagging", {"bucketName": "intacto"}), session=aws, sns_client=sns)["alertas"] == 0
    assert sns.msgs == []


def test_valor_trocado_por_outro_parceiro_e_drift_alterada(aws):
    s3 = aws.client("s3")
    s3.create_bucket(Bucket="trocado")
    s3.put_bucket_tagging(Bucket="trocado", Tagging={"TagSet": [{"Key": "aws-apn-id", "Value": "pc:outro"}]})
    sns = _Sns()
    handler_drift.handler(_ct("s3.amazonaws.com", "PutBucketTagging", {"bucketName": "trocado"}), session=aws, sns_client=sns)
    assert sns.msgs[0]["categoria_final"] == "drift_tag_alterada"


def test_untag_de_outra_chave_e_ignorado_sem_ler_nada(aws):
    sns = _Sns()
    ev = _ct("lambda.amazonaws.com", "UntagResource20170331", {"resource": f"arn:aws:lambda:{R}:{C}:function:f", "tagKeys": ["owner"]})
    assert handler_drift.handler(ev, session=object(), sns_client=sns)["alertas"] == 0
