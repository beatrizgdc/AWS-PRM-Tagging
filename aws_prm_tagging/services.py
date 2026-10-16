"""Serviços elegíveis ao PRM (Resource Tagging) e classificação de ARNs.

A lista vem do CSV oficial da AWS (`data/resource-tagging-included-services.csv`).
Este módulo acrescenta o mapeamento do namespace do ARN (ex.: "ec2", "s3")
para o Product Service Code do CSV, porque a Tagging API devolve ARNs.

Casos especiais em `classify_arn`:

- "rds" cobre RDS, Aurora, DocumentDB e Neptune; sem chamar a API não dá para
  separar, então tudo sai como RDS (na Etapa 3 o `engine` do evento separa).
- "ec2" cobre EC2 e a rede faturada como "AmazonVPC"; o tipo de recurso do
  ARN desambigua.
- "AmazonVPC" tem duas linhas no CSV (Transit Gateway e VPC Lattice); o
  namespace "vpc-lattice" é resolvido por nome, não por código.
- "ssm": o CSV inclui só o OpsCenter, então só o tipo `opsitem` entra.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from importlib import resources
from typing import Optional

logger = logging.getLogger(__name__)

CSV_RESOURCE = "resource-tagging-included-services.csv"


@dataclass(frozen=True)
class Service:
    name: str
    product_service_code: str
    notes: str


def load_services() -> list[Service]:
    """Carrega a lista oficial de serviços elegíveis a partir do CSV."""
    with resources.files(f"{__package__}.data").joinpath(CSV_RESOURCE).open(
        "r", encoding="utf-8"
    ) as f:
        reader = csv.DictReader(f)
        services = [
            Service(
                name=row["Service Name"].strip(),
                product_service_code=row["Product Service Code"].strip(),
                notes=(row.get("Notes") or "").strip(),
            )
            for row in reader
            if row.get("Service Name")
        ]
    if not services:
        raise RuntimeError(f"Nenhum serviço carregado de {CSV_RESOURCE}")
    return services


# Bedrock e EKS ficam de fora: têm descoberta dedicada.
_NAMESPACE_TO_CODE: dict[str, str] = {
    "apigateway": "AmazonApiGateway",
    "appstream": "AmazonAppStream",
    "appsync": "AWSAppSync",
    "athena": "AmazonAthena",
    "dsql": "AuroraDSQL",
    "backup": "AWSBackup",
    # "bedrock" fica de fora: só application inference profiles contam e são
    # tratados em resource_discovery.py. AgentCore usa o passo genérico.
    "bedrock-agentcore": "AmazonBedrockAgentCore",
    "acm": "AWSCertificateManager",
    "acm-pca": "AWSCertificateManager",
    "clouddirectory": "AmazonCloudDirectory",
    "networkmanager": "AWSCloudWAN",
    "cloudfront": "AmazonCloudFront",
    "cloudhsm": "CloudHSM",
    "logs": "AmazonCloudWatch",
    "codebuild": "CodeBuild",
    "codepipeline": "AWSCodePipeline",
    "codestar": "AWSCodeStar",
    "cognito-idp": "AmazonCognito",
    "cognito-identity": "AmazonCognito",
    "comprehend": "comprehend",
    "connect": "AmazonConnect",
    "datapipeline": "datapipeline",
    "dms": "AWSDatabaseMigrationSvc",
    "datasync": "AWSDataSync",
    "deadline": "AWSDeadlineCloud",
    "directconnect": "AWSDirectConnect",
    "ds": "AWSDirectoryService",
    "dynamodb": "AmazonDynamoDB",
    # DAX tem linha própria no CSV.
    "dax": "AmazonDAX",
    # ec2: desambiguado em classify_arn (AmazonEC2 vs AmazonVPC).
    "ecr": "AmazonECR",
    "ecs": "AmazonECS",
    # eks: tratado via resource_discovery especial (EKS).
    "elasticbeanstalk": "AWSElasticBeanstalk",
    "drs": "AWSElasticDisasterRecovery",
    "elasticfilesystem": "AmazonEFS",
    "elasticache": "AmazonElastiCache",
    "mediaconvert": "AWSElementalMediaConvert",
    "medialive": "AWSElementalMediaLive",
    "mediapackage": "AWSElementalMediaPackage",
    "elasticmapreduce": "ElasticMapReduce",
    "finspace": "AmazonFinSpace",
    "fsx": "AmazonFSx",
    "gamelift": "AmazonGameLift",
    "glue": "AWSGlue",
    "medical-imaging": "AmazonMedicalImaging",
    "healthlake": "AmazonHealthLake",
    "iot": "AWSIoT",
    "iotsitewise": "AWSIoTSiteWise",
    "kendra": "AmazonKendra",
    "kms": "awskms",
    "cassandra": "AmazonMCS",
    "kinesisanalytics": "AmazonKinesisAnalytics",
    "firehose": "AmazonKinesisFirehose",
    "kinesis": "AmazonKinesis",
    "kinesisvideo": "AmazonKinesisVideo",
    "lambda": "AWSLambda",
    "elasticloadbalancing": "AWSELB",
    "m2": "AWSM2",
    "memorydb": "AmazonMemoryDB",
    "mq": "AmazonMQ",
    "kafka": "AmazonMSK",
    # rds: também cobre DocumentDB e Neptune (ver limitação nº 1 acima).
    "rds": "AmazonRDS",
    "network-firewall": "AWSNetworkFirewall",
    "omics": "AmazonOmics",
    "es": "AmazonES",
    "payment-cryptography": "PaymentCryptography",
    "quicksight": "AmazonQuickSight",
    "redshift": "AmazonRedshift",
    "redshift-serverless": "AmazonRedshift",
    "resiliencehub": "AWSResilienceHub",
    "route53": "AmazonRoute53",
    "s3": "AmazonS3",
    "glacier": "AmazonGlacier",
    "sagemaker": "AmazonSageMaker",
    "secretsmanager": "AWSSecretsManager",
    "securityhub": "AWSSecurityHub",
    "sns": "AmazonSNS",
    "sqs": "AWSQueueService",
    "states": "AmazonStates",
    "storagegateway": "AWSStorageGateway",
    # ssm: só opsitem (classify_arn).
    "timestream": "AmazonTimestream",
    "transfer": "AWSTransfer",
    # "vpc-lattice" é resolvido por nome em `classify_arn` (código compartilhado).
    "workspaces": "AmazonWorkSpaces",
    # Amazon EVS não tem namespace próprio: só o EC2 subjacente conta.
    "mobiletargeting": "AmazonPinpoint",  # AWS End User Messaging (apps Pinpoint)
    "sms-voice": "AmazonPinpoint",  # AWS End User Messaging SMS/Voice
    "workspaces-instances": "AmazonWorkSpacesInstances",
    # Amazon Connect Decisions (ex-AWS Supply Chain): namespace "scn".
    "scn": "AWSSupplyChain",
    "gameliftstreams": "AmazonGameLiftStreams",
    "rtbfabric": "AWSRTBFabric",
    "ses": "AmazonSES",
}

# Tipos de recurso EC2 (parte após "arn:aws:ec2:region:account:") faturados
# sob o código "AmazonEC2". Qualquer outro tipo sob o namespace "ec2" é
# tratado como rede (código "AmazonVPC" / AWS Transit Gateway).
_EC2_COMPUTE_RESOURCE_TYPES = {
    "instance",
    "volume",
    "snapshot",
    "image",
    "network-interface",
    "elastic-ip",
    "launch-template",
    "spot-instances-request",
    "capacity-reservation",
    "key-pair",
    "placement-group",
    "reserved-instances",
    "security-group",
    "network-interface-attachment",
    # NAT Gateway é cobrado como EC2 ("EC2-Other").
    "natgateway",
}


def _service_by_code(services: list[Service], code: str) -> Optional[Service]:
    for svc in services:
        if svc.product_service_code == code:
            return svc
    return None


def _service_by_name(services: list[Service], name: str) -> Optional[Service]:
    for svc in services:
        if svc.name == name:
            return svc
    return None


def classify_arn(arn: str, services: list[Service]) -> Optional[Service]:
    """Retorna o Service (linha do CSV) correspondente a um ARN, ou None se
    o recurso não pertencer a nenhum serviço elegível conhecido."""
    parts = arn.split(":", 5)
    if len(parts) < 6 or parts[0] != "arn":
        logger.debug("ARN em formato inesperado, ignorando: %s", arn)
        return None
    namespace = parts[2]

    if namespace == "ec2":
        resource_part = parts[5]
        resource_type = resource_part.split("/", 1)[0].split(":", 1)[0]
        code = (
            "AmazonEC2"
            if resource_type in _EC2_COMPUTE_RESOURCE_TYPES
            else "AmazonVPC"
        )
        return _service_by_code(services, code)

    if namespace == "vpc-lattice":
        # Código compartilhado no CSV: resolve por nome.
        return _service_by_name(services, "Amazon VPC Lattice")

    if namespace == "ssm":
        # CSV: "OpsCenter only".
        resource_part = parts[5]
        resource_type = resource_part.split("/", 1)[0].split(":", 1)[0]
        if resource_type != "opsitem":
            return None
        return _service_by_code(services, "AWSSystemsManager")

    code = _NAMESPACE_TO_CODE.get(namespace)
    if code is None:
        return None
    return _service_by_code(services, code)
