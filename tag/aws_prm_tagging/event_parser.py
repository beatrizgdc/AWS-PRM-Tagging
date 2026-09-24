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
recurso independentemente.

## Dois níveis de confiança na extração — e por que existe um terceiro, mais
## fino, dentro do próprio nível "dedicado"

1. **Extractors dedicados** (`_REGISTRY` abaixo) — para os serviços mapeados
   em `event_mapping.py`. Cada campo/caminho usado foi checado contra o
   shape real da operação no `botocore` instalado localmente (não vem só de
   memória) — ver `event_mapping.py` para o processo de verificação.
2. **Fallback genérico best-effort** (`_extract_generic`) — só para o raro
   caso de um evento mapeado sem entrada em `_REGISTRY` (não deveria
   acontecer com a tabela atual, mas existe como rede de segurança):
   procura recursivamente por uma chave terminando em "arn" dentro de
   `responseElements`; nunca inventa um ARN.

Dentro do nível "dedicado", uma distinção adicional importa: o *protocolo*
de API de cada serviço determina se a capitalização exata do shape do
botocore é uma boa aposta para o `responseElements` real do CloudTrail.

- **Serviços `json`/`rest-json`/`smithy-rpc-v2-cbor`** (a grande maioria
  dos mapeados): a experiência documentada é que o CloudTrail preserva os
  nomes de campo tal como a própria API os devolve — usamos `_direct`/
  `_constructed` com a capitalização exata do botocore, alta confiança.
- **Serviços `query`/`ec2`/`rest-xml`** (`ec2`, `rds`, `elasticache`,
  `redshift`, `elasticbeanstalk`, `sns`, `s3`, `route53`, `cloudfront`,
  `elasticloadbalancing`/elbv2): o CloudTrail historicamente aplica uma
  conversão XML→JSON com convenção própria (tipicamente primeira letra
  minúscula) — usamos `_direct_ci`/`_constructed_ci` (tenta a capitalização
  exata E a variante com a primeira letra minúscula) para esses, ou, onde
  possível, preferimos construir o ARN a partir de `requestParameters`
  (mais simples, menos superfície de erro) em vez de confiar num campo de
  resposta com capitalização incerta. `ec2` especificamente tem uma
  estrutura própria adicional bem conhecida (`"instancesSet": {"items":
  [...]}`) tratada em extractors dedicados, não pelos helpers genéricos.

**8 dos 9 serviços de protocolo `query`/`ec2`/`rest-xml` já foram
validados contra um evento CloudTrail real capturado em sandbox**
(2026-09-23/24) — achou e corrigiu 3 bugs reais (RDS `CreateDBInstance`,
ElastiCache `CreateCacheCluster`, Elastic Beanstalk `CreateApplication`);
confirmou S3, SNS, Route 53, ELB e Redshift corretos como estavam. Só
CloudFront (`CreateDistribution`) segue sem evento real capturado (pulado
por custo de tempo de propagação, não de dinheiro) — ver
[docs/melhorias-futuras.md](docs/melhorias-futuras.md) e
[test/manual-live-etapa3/README.md](test/manual-live-etapa3/README.md)
para o resultado completo, serviço a serviço.
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
# Helpers genéricos — cobrem a maioria dos extractors dedicados: ler um
# campo (direto ou construindo o ARN a partir de um ID) em um caminho fixo
# dentro de responseElements/requestParameters.
# ---------------------------------------------------------------------------


def _read_path(node: dict, path: tuple[str, ...], tolerant_casing: bool) -> object:
    for key in path:
        if not isinstance(node, dict):
            return None
        if tolerant_casing:
            alt = (key[0].lower() + key[1:]) if key else key
            node = node.get(key, node.get(alt))
        else:
            node = node.get(key)
    return node


@dataclass(frozen=True)
class _DirectPath:
    """ARN já vem pronto em `responseElements`, no `path` dado. Uma classe
    (não uma closure) de propósito: `path`/`tolerant_casing` ficam
    inspecionáveis por fora — é o que permite `test_event_parser_botocore.py`
    verificar cada extractor dedicado contra o shape real da operação no
    botocore instalado, sem precisar reimplementar a leitura em cada teste."""

    path: tuple[str, ...]
    tolerant_casing: bool = False

    def __call__(self, detail: dict, account_id: str, region: str) -> list[str]:
        val = _read_path(detail.get("responseElements") or {}, self.path, self.tolerant_casing)
        return [val] if isinstance(val, str) and val.startswith("arn:") else []


@dataclass(frozen=True)
class _ConstructedPath:
    """ARN não vem pronto — construído a partir de um `template` (com
    `{region}`/`{account_id}`/`{value}`) usando um valor lido de `source`
    ("responseElements" ou "requestParameters") no `path` dado."""

    template: str
    source: str
    path: tuple[str, ...]
    tolerant_casing: bool = False

    def __call__(self, detail: dict, account_id: str, region: str) -> list[str]:
        val = _read_path(detail.get(self.source) or {}, self.path, self.tolerant_casing)
        if not val:
            return []
        return [self.template.format(region=region, account_id=account_id, value=val)]


def _direct(*path: str, tolerant_casing: bool = False) -> ArnBuilder:
    return _DirectPath(path, tolerant_casing)


def _direct_ci(*path: str) -> ArnBuilder:
    """Como `_direct`, mas tolerante à capitalização (ver docstring do
    módulo — serviços de protocolo query/ec2/rest-xml)."""
    return _direct(*path, tolerant_casing=True)


def _constructed(template: str, source: str, *path: str, tolerant_casing: bool = False) -> ArnBuilder:
    return _ConstructedPath(template, source, path, tolerant_casing)


def _constructed_ci(template: str, source: str, *path: str) -> ArnBuilder:
    return _constructed(template, source, *path, tolerant_casing=True)


# ---------------------------------------------------------------------------
# Extractors com lógica própria — batch (EC2, WorkSpaces), pré-processamento
# do valor (Route 53 tira o prefixo "/hostedzone/"), ou estrutura de
# CloudTrail conhecida e diferente do shape "lógico" do botocore (EC2).
# ---------------------------------------------------------------------------


def _ext_ec2_run_instances(detail: dict, account_id: str, region: str) -> list[str]:
    # Estrutura conhecida de eventos reais de CloudTrail para EC2 (protocolo
    # "ec2", com o empacotamento "xSet"/"items" herdado da API XML antiga) —
    # não é o shape "lógico" que o botocore expõe para o SDK.
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
    # S3 (rest-xml) tem, em versões recentes da API, um campo BucketArn
    # direto na resposta — mas construir a partir do nome pedido
    # (requestParameters, sempre presente, sem incerteza de capitalização)
    # é mais robusto contra variação de versão de API/SDK do chamador.
    bucket = (detail.get("requestParameters") or {}).get("bucketName")
    return [f"arn:aws:s3:::{bucket}"] if bucket else []


def _ext_sqs_create_queue(detail: dict, account_id: str, region: str) -> list[str]:
    # CreateQueue só devolve QueueUrl na resposta, nunca o ARN — construído
    # a partir do nome pedido na requisição (sempre presente).
    queue_name = (detail.get("requestParameters") or {}).get("QueueName")
    return [f"arn:aws:sqs:{region}:{account_id}:{queue_name}"] if queue_name else []


def _ext_route53_create_hosted_zone(detail: dict, account_id: str, region: str) -> list[str]:
    # ARNs de Route 53 não têm região nem conta no formato usual — o "Id"
    # devolvido às vezes já vem prefixado com "/hostedzone/". Tolerante a
    # capitalização (route53 é protocolo rest-xml).
    zone_id = _read_path(detail.get("responseElements") or {}, ("HostedZone", "Id"), tolerant_casing=True)
    if not zone_id:
        return []
    zone_id = str(zone_id).rsplit("/", 1)[-1]
    return [f"arn:aws:route53:::hostedzone/{zone_id}"]


def _ext_elbv2_create_load_balancer(detail: dict, account_id: str, region: str) -> list[str]:
    # elbv2 é protocolo "query" (tolerante a capitalização); CreateLoadBalancer
    # normalmente cria só 1 load balancer por chamada — usamos o primeiro.
    lbs = _read_path(detail.get("responseElements") or {}, ("LoadBalancers",), tolerant_casing=True) or []
    if not lbs or not isinstance(lbs, list):
        return []
    arn = lbs[0].get("LoadBalancerArn", lbs[0].get("loadBalancerArn")) if isinstance(lbs[0], dict) else None
    return [arn] if isinstance(arn, str) and arn.startswith("arn:") else []


def _ext_workspaces_create_workspaces(detail: dict, account_id: str, region: str) -> list[str]:
    # CreateWorkspaces é uma API de LOTE (cria N workspaces numa chamada) —
    # cada item pendente vira um recurso próprio.
    pending = (detail.get("responseElements") or {}).get("PendingRequests") or []
    return [
        f"arn:aws:workspaces:{region}:{account_id}:workspace/{item['WorkspaceId']}"
        for item in pending
        if isinstance(item, dict) and item.get("WorkspaceId")
    ]


_REGISTRY: dict[tuple[str, str], _SpecificExtractor] = {
    # --- EC2 (protocolo "ec2" — estrutura de CloudTrail conhecida à parte) ---
    ("ec2.amazonaws.com", "RunInstances"): _SpecificExtractor("AmazonEC2", _ext_ec2_run_instances),
    ("ec2.amazonaws.com", "CreateVolume"): _SpecificExtractor("AmazonEC2", _ext_ec2_create_volume),
    ("ec2.amazonaws.com", "CreateVpc"): _SpecificExtractor("AmazonVPC", _ext_ec2_create_vpc),
    ("ec2.amazonaws.com", "CreateTransitGateway"): _SpecificExtractor("AmazonVPC", _ext_ec2_create_transit_gateway),
    # --- S3 (rest-xml — construído a partir de requestParameters) ---
    ("s3.amazonaws.com", "CreateBucket"): _SpecificExtractor("AmazonS3", _ext_s3_create_bucket),
    # --- Protocolo query/ec2/rest-xml — leitura tolerante a capitalização ---
    # Confirmado em evento real capturado em sandbox: ao contrário do shape
    # documentado do botocore (que embrulha a resposta em `{"DBInstance":
    # {...}}`), o CloudTrail grava os campos JÁ ACHATADOS — `dBInstanceArn`
    # direto na raiz de `responseElements`, sem o wrapper. `CreateDBCluster`
    # (Aurora) segue o mesmo padrão de shape no botocore (`{"DBCluster":
    # {...}}`) mas não foi testado com um evento real nesta sessão — corrigido
    # por analogia com `CreateDBInstance`, não confirmado independentemente.
    ("rds.amazonaws.com", "CreateDBInstance"): _SpecificExtractor("AmazonRDS", _direct_ci("DBInstanceArn")),
    ("rds.amazonaws.com", "CreateDBCluster"): _SpecificExtractor("AmazonRDS", _direct_ci("DBClusterArn")),
    # Mesmo achatamento confirmado em evento real (ver CreateDBInstance
    # acima): `aRN` vem direto na raiz de `responseElements`, sem o wrapper
    # `CacheCluster` que o shape do botocore documenta.
    ("elasticache.amazonaws.com", "CreateCacheCluster"): _SpecificExtractor(
        "AmazonElastiCache", _direct_ci("ARN")
    ),
    ("redshift.amazonaws.com", "CreateCluster"): _SpecificExtractor(
        "AmazonRedshift",
        _constructed_ci("arn:aws:redshift:{region}:{account_id}:cluster:{value}", "requestParameters", "ClusterIdentifier"),
    ),
    ("elasticbeanstalk.amazonaws.com", "CreateApplication"): _SpecificExtractor(
        "AWSElasticBeanstalk",
        # Confirmado em evento real capturado em sandbox: `responseElements`
        # vem `null` para este evento (não `{"Application": {...}}` como um
        # `_direct_ci` assumiria) — o ARN precisa ser construído a partir do
        # nome pedido, igual ao caminho do S3.
        _constructed_ci(
            "arn:aws:elasticbeanstalk:{region}:{account_id}:application/{value}",
            "requestParameters",
            "ApplicationName",
        ),
    ),
    ("elasticbeanstalk.amazonaws.com", "CreateEnvironment"): _SpecificExtractor(
        "AWSElasticBeanstalk", _direct_ci("EnvironmentArn")
    ),
    ("sns.amazonaws.com", "CreateTopic"): _SpecificExtractor("AmazonSNS", _direct_ci("TopicArn")),
    ("route53.amazonaws.com", "CreateHostedZone"): _SpecificExtractor("AmazonRoute53", _ext_route53_create_hosted_zone),
    ("cloudfront.amazonaws.com", "CreateDistribution"): _SpecificExtractor(
        "AmazonCloudFront",
        _constructed_ci("arn:aws:cloudfront::{account_id}:distribution/{value}", "responseElements", "Distribution", "Id"),
    ),
    ("elasticloadbalancing.amazonaws.com", "CreateLoadBalancer"): _SpecificExtractor(
        "AWSELB", _ext_elbv2_create_load_balancer
    ),
    ("sqs.amazonaws.com", "CreateQueue"): _SpecificExtractor("AWSQueueService", _ext_sqs_create_queue),
    ("workspaces.amazonaws.com", "CreateWorkspaces"): _SpecificExtractor("AmazonWorkSpaces", _ext_workspaces_create_workspaces),
    # --- Protocolo json/rest-json/smithy — capitalização do botocore confiável ---
    ("lambda.amazonaws.com", "CreateFunction"): _SpecificExtractor("AWSLambda", _direct("FunctionArn")),
    ("dynamodb.amazonaws.com", "CreateTable"): _SpecificExtractor("AmazonDynamoDB", _direct("TableDescription", "TableArn")),
    ("eks.amazonaws.com", "CreateCluster"): _SpecificExtractor(
        "AmazonEKS", _direct("cluster", "arn"), tipo_recurso="cluster"
    ),
    ("eks.amazonaws.com", "CreateNodegroup"): _SpecificExtractor(
        "AmazonEKS", _direct("nodegroup", "nodegroupArn"), tipo_recurso="node_group"
    ),
    ("bedrock.amazonaws.com", "CreateInferenceProfile"): _SpecificExtractor(
        "AmazonBedrock", _direct("inferenceProfileArn"), tipo_recurso="application_inference_profile"
    ),
    ("ecr.amazonaws.com", "CreateRepository"): _SpecificExtractor("AmazonECR", _direct("repository", "repositoryArn")),
    ("ecs.amazonaws.com", "CreateCluster"): _SpecificExtractor("AmazonECS", _direct("cluster", "clusterArn")),
    ("ecs.amazonaws.com", "CreateService"): _SpecificExtractor("AmazonECS", _direct("service", "serviceArn")),
    ("elasticfilesystem.amazonaws.com", "CreateFileSystem"): _SpecificExtractor("AmazonEFS", _direct("FileSystemArn")),
    ("kms.amazonaws.com", "CreateKey"): _SpecificExtractor("awskms", _direct("KeyMetadata", "Arn")),
    ("secretsmanager.amazonaws.com", "CreateSecret"): _SpecificExtractor("AWSSecretsManager", _direct("ARN")),
    ("states.amazonaws.com", "CreateStateMachine"): _SpecificExtractor("AmazonStates", _direct("stateMachineArn")),
    ("appsync.amazonaws.com", "CreateGraphqlApi"): _SpecificExtractor("AWSAppSync", _direct("graphqlApi", "arn")),
    ("backup.amazonaws.com", "CreateBackupVault"): _SpecificExtractor("AWSBackup", _direct("BackupVaultArn")),
    ("backup.amazonaws.com", "CreateBackupPlan"): _SpecificExtractor("AWSBackup", _direct("BackupPlanArn")),
    ("acm.amazonaws.com", "RequestCertificate"): _SpecificExtractor("AWSCertificateManager", _direct("CertificateArn")),
    ("acm-pca.amazonaws.com", "CreateCertificateAuthority"): _SpecificExtractor(
        "AWSCertificateManager", _direct("CertificateAuthorityArn")
    ),
    ("networkmanager.amazonaws.com", "CreateCoreNetwork"): _SpecificExtractor(
        "AWSCloudWAN", _direct("CoreNetwork", "CoreNetworkArn")
    ),
    ("networkmanager.amazonaws.com", "CreateGlobalNetwork"): _SpecificExtractor(
        "AWSCloudWAN", _direct("GlobalNetwork", "GlobalNetworkArn")
    ),
    ("medialive.amazonaws.com", "CreateChannel"): _SpecificExtractor("AWSElementalMediaLive", _direct("Channel", "Arn")),
    ("mediapackage.amazonaws.com", "CreateChannel"): _SpecificExtractor("AWSElementalMediaPackage", _direct("Arn")),
    ("network-firewall.amazonaws.com", "CreateFirewall"): _SpecificExtractor(
        "AWSNetworkFirewall", _direct("Firewall", "FirewallArn")
    ),
    ("resiliencehub.amazonaws.com", "CreateApp"): _SpecificExtractor("AWSResilienceHub", _direct("app", "appArn")),
    ("ssm.amazonaws.com", "CreateOpsItem"): _SpecificExtractor("AWSSystemsManager", _direct("OpsItemArn")),
    ("clouddirectory.amazonaws.com", "CreateDirectory"): _SpecificExtractor("AmazonCloudDirectory", _direct("DirectoryArn")),
    ("connect.amazonaws.com", "CreateInstance"): _SpecificExtractor("AmazonConnect", _direct("Arn")),
    ("fsx.amazonaws.com", "CreateFileSystem"): _SpecificExtractor("AmazonFSx", _direct("FileSystem", "ResourceARN")),
    ("finspace.amazonaws.com", "CreateEnvironment"): _SpecificExtractor("AmazonFinSpace", _direct("environmentArn")),
    ("gamelift.amazonaws.com", "CreateFleet"): _SpecificExtractor("AmazonGameLift", _direct("FleetAttributes", "FleetArn")),
    ("healthlake.amazonaws.com", "CreateFHIRDatastore"): _SpecificExtractor("AmazonHealthLake", _direct("DatastoreArn")),
    ("kinesisanalytics.amazonaws.com", "CreateApplication"): _SpecificExtractor(
        "AmazonKinesisAnalytics", _direct("ApplicationDetail", "ApplicationARN")
    ),
    ("firehose.amazonaws.com", "CreateDeliveryStream"): _SpecificExtractor("AmazonKinesisFirehose", _direct("DeliveryStreamARN")),
    ("kinesisvideo.amazonaws.com", "CreateStream"): _SpecificExtractor("AmazonKinesisVideo", _direct("StreamARN")),
    ("mq.amazonaws.com", "CreateBroker"): _SpecificExtractor("AmazonMQ", _direct("BrokerArn")),
    ("kafka.amazonaws.com", "CreateClusterV2"): _SpecificExtractor("AmazonMSK", _direct("ClusterArn")),
    ("memorydb.amazonaws.com", "CreateCluster"): _SpecificExtractor("AmazonMemoryDB", _direct("Cluster", "ARN")),
    ("timestream.amazonaws.com", "CreateDatabase"): _SpecificExtractor("AmazonTimestream", _direct("Database", "Arn")),
    ("vpc-lattice.amazonaws.com", "CreateServiceNetwork"): _SpecificExtractor("AmazonVPC", _direct("arn")),
    ("payment-cryptography.amazonaws.com", "CreateKey"): _SpecificExtractor("PaymentCryptography", _direct("Key", "KeyArn")),
    ("codebuild.amazonaws.com", "CreateProject"): _SpecificExtractor("CodeBuild", _direct("project", "arn")),
    ("es.amazonaws.com", "CreateDomain"): _SpecificExtractor("AmazonES", _direct("DomainStatus", "ARN")),
    ("es.amazonaws.com", "CreateElasticsearchDomain"): _SpecificExtractor("AmazonES", _direct("DomainStatus", "ARN")),
    ("redshift-serverless.amazonaws.com", "CreateWorkgroup"): _SpecificExtractor(
        "AmazonRedshift", _direct("workgroup", "workgroupArn")
    ),
    ("dsql.amazonaws.com", "CreateCluster"): _SpecificExtractor("AuroraDSQL", _direct("arn")),
    ("datapipeline.amazonaws.com", "CreatePipeline"): _SpecificExtractor(
        "datapipeline", _constructed("arn:aws:datapipeline:{region}:{account_id}:pipeline/{value}", "responseElements", "pipelineId")
    ),
    ("cassandra.amazonaws.com", "CreateKeyspace"): _SpecificExtractor("AmazonMCS", _direct("resourceArn")),
    ("cassandra.amazonaws.com", "CreateTable"): _SpecificExtractor("AmazonMCS", _direct("resourceArn")),
    ("apigateway.amazonaws.com", "CreateRestApi"): _SpecificExtractor(
        "AmazonApiGateway", _constructed("arn:aws:apigateway:{region}::/restapis/{value}", "responseElements", "id")
    ),
    ("apigateway.amazonaws.com", "CreateApi"): _SpecificExtractor(
        "AmazonApiGateway", _constructed("arn:aws:apigateway:{region}::/apis/{value}", "responseElements", "ApiId")
    ),
    ("storagegateway.amazonaws.com", "CreateNFSFileShare"): _SpecificExtractor("AWSStorageGateway", _direct("FileShareARN")),
    ("storagegateway.amazonaws.com", "CreateSMBFileShare"): _SpecificExtractor("AWSStorageGateway", _direct("FileShareARN")),
    ("storagegateway.amazonaws.com", "CreateStorediSCSIVolume"): _SpecificExtractor("AWSStorageGateway", _direct("VolumeARN")),
    ("storagegateway.amazonaws.com", "CreateCachediSCSIVolume"): _SpecificExtractor("AWSStorageGateway", _direct("VolumeARN")),
    ("m2.amazonaws.com", "CreateEnvironment"): _SpecificExtractor(
        "AWSM2", _constructed("arn:aws:m2:{region}:{account_id}:environment/{value}", "responseElements", "environmentId")
    ),
    ("m2.amazonaws.com", "CreateApplication"): _SpecificExtractor("AWSM2", _direct("applicationArn")),
    ("mediaconvert.amazonaws.com", "CreateQueue"): _SpecificExtractor("AWSElementalMediaConvert", _direct("Queue", "Arn")),
    ("cognito-idp.amazonaws.com", "CreateUserPool"): _SpecificExtractor("AmazonCognito", _direct("UserPool", "Arn")),
    ("cognito-identity.amazonaws.com", "CreateIdentityPool"): _SpecificExtractor(
        "AmazonCognito",
        _constructed("arn:aws:cognito-identity:{region}:{account_id}:identitypool/{value}", "responseElements", "IdentityPoolId"),
    ),
    ("dms.amazonaws.com", "CreateReplicationInstance"): _SpecificExtractor(
        "AWSDatabaseMigrationSvc", _direct("ReplicationInstance", "ReplicationInstanceArn")
    ),
    ("dms.amazonaws.com", "CreateReplicationTask"): _SpecificExtractor(
        "AWSDatabaseMigrationSvc", _direct("ReplicationTask", "ReplicationTaskArn")
    ),
    ("directconnect.amazonaws.com", "CreateConnection"): _SpecificExtractor(
        "AWSDirectConnect",
        _constructed("arn:aws:directconnect:{region}:{account_id}:dxcon/{value}", "responseElements", "connectionId"),
    ),
    ("directconnect.amazonaws.com", "CreateDirectConnectGateway"): _SpecificExtractor(
        "AWSDirectConnect",
        _constructed(
            "arn:aws:directconnect::{account_id}:dx-gateway/{value}",
            "responseElements",
            "directConnectGateway",
            "directConnectGatewayId",
        ),
    ),
    ("ds.amazonaws.com", "CreateDirectory"): _SpecificExtractor(
        "AWSDirectoryService", _constructed("arn:aws:ds:{region}:{account_id}:directory/{value}", "responseElements", "DirectoryId")
    ),
    ("ds.amazonaws.com", "CreateMicrosoftAD"): _SpecificExtractor(
        "AWSDirectoryService", _constructed("arn:aws:ds:{region}:{account_id}:directory/{value}", "responseElements", "DirectoryId")
    ),
    ("transfer.amazonaws.com", "CreateServer"): _SpecificExtractor(
        "AWSTransfer", _constructed("arn:aws:transfer:{region}:{account_id}:server/{value}", "responseElements", "ServerId")
    ),
    ("athena.amazonaws.com", "CreateWorkGroup"): _SpecificExtractor(
        "AmazonAthena", _constructed("arn:aws:athena:{region}:{account_id}:workgroup/{value}", "requestParameters", "Name")
    ),
    ("athena.amazonaws.com", "CreateDataCatalog"): _SpecificExtractor(
        "AmazonAthena", _constructed("arn:aws:athena:{region}:{account_id}:datacatalog/{value}", "requestParameters", "Name")
    ),
    ("logs.amazonaws.com", "CreateLogGroup"): _SpecificExtractor(
        "AmazonCloudWatch",
        _constructed("arn:aws:logs:{region}:{account_id}:log-group:{value}", "requestParameters", "logGroupName"),
    ),
    ("glacier.amazonaws.com", "CreateVault"): _SpecificExtractor(
        "AmazonGlacier", _constructed("arn:aws:glacier:{region}:{account_id}:vaults/{value}", "requestParameters", "vaultName")
    ),
    ("kendra.amazonaws.com", "CreateIndex"): _SpecificExtractor(
        "AmazonKendra", _constructed("arn:aws:kendra:{region}:{account_id}:index/{value}", "responseElements", "Id")
    ),
    ("kinesis.amazonaws.com", "CreateStream"): _SpecificExtractor(
        "AmazonKinesis", _constructed("arn:aws:kinesis:{region}:{account_id}:stream/{value}", "requestParameters", "StreamName")
    ),
    ("medical-imaging.amazonaws.com", "CreateDatastore"): _SpecificExtractor(
        "AmazonMedicalImaging",
        _constructed("arn:aws:medical-imaging:{region}:{account_id}:datastore/{value}", "responseElements", "datastoreId"),
    ),
    ("codepipeline.amazonaws.com", "CreatePipeline"): _SpecificExtractor(
        "AWSCodePipeline",
        _constructed("arn:aws:codepipeline:{region}:{account_id}:{value}", "requestParameters", "pipeline", "name"),
    ),
    ("cloudhsm.amazonaws.com", "CreateCluster"): _SpecificExtractor(
        "CloudHSM", _constructed("arn:aws:cloudhsm:{region}:{account_id}:cluster/{value}", "responseElements", "Cluster", "ClusterId")
    ),
    ("elasticmapreduce.amazonaws.com", "RunJobFlow"): _SpecificExtractor("ElasticMapReduce", _direct("ClusterArn")),
}


# ---------------------------------------------------------------------------
# Fallback genérico best-effort — rede de segurança para um evento mapeado
# em event_mapping.py sem entrada em _REGISTRY (não deveria acontecer com a
# tabela atual, mas defensivo).
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
