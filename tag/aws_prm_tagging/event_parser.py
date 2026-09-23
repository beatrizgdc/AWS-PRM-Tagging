"""Extração do(s) recurso(s) recém-criado(s) a partir de um evento do
EventBridge (formato "AWS API Call via CloudTrail" — ver `event_mapping.py`
para como o event pattern que entrega esses eventos é construído).

Módulo puro: nenhuma chamada de rede — só leitura do payload do evento já
recebido pela Lambda. Quando o payload não é suficiente para montar um ARN
com confiança, a função devolve lista vazia — não há fallback de API aqui
(isso fica a cargo do chamador, ver `docs/melhorias-futuras.md`: por ora,
falha de extração é só logada, sem chamada de API adicional; a Etapa 4
continua sendo o backstop).

## Por que `parse_creation_event` devolve uma LISTA

Algumas APIs de criação são de lote — `ec2:RunInstances` pode criar várias
instâncias numa única chamada (`responseElements.instancesSet.items` é uma
lista), `workspaces:CreateWorkspaces` idem. Um único evento de CloudTrail
pode então corresponder a N recursos, não só 1 — o handler da Etapa 3 (ver
`handler_continuous_tagging.py`) itera sobre essa lista e processa cada
recurso independentemente (cada um com sua própria leitura de estado,
decisão e publicação de resultado).

## Dois níveis de confiança na extração

1. **Extractors dedicados** (`_REGISTRY` abaixo) — só para os serviços onde
   eu tenho confiança razoável na forma exata do `responseElements`/
   `requestParameters` (baseado em documentação pública das APIs, não em
   evento real capturado — ver ressalva em `event_mapping.py`).
2. **Fallback genérico best-effort** (`_extract_generic`) — para qualquer
   evento mapeado em `event_mapping.py` SEM extractor dedicado: procura
   recursivamente por uma chave terminando em "arn" (case-insensitive) cujo
   valor pareça um ARN (`arn:aws...`) dentro de `responseElements`. Cobre
   corretamente uma fração real das APIs mais novas da AWS (que tendem a
   devolver o ARN direto na resposta), mas é deliberadamente conservador:
   qualquer incerteza devolve `None`/lista vazia, nunca inventa um ARN.

Nenhum dos dois níveis foi validado contra uma conta AWS real ainda — ver
"Extractors da Etapa 3 ainda não validados em sandbox" em
[docs/melhorias-futuras.md](docs/melhorias-futuras.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .event_mapping import mapped_services
from .services import Service, load_services


@dataclass(frozen=True)
class ExtractedResource:
    arn: str
    servico: str
    regiao: str
    tipo_recurso: str | None = None


ArnBuilder = Callable[[dict, str, str], list[str]]


@dataclass(frozen=True)
class _SpecificExtractor:
    product_service_code: str
    arn_builder: ArnBuilder
    tipo_recurso: str | None = None


def _svc_name(services: list[Service], product_service_code: str) -> str | None:
    for svc in services:
        if svc.product_service_code == product_service_code:
            return svc.name
    return None


# ---------------------------------------------------------------------------
# Extractors dedicados — um por (eventSource, eventName) de alta confiança.
# ---------------------------------------------------------------------------


def _ext_ec2_run_instances(detail: dict, account_id: str, region: str) -> list[str]:
    items = (detail.get("responseElements") or {}).get("instancesSet", {}).get("items") or []
    return [
        f"arn:aws:ec2:{region}:{account_id}:instance/{item['instanceId']}"
        for item in items
        if item.get("instanceId")
    ]


def _ext_ec2_create_volume(detail: dict, account_id: str, region: str) -> list[str]:
    volume_id = (detail.get("responseElements") or {}).get("volumeId")
    return [f"arn:aws:ec2:{region}:{account_id}:volume/{volume_id}"] if volume_id else []


def _ext_ec2_create_vpc(detail: dict, account_id: str, region: str) -> list[str]:
    vpc_id = (detail.get("responseElements") or {}).get("vpc", {}).get("vpcId")
    return [f"arn:aws:ec2:{region}:{account_id}:vpc/{vpc_id}"] if vpc_id else []


def _ext_ec2_create_transit_gateway(detail: dict, account_id: str, region: str) -> list[str]:
    tgw_id = (detail.get("responseElements") or {}).get("transitGateway", {}).get("transitGatewayId")
    return [f"arn:aws:ec2:{region}:{account_id}:transit-gateway/{tgw_id}"] if tgw_id else []


def _ext_s3_create_bucket(detail: dict, account_id: str, region: str) -> list[str]:
    bucket = (detail.get("requestParameters") or {}).get("bucketName")
    return [f"arn:aws:s3:::{bucket}"] if bucket else []


def _ext_lambda_create_function(detail: dict, account_id: str, region: str) -> list[str]:
    arn = (detail.get("responseElements") or {}).get("functionArn")
    return [arn] if arn else []


def _ext_dynamodb_create_table(detail: dict, account_id: str, region: str) -> list[str]:
    resp = detail.get("responseElements") or {}
    arn = resp.get("tableDescription", {}).get("tableArn")
    if arn:
        return [arn]
    table_name = (detail.get("requestParameters") or {}).get("tableName")
    return [f"arn:aws:dynamodb:{region}:{account_id}:table/{table_name}"] if table_name else []


def _ext_rds_create_db_instance(detail: dict, account_id: str, region: str) -> list[str]:
    resp = detail.get("responseElements") or {}
    arn = resp.get("dBInstanceArn")
    if arn:
        return [arn]
    identifier = (detail.get("requestParameters") or {}).get("dBInstanceIdentifier")
    return [f"arn:aws:rds:{region}:{account_id}:db:{identifier}"] if identifier else []


def _ext_rds_create_db_cluster(detail: dict, account_id: str, region: str) -> list[str]:
    # Ver docstring de event_mapping.py: este evento também é disparado por
    # criações de Aurora/DocumentDB/Neptune (mesmo namespace "rds") — o
    # recurso resultante é sempre rotulado "Amazon RDS" (mesma limitação já
    # documentada para o passo genérico da Etapa 1 em services.py).
    resp = detail.get("responseElements") or {}
    arn = resp.get("dBClusterArn")
    if arn:
        return [arn]
    identifier = (detail.get("requestParameters") or {}).get("dBClusterIdentifier")
    return [f"arn:aws:rds:{region}:{account_id}:cluster:{identifier}"] if identifier else []


def _ext_eks_create_cluster(detail: dict, account_id: str, region: str) -> list[str]:
    arn = (detail.get("responseElements") or {}).get("cluster", {}).get("arn")
    return [arn] if arn else []


def _ext_eks_create_nodegroup(detail: dict, account_id: str, region: str) -> list[str]:
    arn = (detail.get("responseElements") or {}).get("nodegroup", {}).get("nodegroupArn")
    return [arn] if arn else []


def _ext_bedrock_create_inference_profile(detail: dict, account_id: str, region: str) -> list[str]:
    # bedrock:CreateInferenceProfile só cria profiles do tipo APPLICATION —
    # profiles de sistema (cross-region) são pré-existentes, não criados
    # pelo cliente via esta API. Por isso não é preciso checar o tipo aqui
    # (ao contrário de resource_discovery.discover_bedrock_resources, que
    # lista TODOS os profiles e precisa filtrar).
    arn = (detail.get("responseElements") or {}).get("inferenceProfileArn")
    return [arn] if arn else []


def _ext_sns_create_topic(detail: dict, account_id: str, region: str) -> list[str]:
    arn = (detail.get("responseElements") or {}).get("topicArn")
    return [arn] if arn else []


def _ext_sqs_create_queue(detail: dict, account_id: str, region: str) -> list[str]:
    # CreateQueue só devolve QueueUrl na resposta, nunca o ARN — construído
    # a partir do nome pedido na requisição (sempre presente).
    queue_name = (detail.get("requestParameters") or {}).get("queueName")
    return [f"arn:aws:sqs:{region}:{account_id}:{queue_name}"] if queue_name else []


def _ext_ecr_create_repository(detail: dict, account_id: str, region: str) -> list[str]:
    arn = (detail.get("responseElements") or {}).get("repository", {}).get("repositoryArn")
    return [arn] if arn else []


def _ext_ecs_create_cluster(detail: dict, account_id: str, region: str) -> list[str]:
    arn = (detail.get("responseElements") or {}).get("cluster", {}).get("clusterArn")
    return [arn] if arn else []


def _ext_ecs_create_service(detail: dict, account_id: str, region: str) -> list[str]:
    arn = (detail.get("responseElements") or {}).get("service", {}).get("serviceArn")
    return [arn] if arn else []


def _ext_efs_create_file_system(detail: dict, account_id: str, region: str) -> list[str]:
    resp = detail.get("responseElements") or {}
    arn = resp.get("fileSystemArn")
    if arn:
        return [arn]
    fs_id = resp.get("fileSystemId")
    return [f"arn:aws:elasticfilesystem:{region}:{account_id}:file-system/{fs_id}"] if fs_id else []


def _ext_elasticache_create_cache_cluster(detail: dict, account_id: str, region: str) -> list[str]:
    # API clássica do ElastiCache não devolve ARN na resposta — construído
    # a partir do identificador pedido na requisição.
    cluster_id = (detail.get("requestParameters") or {}).get("cacheClusterId")
    return [f"arn:aws:elasticache:{region}:{account_id}:cluster:{cluster_id}"] if cluster_id else []


def _ext_kms_create_key(detail: dict, account_id: str, region: str) -> list[str]:
    arn = (detail.get("responseElements") or {}).get("keyMetadata", {}).get("arn")
    return [arn] if arn else []


def _ext_cloudfront_create_distribution(detail: dict, account_id: str, region: str) -> list[str]:
    # ARNs de CloudFront não têm região — construído a partir do Id, mais
    # confiável do que confiar no campo (pouco documentado) de ARN direto
    # na resposta.
    dist_id = (detail.get("responseElements") or {}).get("distribution", {}).get("id")
    return [f"arn:aws:cloudfront::{account_id}:distribution/{dist_id}"] if dist_id else []


def _ext_route53_create_hosted_zone(detail: dict, account_id: str, region: str) -> list[str]:
    # ARNs de Route 53 não têm região nem conta no formato usual — o "Id"
    # devolvido às vezes já vem prefixado com "/hostedzone/".
    zone_id = (detail.get("responseElements") or {}).get("hostedZone", {}).get("id")
    if not zone_id:
        return []
    zone_id = zone_id.rsplit("/", 1)[-1]
    return [f"arn:aws:route53:::hostedzone/{zone_id}"]


def _ext_secretsmanager_create_secret(detail: dict, account_id: str, region: str) -> list[str]:
    resp = detail.get("responseElements") or {}
    arn = resp.get("aRN") or resp.get("ARN")
    return [arn] if arn else []


def _ext_states_create_state_machine(detail: dict, account_id: str, region: str) -> list[str]:
    arn = (detail.get("responseElements") or {}).get("stateMachineArn")
    return [arn] if arn else []


def _ext_elb_create_load_balancer(detail: dict, account_id: str, region: str) -> list[str]:
    lbs = (detail.get("responseElements") or {}).get("loadBalancers") or []
    return [lbs[0]["loadBalancerArn"]] if lbs and lbs[0].get("loadBalancerArn") else []


_REGISTRY: dict[tuple[str, str], _SpecificExtractor] = {
    ("ec2.amazonaws.com", "RunInstances"): _SpecificExtractor("AmazonEC2", _ext_ec2_run_instances),
    ("ec2.amazonaws.com", "CreateVolume"): _SpecificExtractor("AmazonEC2", _ext_ec2_create_volume),
    ("ec2.amazonaws.com", "CreateVpc"): _SpecificExtractor("AmazonVPC", _ext_ec2_create_vpc),
    ("ec2.amazonaws.com", "CreateTransitGateway"): _SpecificExtractor("AmazonVPC", _ext_ec2_create_transit_gateway),
    ("s3.amazonaws.com", "CreateBucket"): _SpecificExtractor("AmazonS3", _ext_s3_create_bucket),
    ("lambda.amazonaws.com", "CreateFunction20150331v2"): _SpecificExtractor(
        "AWSLambda", _ext_lambda_create_function
    ),
    ("dynamodb.amazonaws.com", "CreateTable"): _SpecificExtractor("AmazonDynamoDB", _ext_dynamodb_create_table),
    ("rds.amazonaws.com", "CreateDBInstance"): _SpecificExtractor("AmazonRDS", _ext_rds_create_db_instance),
    ("rds.amazonaws.com", "CreateDBCluster"): _SpecificExtractor("AmazonRDS", _ext_rds_create_db_cluster),
    ("eks.amazonaws.com", "CreateCluster"): _SpecificExtractor(
        "AmazonEKS", _ext_eks_create_cluster, tipo_recurso="cluster"
    ),
    ("eks.amazonaws.com", "CreateNodegroup"): _SpecificExtractor(
        "AmazonEKS", _ext_eks_create_nodegroup, tipo_recurso="node_group"
    ),
    ("bedrock.amazonaws.com", "CreateInferenceProfile"): _SpecificExtractor(
        "AmazonBedrock", _ext_bedrock_create_inference_profile, tipo_recurso="application_inference_profile"
    ),
    ("sns.amazonaws.com", "CreateTopic"): _SpecificExtractor("AmazonSNS", _ext_sns_create_topic),
    ("sqs.amazonaws.com", "CreateQueue"): _SpecificExtractor("AWSQueueService", _ext_sqs_create_queue),
    ("ecr.amazonaws.com", "CreateRepository"): _SpecificExtractor("AmazonECR", _ext_ecr_create_repository),
    ("ecs.amazonaws.com", "CreateCluster"): _SpecificExtractor("AmazonECS", _ext_ecs_create_cluster),
    ("ecs.amazonaws.com", "CreateService"): _SpecificExtractor("AmazonECS", _ext_ecs_create_service),
    ("elasticfilesystem.amazonaws.com", "CreateFileSystem"): _SpecificExtractor(
        "AmazonEFS", _ext_efs_create_file_system
    ),
    ("elasticache.amazonaws.com", "CreateCacheCluster"): _SpecificExtractor(
        "AmazonElastiCache", _ext_elasticache_create_cache_cluster
    ),
    ("kms.amazonaws.com", "CreateKey"): _SpecificExtractor("awskms", _ext_kms_create_key),
    ("cloudfront.amazonaws.com", "CreateDistribution"): _SpecificExtractor(
        "AmazonCloudFront", _ext_cloudfront_create_distribution
    ),
    ("route53.amazonaws.com", "CreateHostedZone"): _SpecificExtractor(
        "AmazonRoute53", _ext_route53_create_hosted_zone
    ),
    ("secretsmanager.amazonaws.com", "CreateSecret"): _SpecificExtractor(
        "AWSSecretsManager", _ext_secretsmanager_create_secret
    ),
    ("states.amazonaws.com", "CreateStateMachine"): _SpecificExtractor(
        "AmazonStates", _ext_states_create_state_machine
    ),
    ("elasticloadbalancing.amazonaws.com", "CreateLoadBalancer"): _SpecificExtractor(
        "AWSELB", _ext_elb_create_load_balancer
    ),
}


# ---------------------------------------------------------------------------
# Fallback genérico best-effort — para eventos mapeados sem extractor dedicado.
# ---------------------------------------------------------------------------


def _find_arn_recursive(obj, depth: int = 0) -> str | None:
    if depth > 4 or obj is None:
        return None
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower().endswith("arn") and isinstance(value, str) and value.startswith("arn:aws"):
                return value
        for value in obj.values():
            found = _find_arn_recursive(value, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_arn_recursive(item, depth + 1)
            if found:
                return found
    return None


def _extract_generic(detail: dict) -> list[str]:
    arn = _find_arn_recursive(detail.get("responseElements"))
    return [arn] if arn else []


def _build_reverse_index() -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    for code, rules in mapped_services().items():
        for rule in rules:
            index[(rule.event_source, rule.event_name)] = code
    return index


_MAPPED_EVENTS = _build_reverse_index()


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------


def parse_creation_event(event: dict, services: list[Service] | None = None) -> list[ExtractedResource]:
    """Extrai 0, 1 ou N `ExtractedResource` de um evento do EventBridge no
    formato "AWS API Call via CloudTrail". Devolve lista vazia sempre que
    não há confiança suficiente para montar um ARN — nunca um valor
    parcial/adivinhado."""
    services = services if services is not None else load_services()
    detail = event.get("detail") or {}
    event_source = detail.get("eventSource")
    event_name = detail.get("eventName")
    if not event_source or not event_name:
        return []

    account_id = detail.get("recipientAccountId") or event.get("account")
    region = detail.get("awsRegion") or event.get("region")
    if not account_id or not region:
        return []

    key = (event_source, event_name)
    spec = _REGISTRY.get(key)

    if spec is not None:
        arns = spec.arn_builder(detail, account_id, region)
        servico = _svc_name(services, spec.product_service_code)
        tipo_recurso = spec.tipo_recurso
    else:
        product_service_code = _MAPPED_EVENTS.get(key)
        if product_service_code is None:
            return []  # evento não está em event_mapping.py — nunca deveria acontecer se o pattern do EventBridge está correto, mas defensivo
        arns = _extract_generic(detail)
        servico = _svc_name(services, product_service_code)
        tipo_recurso = None

    if not arns or servico is None:
        return []

    return [ExtractedResource(arn=arn, servico=servico, regiao=region, tipo_recurso=tipo_recurso) for arn in arns]
