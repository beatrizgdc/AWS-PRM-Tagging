"""Etapa 4 — alerta de drift/remoção da tag quase em tempo real.

Uma regra do EventBridge (stack regional) entrega qualquer chamada de
REMOÇÃO/SUBSTITUIÇÃO de tags gravada pelo CloudTrail (`Untag*`,
`RemoveTags*`, `DeleteTags`, `PutBucketTagging`/`DeleteBucketTagging`,
`ChangeTagsForResource`, `UpdateTagsForResource`) — pattern em
`event_mapping.DRIFT_EVENT_PATTERN`. Este handler:

1. descarta eventos que não tocam a tag `aws-apn-id` (a chave não aparece
   na requisição e a operação não é do tipo "substitui todas as tags");
2. extrai o(s) ARN(s) afetado(s) — ARN explícito na requisição, ou
   montado a partir de nome/ID nos serviços que não usam ARN (S3, EC2,
   SQS, Logs, Kinesis, ELB clássico, Route 53);
3. RELÊ o estado atual de cada recurso (`single_resource`) e só alerta se
   a tag de fato não está mais correta — o evento diz o que alguém pediu,
   a releitura diz o que ficou.

Só alerta; não reaplica a tag. Reaplicar fica com a varredura recorrente
em modo `live` (stack hub), que é o backstop — reagir aqui na hora
brigaria com uma remoção intencional (ex.: alguém corrigindo o contrato)
sem revisão humana.
"""
from __future__ import annotations

import json
import logging
import os

import boto3

from . import escopo_teste, publish, single_resource
from .services import classify_arn, load_services
from .tag_status import STATUS_OK, TAG_KEY

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ETAPA = "4-drift"

# Operações que substituem/apagam TODAS as tags do recurso — a chave
# removida não aparece na requisição.
_SUBSTITUI_TUDO = frozenset({"PutBucketTagging", "DeleteBucketTagging"})

_PREFIXO_EC2 = {
    "i-": "instance",
    "vol-": "volume",
    "snap-": "snapshot",
    "eipalloc-": "elastic-ip",
    "nat-": "natgateway",
    "vpce-": "vpc-endpoint",
    "tgw-": "transit-gateway",
    "vpn-": "vpn-connection",
}


def _strings(obj, depth=0):
    if depth > 8 or obj is None:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v, depth + 1)


def _valores_da_chave(obj, chave: str, depth=0):
    if depth > 8 or obj is None:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() == chave.lower():
                if isinstance(v, str):
                    yield v
                elif isinstance(v, list):
                    yield from (x for x in v if isinstance(x, str))
            else:
                yield from _valores_da_chave(v, chave, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _valores_da_chave(v, chave, depth + 1)


def extrair_arns(detail: dict) -> list[str]:
    req = detail.get("requestParameters") or {}
    regiao = detail.get("awsRegion") or ""
    conta = detail.get("recipientAccountId") or ""
    arns: list[str] = []

    def add(a: str):
        if a and a not in arns:
            arns.append(a)

    for s in _strings(req):
        if s.startswith("arn:"):
            add(s)
    for b in _valores_da_chave(req, "bucketName"):
        add(f"arn:aws:s3:::{b}")
    for rid in _valores_da_chave(req, "resourceId"):
        for prefixo, tipo in _PREFIXO_EC2.items():
            if rid.startswith(prefixo):
                add(f"arn:aws:ec2:{regiao}::{tipo}/{rid}" if tipo == "snapshot" else f"arn:aws:ec2:{regiao}:{conta}:{tipo}/{rid}")
        if detail.get("eventSource") == "route53.amazonaws.com":
            add(f"arn:aws:route53:::hostedzone/{rid.rsplit('/', 1)[-1]}")
    for url in _valores_da_chave(req, "queueUrl"):
        add(f"arn:aws:sqs:{regiao}:{conta}:{url.rstrip('/').rsplit('/', 1)[-1]}")
    for nome in _valores_da_chave(req, "logGroupName"):
        add(f"arn:aws:logs:{regiao}:{conta}:log-group:{nome}")
    for nome in _valores_da_chave(req, "streamName"):
        add(f"arn:aws:kinesis:{regiao}:{conta}:stream/{nome}")
    for nome in _valores_da_chave(req, "loadBalancerNames"):
        add(f"arn:aws:elasticloadbalancing:{regiao}:{conta}:loadbalancer/{nome}")
    return arns


def toca_a_tag_prm(detail: dict) -> bool:
    if detail.get("eventName") in _SUBSTITUI_TUDO:
        return True
    texto = json.dumps(detail.get("requestParameters") or {}).lower()
    return TAG_KEY in texto


_CATEGORIA_POR_STATUS = {
    "sem_tag": "drift_tag_removida",
    "conflito": "drift_tag_alterada",
}


def handler(event: dict, context=None, *, session: boto3.Session | None = None, sns_client=None) -> dict:
    detail = event.get("detail") or {}
    if not toca_a_tag_prm(detail):
        return {"alertas": 0, "motivo": "evento não toca aws-apn-id"}

    valor = os.environ["EXPECTED_TAG_VALUE"]
    topic_arn = os.environ["SNS_TOPIC_ARN"]
    session = session or boto3.Session()
    services = load_services()
    sns = sns_client or session.client("sns", region_name=topic_arn.split(":")[3])
    regiao_evento = detail.get("awsRegion") or event.get("region")

    alertas = 0
    for arn in extrair_arns(detail):
        if not escopo_teste.dentro_do_escopo(event, arn):
            continue  # FiltroEscopoTeste (sandbox compartilhada)
        svc = classify_arn(arn, services)
        if svc is None:
            continue  # fora do CSV
        regiao = arn.split(":")[3] or regiao_evento
        estado = single_resource.build_resource_from_arn(session, arn, svc.name, regiao, valor)
        if estado["status_tag"] == STATUS_OK:
            logger.info("Evento %s em %s, mas a tag continua correta — sem alerta", detail.get("eventName"), arn)
            continue
        categoria = _CATEGORIA_POR_STATUS.get(estado["status_tag"], f"drift_{estado['status_tag']}")
        publish.publish_outcome(
            sns,
            topic_arn,
            {
                "etapa": ETAPA,
                "categoria_final": categoria,
                "conta_id": detail.get("recipientAccountId") or event.get("account"),
                "arn": arn,
                "servico": svc.name,
                "regiao": regiao,
                "estado_atual": {
                    "status_tag": estado["status_tag"],
                    "valor_tag_encontrado": estado["valor_tag_encontrado"],
                    "iac": estado["iac"],
                },
                "evento": {
                    "event_id": detail.get("eventID"),
                    "event_name": detail.get("eventName"),
                    "event_source": detail.get("eventSource"),
                    "quando": detail.get("eventTime"),
                    "principal": (detail.get("userIdentity") or {}).get("arn"),
                },
                "acao": "nenhuma automática — a varredura recorrente em modo live reaplica a tag; "
                "em dry-run, reaplicar manualmente ou confirmar se a remoção foi intencional",
            },
        )
        alertas += 1
    return {"alertas": alertas}
