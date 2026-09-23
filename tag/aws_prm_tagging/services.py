"""Lista de serviços elegíveis ao PRM (Resource Tagging) e classificação de ARNs.

A lista de serviços é extraída integralmente do CSV oficial
(`data/resource-tagging-included-services.csv`, fonte de verdade fornecida pela
AWS) — nunca hardcoded aqui. O que este módulo adiciona é um mapeamento técnico
entre o "namespace" de serviço usado nos ARNs (ex.: "ec2", "s3", "rds") e o
"Product Service Code" da linha correspondente do CSV, necessário porque a
Resource Groups Tagging API devolve ARNs, não códigos de billing, e o CSV não
traz essa correspondência.

Esse mapeamento é heurístico/best-effort e tem duas limitações conhecidas,
documentadas para os próximos estágios:

1. Namespace "rds" é compartilhado por Amazon RDS, Aurora, Amazon DocumentDB e
   Amazon Neptune (todos usam ARNs `arn:aws:rds:...`). Sem chamar a API
   específica de cada engine (para inspecionar o atributo Engine), não é
   possível diferenciar com certeza — todos os recursos "rds" são
   classificados como "Amazon Relational Database Service (RDS)".
2. Namespace "ec2" é compartilhado por Amazon EC2 e pelos recursos de rede que
   a AWS fatura sob o código "AmazonVPC" (Transit Gateway, VPC, Peering,
   Direct Connect Gateway). Usamos o tipo de recurso dentro do ARN
   (ex.: "instance", "volume" vs. "vpc", "transit-gateway") para desambiguar
   na função `classify_arn`.

O código "AmazonVPC" também é compartilhado no CSV por DUAS linhas ("AWS
Transit Gateway" e "Amazon VPC Lattice") — ao contrário da limitação nº 2
acima, aqui não é ambiguidade real: o namespace "vpc-lattice" do ARN já
identifica o recurso sem dúvida nenhuma. `classify_arn` desambigua isso por
nome (`_service_by_name`) em vez de por código, para não deixar
`_service_by_code` pegar sempre a primeira linha com esse código (que
rotularia todo recurso VPC Lattice como "AWS Transit Gateway" no
relatório — a tag aplicada não mudaria, só o campo "servico").
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


# namespace do ARN (arn:aws:<namespace>:...) -> Product Service Code do CSV.
# Serviços tratados via lógica dedicada (Bedrock, EKS) são deliberadamente
# omitidos daqui e nunca classificados pela descoberta genérica.
_NAMESPACE_TO_CODE: dict[str, str] = {
    "apigateway": "AmazonApiGateway",
    "appstream": "AmazonAppStream",
    "appsync": "AWSAppSync",
    "athena": "AmazonAthena",
    "dsql": "AuroraDSQL",
    "backup": "AWSBackup",
    # "bedrock" (foundation-model / inference-profile) fica de fora de propósito:
    # só application inference profiles contam para atribuição e não são bem
    # cobertos pela Resource Groups Tagging API — tratados via lógica dedicada
    # em resource_discovery.py. AgentCore não é caso especial no guia e usa
    # tagging genérico normalmente.
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
    # "dax" fica de fora de propósito: o CSV não tem nenhum "Product Service
    # Code" para DAX — a própria linha de DynamoDB traz a nota "Excludes DAX",
    # ou seja, a AWS exclui DAX da elegibilidade do PRM explicitamente.
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
    "ssm": "AWSSystemsManager",
    "timestream": "AmazonTimestream",
    "transfer": "AWSTransfer",
    # "vpc-lattice" fica de fora de propósito: compartilha o código
    # "AmazonVPC" com "AWS Transit Gateway" no CSV — tratado à parte em
    # `classify_arn` via `_service_by_name`, não por este mapeamento de
    # código (ver docstring do módulo).
    "workspaces": "AmazonWorkSpaces",
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
        # Ver docstring do módulo — namespace inequívoco, mas o código
        # "AmazonVPC" é compartilhado com "AWS Transit Gateway" no CSV, e
        # _service_by_code pegaria sempre a primeira linha com esse código.
        return _service_by_name(services, "Amazon VPC Lattice")

    code = _NAMESPACE_TO_CODE.get(namespace)
    if code is None:
        return None
    return _service_by_code(services, code)
