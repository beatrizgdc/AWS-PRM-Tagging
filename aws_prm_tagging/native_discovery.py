"""Descoberta nativa (por serviço): recursos que nunca receberam tag.

`GetResources` (passo genérico) só devolve recursos que têm ou já tiveram
alguma tag. Este módulo lista os recursos na API de cada serviço
(`Describe*`/`List*`) e devolve só os ARNs que o passo genérico não viu.
Somente leitura; o Resource Explorer foi descartado porque exige criar um
índice na conta.

Cada serviço é uma entrada declarativa em `_ENUMERADORES` (client, operação,
onde está a lista, como montar o ARN e, se houver, como ler as tags). O ARN
passa por `services.classify_arn`, o mesmo classificador do passo genérico:
serviço fora do CSV é descartado e o rótulo `servico` é o mesmo.

Sem tags na listagem, o recurso entra com `tags={}` (se tivesse tag, o
`GetResources` o teria devolvido). A revalidação da Etapa 2c relê o estado
antes de qualquer escrita.

A cobertura prioriza gasto típico (compute, storage, bancos, rede paga,
mensageria). `required_iam_actions()` lista as permissões de leitura e
`servicos_cobertos()` os serviços com enumerador.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

from .iac_detection import detect_iac
from .services import Service, classify_arn
from .tag_status import find_similar_tag_keys, get_tag_status, tags_list_to_dict

logger = logging.getLogger(__name__)

# Retry nativo do botocore (modo adaptativo) — mais simples que decorar cada
# página: a paginação do boto3 faz várias chamadas por dentro.
_CLIENT_CONFIG = Config(retries={"mode": "adaptive", "max_attempts": 10})

REGIAO_SERVICOS_GLOBAIS = "us-east-1"

Item = dict
ArnFn = Callable[[Item, str, str], "str | None"]


@dataclass(frozen=True)
class _Enumerador:
    nome: str
    client: str
    operacao: str
    itens: Callable[[dict], list]  # resposta (uma página) -> itens
    arn: ArnFn  # (item, regiao, conta) -> ARN
    iam: tuple[str, ...]
    tags: Callable[[Item], "dict | None"] | None = None
    kwargs: dict = field(default_factory=dict)
    filtro: Callable[[Item], bool] | None = None
    global_: bool = False  # roda só em us-east-1
    regiao_item: Callable[[Item], "str | None"] | None = None  # região real do item (S3)


def _chave(nome: str) -> Callable[[dict], list]:
    return lambda resp: resp.get(nome) or []


def _tags_lista(campo: str = "Tags") -> Callable[[Item], dict]:
    return lambda item: tags_list_to_dict(item.get(campo) or [])


def _tags_dict(campo: str = "Tags") -> Callable[[Item], dict]:
    return lambda item: dict(item.get(campo) or {})


def _campo(nome: str) -> ArnFn:
    return lambda item, r, a: item.get(nome)


def _instancias(resp: dict) -> list:
    return [i for res in resp.get("Reservations", []) for i in res.get("Instances", [])]


_ESTADOS_INSTANCIA_IGNORADOS = {"terminated", "shutting-down"}


_ENUMERADORES: tuple[_Enumerador, ...] = (
    # ------------------------------ EC2 / EBS / rede paga ------------------------------
    _Enumerador(
        "ec2_instancias", "ec2", "describe_instances", _instancias,
        lambda i, r, a: f"arn:aws:ec2:{r}:{a}:instance/{i['InstanceId']}",
        ("ec2:DescribeInstances",), tags=_tags_lista(),
        filtro=lambda i: (i.get("State") or {}).get("Name") not in _ESTADOS_INSTANCIA_IGNORADOS,
    ),
    _Enumerador(
        "ec2_volumes", "ec2", "describe_volumes", _chave("Volumes"),
        lambda i, r, a: f"arn:aws:ec2:{r}:{a}:volume/{i['VolumeId']}",
        ("ec2:DescribeVolumes",), tags=_tags_lista(),
    ),
    _Enumerador(
        # ARN de snapshot EBS não tem conta (formato da AWS).
        "ec2_snapshots", "ec2", "describe_snapshots", _chave("Snapshots"),
        lambda i, r, a: f"arn:aws:ec2:{r}::snapshot/{i['SnapshotId']}",
        ("ec2:DescribeSnapshots",), tags=_tags_lista(), kwargs={"OwnerIds": ["self"]},
    ),
    _Enumerador(
        "ec2_nat_gateways", "ec2", "describe_nat_gateways", _chave("NatGateways"),
        lambda i, r, a: f"arn:aws:ec2:{r}:{a}:natgateway/{i['NatGatewayId']}",
        ("ec2:DescribeNatGateways",), tags=_tags_lista(),
        filtro=lambda i: i.get("State") in ("pending", "available"),
    ),
    _Enumerador(
        "ec2_elastic_ips", "ec2", "describe_addresses", _chave("Addresses"),
        lambda i, r, a: f"arn:aws:ec2:{r}:{a}:elastic-ip/{i['AllocationId']}" if i.get("AllocationId") else None,
        ("ec2:DescribeAddresses",), tags=_tags_lista(),
    ),
    _Enumerador(
        "ec2_transit_gateways", "ec2", "describe_transit_gateways", _chave("TransitGateways"),
        _campo("TransitGatewayArn"), ("ec2:DescribeTransitGateways",), tags=_tags_lista(),
        filtro=lambda i: i.get("State") not in ("deleted", "deleting"),
    ),
    _Enumerador(
        "ec2_vpc_endpoints", "ec2", "describe_vpc_endpoints", _chave("VpcEndpoints"),
        lambda i, r, a: f"arn:aws:ec2:{r}:{a}:vpc-endpoint/{i['VpcEndpointId']}",
        ("ec2:DescribeVpcEndpoints",), tags=_tags_lista(),
        filtro=lambda i: (i.get("State") or "").lower() not in ("deleted", "deleting"),
    ),
    _Enumerador(
        "ec2_vpn_connections", "ec2", "describe_vpn_connections", _chave("VpnConnections"),
        lambda i, r, a: f"arn:aws:ec2:{r}:{a}:vpn-connection/{i['VpnConnectionId']}",
        ("ec2:DescribeVpnConnections",), tags=_tags_lista(),
        filtro=lambda i: i.get("State") not in ("deleted", "deleting"),
    ),
    # ------------------------------ Armazenamento ------------------------------
    _Enumerador(
        # Global: roda uma vez só; a região real vem do próprio item.
        "s3_buckets", "s3", "list_buckets", _chave("Buckets"),
        lambda i, r, a: f"arn:aws:s3:::{i['Name']}",
        # GetBucketLocation quando o ListBuckets não traz BucketRegion (boto3
        # mais antigo no runtime do Lambda).
        ("s3:ListAllMyBuckets", "s3:GetBucketLocation"), global_=True, regiao_item=lambda i: i.get("BucketRegion"),
    ),
    _Enumerador(
        "efs", "efs", "describe_file_systems", _chave("FileSystems"),
        _campo("FileSystemArn"), ("elasticfilesystem:DescribeFileSystems",), tags=_tags_lista(),
    ),
    _Enumerador(
        "fsx", "fsx", "describe_file_systems", _chave("FileSystems"),
        _campo("ResourceARN"), ("fsx:DescribeFileSystems",), tags=_tags_lista(),
    ),
    _Enumerador(
        "backup_vaults", "backup", "list_backup_vaults", _chave("BackupVaultList"),
        _campo("BackupVaultArn"), ("backup:ListBackupVaults",),
    ),
    # ------------------------------ Bancos de dados ------------------------------
    _Enumerador(
        "rds_instancias", "rds", "describe_db_instances", _chave("DBInstances"),
        _campo("DBInstanceArn"), ("rds:DescribeDBInstances",), tags=_tags_lista("TagList"),
    ),
    _Enumerador(
        "rds_clusters", "rds", "describe_db_clusters", _chave("DBClusters"),
        _campo("DBClusterArn"), ("rds:DescribeDBClusters",), tags=_tags_lista("TagList"),
    ),
    _Enumerador(
        "dynamodb_tabelas", "dynamodb", "list_tables", _chave("TableNames"),
        lambda nome, r, a: f"arn:aws:dynamodb:{r}:{a}:table/{nome}", ("dynamodb:ListTables",),
    ),
    _Enumerador(
        "dax_clusters", "dax", "describe_clusters", _chave("Clusters"),
        _campo("ClusterArn"), ("dax:DescribeClusters",),
    ),
    _Enumerador(
        "elasticache_replication_groups", "elasticache", "describe_replication_groups",
        _chave("ReplicationGroups"), _campo("ARN"), ("elasticache:DescribeReplicationGroups",),
    ),
    _Enumerador(
        # Só clusters avulsos (Memcached / Redis sem replication group) — os
        # membros de um replication group já são cobertos pelo grupo.
        "elasticache_clusters", "elasticache", "describe_cache_clusters", _chave("CacheClusters"),
        _campo("ARN"), ("elasticache:DescribeCacheClusters",), filtro=lambda i: not i.get("ReplicationGroupId"),
    ),
    _Enumerador(
        "elasticache_serverless", "elasticache", "describe_serverless_caches", _chave("ServerlessCaches"),
        _campo("ARN"), ("elasticache:DescribeServerlessCaches",),
    ),
    _Enumerador(
        "memorydb_clusters", "memorydb", "describe_clusters", _chave("Clusters"),
        _campo("ARN"), ("memorydb:DescribeClusters",),
    ),
    _Enumerador(
        "redshift_clusters", "redshift", "describe_clusters", _chave("Clusters"),
        lambda i, r, a: f"arn:aws:redshift:{r}:{a}:cluster:{i['ClusterIdentifier']}",
        ("redshift:DescribeClusters",), tags=_tags_lista(),
    ),
    _Enumerador(
        "opensearch_dominios", "opensearch", "list_domain_names", _chave("DomainNames"),
        lambda i, r, a: f"arn:aws:es:{r}:{a}:domain/{i['DomainName']}", ("es:ListDomainNames",),
    ),
    # ------------------------------ Compute / containers ------------------------------
    _Enumerador(
        "lambda_funcoes", "lambda", "list_functions", _chave("Functions"),
        _campo("FunctionArn"), ("lambda:ListFunctions",),
    ),
    _Enumerador(
        "ecs_clusters", "ecs", "list_clusters", _chave("clusterArns"),
        lambda arn, r, a: arn, ("ecs:ListClusters",),
    ),
    _Enumerador(
        "ecr_repositorios", "ecr", "describe_repositories", _chave("repositories"),
        _campo("repositoryArn"), ("ecr:DescribeRepositories",),
    ),
    _Enumerador(
        "emr_clusters", "emr", "list_clusters", _chave("Clusters"),
        _campo("ClusterArn"), ("elasticmapreduce:ListClusters",),
        kwargs={"ClusterStates": ["STARTING", "BOOTSTRAPPING", "RUNNING", "WAITING"]},
    ),
    _Enumerador(
        "elastic_beanstalk_ambientes", "elasticbeanstalk", "describe_environments", _chave("Environments"),
        _campo("EnvironmentArn"), ("elasticbeanstalk:DescribeEnvironments",),
        filtro=lambda i: i.get("Status") not in ("Terminated", "Terminating"),
    ),
    _Enumerador(
        "sagemaker_endpoints", "sagemaker", "list_endpoints", _chave("Endpoints"),
        _campo("EndpointArn"), ("sagemaker:ListEndpoints",),
    ),
    _Enumerador(
        "sagemaker_notebooks", "sagemaker", "list_notebook_instances", _chave("NotebookInstances"),
        _campo("NotebookInstanceArn"), ("sagemaker:ListNotebookInstances",),
    ),
    _Enumerador(
        "workspaces", "workspaces", "describe_workspaces", _chave("Workspaces"),
        lambda i, r, a: f"arn:aws:workspaces:{r}:{a}:workspace/{i['WorkspaceId']}", ("workspaces:DescribeWorkspaces",),
    ),
    _Enumerador(
        "glue_jobs", "glue", "list_jobs", _chave("JobNames"),
        lambda nome, r, a: f"arn:aws:glue:{r}:{a}:job/{nome}", ("glue:ListJobs",),
    ),
    _Enumerador(
        "codebuild_projetos", "codebuild", "list_projects", _chave("projects"),
        lambda nome, r, a: f"arn:aws:codebuild:{r}:{a}:project/{nome}", ("codebuild:ListProjects",),
    ),
    _Enumerador(
        "codepipeline_pipelines", "codepipeline", "list_pipelines", _chave("pipelines"),
        lambda i, r, a: f"arn:aws:codepipeline:{r}:{a}:{i['name']}", ("codepipeline:ListPipelines",),
    ),
    # ------------------------------ Rede / borda ------------------------------
    _Enumerador(
        "elbv2", "elbv2", "describe_load_balancers", _chave("LoadBalancers"),
        _campo("LoadBalancerArn"), ("elasticloadbalancing:DescribeLoadBalancers",),
    ),
    _Enumerador(
        "elb_classico", "elb", "describe_load_balancers", _chave("LoadBalancerDescriptions"),
        lambda i, r, a: f"arn:aws:elasticloadbalancing:{r}:{a}:loadbalancer/{i['LoadBalancerName']}",
        ("elasticloadbalancing:DescribeLoadBalancers",),
    ),
    _Enumerador(
        "network_firewall", "network-firewall", "list_firewalls", _chave("Firewalls"),
        _campo("FirewallArn"), ("network-firewall:ListFirewalls",),
    ),
    _Enumerador(
        "cloudfront_distribuicoes", "cloudfront", "list_distributions",
        lambda resp: (resp.get("DistributionList") or {}).get("Items") or [],
        _campo("ARN"), ("cloudfront:ListDistributions",), global_=True,
    ),
    _Enumerador(
        "route53_zonas", "route53", "list_hosted_zones", _chave("HostedZones"),
        lambda i, r, a: f"arn:aws:route53:::hostedzone/{i['Id'].rsplit('/', 1)[-1]}",
        ("route53:ListHostedZones",), global_=True,
    ),
    _Enumerador(
        "apigateway_rest", "apigateway", "get_rest_apis", _chave("items"),
        lambda i, r, a: f"arn:aws:apigateway:{r}::/restapis/{i['id']}", ("apigateway:GET",), tags=_tags_dict("tags"),
    ),
    _Enumerador(
        "apigateway_http_ws", "apigatewayv2", "get_apis", _chave("Items"),
        lambda i, r, a: f"arn:aws:apigateway:{r}::/apis/{i['ApiId']}", ("apigateway:GET",), tags=_tags_dict(),
    ),
    # ------------------------------ Mensageria / streaming ------------------------------
    _Enumerador(
        "sqs_filas", "sqs", "list_queues", _chave("QueueUrls"),
        lambda url, r, a: f"arn:aws:sqs:{r}:{a}:{url.rstrip('/').rsplit('/', 1)[-1]}", ("sqs:ListQueues",),
    ),
    _Enumerador(
        "sns_topicos", "sns", "list_topics", _chave("Topics"),
        _campo("TopicArn"), ("sns:ListTopics",),
    ),
    _Enumerador(
        "kinesis_streams", "kinesis", "list_streams", _chave("StreamSummaries"),
        _campo("StreamARN"), ("kinesis:ListStreams",),
    ),
    _Enumerador(
        "firehose", "firehose", "list_delivery_streams", _chave("DeliveryStreamNames"),
        lambda nome, r, a: f"arn:aws:firehose:{r}:{a}:deliverystream/{nome}", ("firehose:ListDeliveryStreams",),
    ),
    _Enumerador(
        "msk_clusters", "kafka", "list_clusters_v2", _chave("ClusterInfoList"),
        _campo("ClusterArn"), ("kafka:ListClustersV2",), tags=_tags_dict(),
    ),
    _Enumerador(
        "mq_brokers", "mq", "list_brokers", _chave("BrokerSummaries"),
        _campo("BrokerArn"), ("mq:ListBrokers",),
    ),
    _Enumerador(
        "step_functions", "stepfunctions", "list_state_machines", _chave("stateMachines"),
        _campo("stateMachineArn"), ("states:ListStateMachines",),
    ),
    # ------------------------------ Segurança / observabilidade ------------------------------
    _Enumerador(
        # `logGroupArn` (sem o sufixo ":*") é o formato da Tagging API; o
        # campo antigo `arn` termina em ":*".
        "cloudwatch_log_groups", "logs", "describe_log_groups", _chave("logGroups"),
        lambda i, r, a: i.get("logGroupArn") or (i.get("arn") or "").removesuffix(":*") or None,
        ("logs:DescribeLogGroups",),
    ),
    _Enumerador(
        "secrets_manager", "secretsmanager", "list_secrets", _chave("SecretList"),
        _campo("ARN"), ("secretsmanager:ListSecrets",), tags=_tags_lista(),
    ),
    _Enumerador(
        "acm_certificados", "acm", "list_certificates", _chave("CertificateSummaryList"),
        _campo("CertificateArn"), ("acm:ListCertificates",),
    ),
    _Enumerador(
        "transfer_servidores", "transfer", "list_servers", _chave("Servers"),
        _campo("Arn"), ("transfer:ListServers",),
    ),
)


_IAM_ESPECIAIS = {"kms:ListKeys", "kms:DescribeKey", "ecs:ListServices", "ecs:ListTasks"}


def required_iam_actions() -> list[str]:
    """Permissões de LEITURA exigidas pelos enumeradores (inclui KMS e ECS)."""
    acoes = {a for e in _ENUMERADORES for a in e.iam} | _IAM_ESPECIAIS
    return sorted(acoes)


def servicos_cobertos() -> list[str]:
    return sorted({e.nome for e in _ENUMERADORES} | {"kms_chaves_cliente", "ecs_servicos", "ecs_tasks"})


def _ecs_servicos_e_tasks(session: boto3.Session, regiao: str) -> list[tuple[str, str | None]]:
    """ECS: services e tasks em execução de cada cluster, como
        `(arn, tipo_recurso)`. Tasks com ARN curto (sem o cluster) não aceitam tag."""
    client = session.client("ecs", region_name=regiao, config=_CLIENT_CONFIG)
    saida: list[tuple[str, str | None]] = []
    for pagina in client.get_paginator("list_clusters").paginate():
        for cluster in pagina.get("clusterArns", []):
            for p in client.get_paginator("list_services").paginate(cluster=cluster):
                saida.extend((arn, None) for arn in p.get("serviceArns", []))
            for p in client.get_paginator("list_tasks").paginate(cluster=cluster, desiredStatus="RUNNING"):
                for arn in p.get("taskArns", []):
                    if arn.split(":task/", 1)[-1].count("/") >= 1:
                        saida.append((arn, "task"))
    return saida


def _paginar(client, enum: _Enumerador) -> list:
    if client.can_paginate(enum.operacao):
        itens: list = []
        for pagina in client.get_paginator(enum.operacao).paginate(**enum.kwargs):
            itens.extend(enum.itens(pagina))
        return itens
    return enum.itens(getattr(client, enum.operacao)(**enum.kwargs))


def _kms_chaves_cliente(session: boto3.Session, regiao: str) -> list[str]:
    """KMS: só chaves gerenciadas pelo cliente (as AWS managed não são
        cobradas nem tagueáveis). Exige um `DescribeKey` por chave."""
    client = session.client("kms", region_name=regiao, config=_CLIENT_CONFIG)
    arns: list[str] = []
    for pagina in client.get_paginator("list_keys").paginate():
        for chave in pagina.get("Keys", []):
            meta = client.describe_key(KeyId=chave["KeyId"])["KeyMetadata"]
            if meta.get("KeyManager") == "CUSTOMER" and meta.get("KeyState") not in ("PendingDeletion",):
                arns.append(meta["Arn"])
    return arns


def _montar(arn: str, servico: str, regiao: str, tags: dict, expected: str) -> dict:
    status_tag, valor = get_tag_status(tags, expected)
    similares = find_similar_tag_keys(tags)
    return {
        "arn": arn,
        "servico": servico,
        "regiao": regiao,
        "tipo_recurso": None,
        "status_tag": status_tag,
        "valor_tag_encontrado": valor,
        "tag_similar_encontrada": bool(similares),
        "tag_similar_chaves": similares,
        "tag_similar_valores": {k: tags.get(k) for k in similares},
        "iac": detect_iac(tags),
        "servico_faturamento": servico,
        "cluster_eks": None,
        # Rastro de como o recurso foi encontrado — permite medir no
        # relatório quanto a descoberta nativa acrescentou.
        "origem_descoberta": "nativa",
    }


def discover_native_resources(
    session: boto3.Session,
    regiao: str,
    account_id: str,
    services: list[Service],
    expected_tag_value: str,
    incluir_s3: bool = True,
) -> tuple[list[dict], list[dict]]:
    """Recursos elegíveis da região pelas APIs nativas: `(recursos, falhas)`.
        O filtro "só o que o passo genérico não viu" fica com o chamador
        (`merge_native_resources`), pois buckets S3 e serviços globais podem
        aparecer no passo genérico de outra região."""
    recursos: list[dict] = []
    falhas: list[dict] = []
    for enum in _ENUMERADORES:
        if enum.global_ and regiao != REGIAO_SERVICOS_GLOBAIS:
            continue
        if enum.nome == "s3_buckets" and not incluir_s3:
            continue  # o pipeline trata S3 por região real (discover_s3_buckets_por_regiao)
        try:
            client = session.client(enum.client, region_name=regiao, config=_CLIENT_CONFIG)
            itens = _paginar(client, enum)
        except EndpointConnectionError:
            logger.debug("Serviço %s sem endpoint em %s — ignorado", enum.client, regiao)
            continue
        except ClientError as exc:
            erro = exc.response.get("Error", {})
            falhas.append(
                {
                    "regiao": regiao,
                    "etapa": f"nativo:{enum.nome}",
                    "erro": f"{enum.operacao}: {erro.get('Code', '')}: {erro.get('Message', str(exc))}",
                }
            )
            continue
        except BotoCoreError as exc:
            falhas.append({"regiao": regiao, "etapa": f"nativo:{enum.nome}", "erro": f"{enum.operacao}: {exc}"})
            continue
        except Exception as exc:  # noqa: BLE001 — isolamento por enumerador
            # Um enumerador quebrado (ex.: UnknownServiceError) não derruba os demais.
            logger.exception("Falha inesperada no enumerador %s em %s", enum.nome, regiao)
            falhas.append(
                {"regiao": regiao, "etapa": f"nativo:{enum.nome}", "erro": f"{type(exc).__name__}: {exc}"}
            )
            continue

        for item in itens:
            try:
                if enum.filtro is not None and not enum.filtro(item):
                    continue
                arn = enum.arn(item, regiao, account_id)
            except (KeyError, TypeError, AttributeError) as exc:
                falhas.append(
                    {"regiao": regiao, "etapa": f"nativo:{enum.nome}", "erro": f"item inesperado: {type(exc).__name__}: {exc}"}
                )
                continue
            if not arn:
                continue
            svc = classify_arn(arn, services)
            if svc is None:
                continue
            tags = (enum.tags(item) if enum.tags else None) or {}
            regiao_real = (enum.regiao_item(item) if enum.regiao_item else None) or regiao
            recursos.append(_montar(arn, svc.name, regiao_real, tags, expected_tag_value))

    try:
        for arn in _kms_chaves_cliente(session, regiao):
            svc = classify_arn(arn, services)
            if svc is not None:
                recursos.append(_montar(arn, svc.name, regiao, {}, expected_tag_value))
    except EndpointConnectionError:
        pass
    except ClientError as exc:
        erro = exc.response.get("Error", {})
        falhas.append({"regiao": regiao, "etapa": "nativo:kms_chaves_cliente", "erro": f"{erro.get('Code', '')}: {erro.get('Message', '')}"})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha inesperada no enumerador de KMS em %s", regiao)
        falhas.append({"regiao": regiao, "etapa": "nativo:kms_chaves_cliente", "erro": f"{type(exc).__name__}: {exc}"})

    try:
        for arn, tipo in _ecs_servicos_e_tasks(session, regiao):
            svc = classify_arn(arn, services)
            if svc is not None:
                recurso = _montar(arn, svc.name, regiao, {}, expected_tag_value)
                recurso["tipo_recurso"] = tipo
                recursos.append(recurso)
    except EndpointConnectionError:
        pass
    except ClientError as exc:
        erro = exc.response.get("Error", {})
        falhas.append({"regiao": regiao, "etapa": "nativo:ecs_servicos_e_tasks", "erro": f"{erro.get('Code', '')}: {erro.get('Message', '')}"})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha inesperada no enumerador de ECS em %s", regiao)
        falhas.append({"regiao": regiao, "etapa": "nativo:ecs_servicos_e_tasks", "erro": f"{type(exc).__name__}: {exc}"})

    logger.info("Região %s: %d recurso(s) listados pelas APIs nativas", regiao, len(recursos))
    return recursos, falhas


def _chave_comparacao(arn: str) -> str:
    """ARN sem o campo de conta. A descoberta roda numa conta só, e há ARN
        que aparece com e sem conta conforme a API (snapshot EBS)."""
    partes = arn.split(":", 5)
    if len(partes) == 6:
        partes[4] = ""
    return ":".join(partes)


def discover_s3_buckets_por_regiao(
    session: boto3.Session,
    regioes: set[str],
    account_id: str,
    services: list[Service],
    expected_tag_value: str,
) -> tuple[list[dict], list[dict]]:
    """Buckets S3 cuja região real está em `regioes` (cada região trata só
        os próprios buckets).

        `ListBuckets` é global. A região vem de `BucketRegion` ou, se o boto3
        não trouxer o campo, de `GetBucketLocation` por bucket, isolado: um
        bucket que nega a leitura vira falha própria sem derrubar os outros.
        As falhas são agregadas numa entrada (amostra de até 5 buckets)."""
    enum = next(e for e in _ENUMERADORES if e.nome == "s3_buckets")
    recursos: list[dict] = []
    negados: list[str] = []
    ultimo_erro = ""
    try:
        client = session.client("s3", region_name=REGIAO_SERVICOS_GLOBAIS, config=_CLIENT_CONFIG)
        itens = _paginar(client, enum)
    except (ClientError, BotoCoreError) as exc:
        return [], [{"regiao": ",".join(sorted(regioes)), "etapa": "nativo:s3_buckets", "erro": str(exc)}]
    for item in itens:
        regiao = item.get("BucketRegion")
        if not regiao:
            try:
                loc = client.get_bucket_location(Bucket=item["Name"]).get("LocationConstraint")
            except (ClientError, BotoCoreError) as exc:
                negados.append(item["Name"])
                ultimo_erro = str(exc)
                continue
            regiao = {None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}.get(loc, loc)
        if regiao not in regioes:
            continue
        arn = enum.arn(item, regiao, account_id)
        svc = classify_arn(arn, services)
        if svc is not None:
            recursos.append(_montar(arn, svc.name, regiao, {}, expected_tag_value))
    falhas = []
    if negados:
        falhas.append(
            {
                "regiao": ",".join(sorted(regioes)),
                "etapa": "nativo:s3_buckets",
                "erro": f"{len(negados)} bucket(s) sem leitura de região (ignorados; os demais seguiram): "
                f"{', '.join(negados[:5])}{' ...' if len(negados) > 5 else ''}. Último erro: {ultimo_erro}",
            }
        )
    return recursos, falhas


def merge_native_resources(genericos: list[dict], nativos: list[dict]) -> list[dict]:
    """Só os recursos nativos cujo ARN não apareceu em nenhum resultado
        anterior (de qualquer região), sem duplicatas entre si."""
    vistos = {_chave_comparacao(r["arn"]) for r in genericos}
    novos: list[dict] = []
    for r in nativos:
        chave = _chave_comparacao(r["arn"])
        if chave in vistos:
            continue
        vistos.add(chave)
        novos.append(r)
    return novos
