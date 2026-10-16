"""Eventos de criação que a Etapa 3 observa, por serviço do CSV oficial.

Módulo puro (só dados e validação).

- `_EVENT_MAPPING` é indexado pelo `product_service_code` do CSV e cobre
  todo código, sem exceção; `None` = sem evento mapeado, com o motivo ao
  lado. `validate_against_csv()` (testado em `test_event_mapping.py`) falha
  se um código novo do CSV ficar sem entrada.
- `eventSource`/`eventName` foram conferidos contra os modelos do botocore
  (`test_event_parser_botocore.py`); o `eventSource` é o `signingName` do
  serviço, que nem sempre bate com o nome do pacote do SDK (ex.: CloudHSM,
  Keyspaces, Kinesis Analytics, Timestream).
- Serviços sem evento mapeado (`None`) têm o motivo ao lado: em geral,
  dezenas de operações `Create*` sem um recurso de cobrança óbvio, ou
  nenhuma API de criação (DRS, CodeStar descontinuado). A varredura
  (Etapa 1/4) cobre esses recursos.
- `build_event_patterns()` empacota os eventos em poucas regras do
  EventBridge, abaixo do limite padrão de tamanho de pattern;
  `sync_template_event_patterns()` grava os patterns no template regional.
"""
from __future__ import annotations

from dataclasses import dataclass

from .services import Service, load_services


@dataclass(frozen=True)
class CreationEventRule:
    """Um par (eventSource, eventName) de CloudTrail que representa a
    criação de um recurso elegível. `event_source` é o valor exato do campo
    `detail.eventSource` do evento (ex.: "ec2.amazonaws.com")."""

    event_source: str
    event_name: str


# product_service_code -> eventos de criação, ou `None` (sem mapeamento).
# "AmazonVPC" tem duas linhas no CSV (Transit Gateway e VPC Lattice): a
# entrada cobre as duas; o event_parser separa pelo namespace.
_EVENT_MAPPING: dict[str, tuple[CreationEventRule, ...] | None] = {
    "AmazonApiGateway": (
        CreationEventRule("apigateway.amazonaws.com", "CreateRestApi"),
        CreationEventRule("apigateway.amazonaws.com", "CreateApi"),  # HTTP/WebSocket API (v2) — mesma fonte que REST API
    ),
    # Fleets e image builders geram a cobrança por hora; stacks não têm custo.
    "AmazonAppStream": (
        CreationEventRule("appstream.amazonaws.com", "CreateFleet"),
        CreationEventRule("appstream.amazonaws.com", "CreateImageBuilder"),
    ),
    "AWSAppSync": (CreationEventRule("appsync.amazonaws.com", "CreateGraphqlApi"),),
    "AmazonAthena": (
        CreationEventRule("athena.amazonaws.com", "CreateWorkGroup"),
        CreationEventRule("athena.amazonaws.com", "CreateDataCatalog"),
    ),
    "AuroraDSQL": (CreationEventRule("dsql.amazonaws.com", "CreateCluster"),),
    "AWSBackup": (
        CreationEventRule("backup.amazonaws.com", "CreateBackupVault"),
        CreationEventRule("backup.amazonaws.com", "CreateBackupPlan"),
    ),
    "AmazonBedrock": (CreationEventRule("bedrock.amazonaws.com", "CreateInferenceProfile"),),
    # Componentes listados na nota do CSV.
    "AmazonBedrockAgentCore": (
        CreationEventRule("bedrock-agentcore.amazonaws.com", "CreateAgentRuntime"),
        CreationEventRule("bedrock-agentcore.amazonaws.com", "CreateBrowser"),
        CreationEventRule("bedrock-agentcore.amazonaws.com", "CreateCodeInterpreter"),
        CreationEventRule("bedrock-agentcore.amazonaws.com", "CreateGateway"),
        CreationEventRule("bedrock-agentcore.amazonaws.com", "CreateMemory"),
        CreationEventRule("bedrock-agentcore.amazonaws.com", "CreateWorkloadIdentity"),
    ),
    "AWSCertificateManager": (
        CreationEventRule("acm.amazonaws.com", "RequestCertificate"),
        CreationEventRule("acm-pca.amazonaws.com", "CreateCertificateAuthority"),
    ),
    "AmazonCloudDirectory": (CreationEventRule("clouddirectory.amazonaws.com", "CreateDirectory"),),
    "AWSCloudWAN": (
        CreationEventRule("networkmanager.amazonaws.com", "CreateCoreNetwork"),
        CreationEventRule("networkmanager.amazonaws.com", "CreateGlobalNetwork"),
    ),
    # O CloudTrail pode gravar o nome com sufixo de versão (normalizado no
    # parser). `...WithTags` é a operação usada pelo console.
    "AmazonCloudFront": (
        CreationEventRule("cloudfront.amazonaws.com", "CreateDistribution"),
        CreationEventRule("cloudfront.amazonaws.com", "CreateDistribution2020_05_31"),
        CreationEventRule("cloudfront.amazonaws.com", "CreateDistributionWithTags"),
        CreationEventRule("cloudfront.amazonaws.com", "CreateDistributionWithTags2020_05_31"),
    ),
    # eventSource "cloudhsm" (o pacote do SDK é cloudhsmv2).
    "CloudHSM": (CreationEventRule("cloudhsm.amazonaws.com", "CreateCluster"),),
    "AmazonCloudWatch": (CreationEventRule("logs.amazonaws.com", "CreateLogGroup"),),  # CSV: "Logs only"
    "CodeBuild": (CreationEventRule("codebuild.amazonaws.com", "CreateProject"),),
    "AWSCodePipeline": (CreationEventRule("codepipeline.amazonaws.com", "CreatePipeline"),),
    # CodeStar foi descontinuado (não cria mais projetos); o custo está nos
    # serviços por baixo, que têm mapeamento próprio.
    "AWSCodeStar": None,
    "AmazonCognito": (
        CreationEventRule("cognito-idp.amazonaws.com", "CreateUserPool"),
        CreationEventRule("cognito-identity.amazonaws.com", "CreateIdentityPool"),
    ),
    # Endpoints (cobrança por hora) e modelos customizados; jobs são efêmeros.
    "comprehend": (
        CreationEventRule("comprehend.amazonaws.com", "CreateEndpoint"),
        CreationEventRule("comprehend.amazonaws.com", "CreateDocumentClassifier"),
        CreationEventRule("comprehend.amazonaws.com", "CreateEntityRecognizer"),
    ),
    "AmazonConnect": (CreationEventRule("connect.amazonaws.com", "CreateInstance"),),
    # Amazon Connect Decisions (ex-AWS Supply Chain): namespace "scn".
    "AWSSupplyChain": (CreationEventRule("scn.amazonaws.com", "CreateInstance"),),
    "AmazonDAX": (CreationEventRule("dax.amazonaws.com", "CreateCluster"),),
    # AWS End User Messaging: SMS/voz e apps Pinpoint. O Pinpoint entra com
    # as duas fontes possíveis ("mobiletargeting" e "pinpoint").
    "AmazonPinpoint": (
        CreationEventRule("sms-voice.amazonaws.com", "CreateConfigurationSet"),
        CreationEventRule("sms-voice.amazonaws.com", "CreatePool"),
        CreationEventRule("sms-voice.amazonaws.com", "RequestPhoneNumber"),
        CreationEventRule("sms-voice.amazonaws.com", "RequestSenderId"),
        CreationEventRule("mobiletargeting.amazonaws.com", "CreateApp"),
        CreationEventRule("pinpoint.amazonaws.com", "CreateApp"),
    ),
    "AmazonGameLiftStreams": (
        CreationEventRule("gameliftstreams.amazonaws.com", "CreateStreamGroup"),
        CreationEventRule("gameliftstreams.amazonaws.com", "CreateApplication"),
    ),
    "AWSRTBFabric": (
        CreationEventRule("rtbfabric.amazonaws.com", "CreateRequesterGateway"),
        CreationEventRule("rtbfabric.amazonaws.com", "CreateResponderGateway"),
    ),
    # SES v1 e v2 usam o mesmo eventSource. Identidades, configuration sets e
    # tenants são os recursos tagueáveis por onde o envio passa.
    "AmazonSES": (
        CreationEventRule("ses.amazonaws.com", "CreateEmailIdentity"),
        CreationEventRule("ses.amazonaws.com", "VerifyEmailIdentity"),
        CreationEventRule("ses.amazonaws.com", "VerifyDomainIdentity"),
        CreationEventRule("ses.amazonaws.com", "CreateConfigurationSet"),
        CreationEventRule("ses.amazonaws.com", "CreateTenant"),
    ),
    # A resposta só traz o ID; o ARN é workspaceinstance/<id>.
    "AmazonWorkSpacesInstances": (CreationEventRule("workspaces-instances.amazonaws.com", "CreateWorkspaceInstance"),),
    "datapipeline": (CreationEventRule("datapipeline.amazonaws.com", "CreatePipeline"),),
    "AWSDatabaseMigrationSvc": (
        CreationEventRule("dms.amazonaws.com", "CreateReplicationInstance"),
        CreationEventRule("dms.amazonaws.com", "CreateReplicationTask"),
    ),
    # A cobrança é por task; agents e locations não são cobrados.
    "AWSDataSync": (
        CreationEventRule("datasync.amazonaws.com", "CreateTask"),
    ),
    # Farms, queues e fleets (a resposta só traz IDs).
    "AWSDeadlineCloud": (
        CreationEventRule("deadline.amazonaws.com", "CreateFarm"),
        CreationEventRule("deadline.amazonaws.com", "CreateQueue"),
        CreationEventRule("deadline.amazonaws.com", "CreateFleet"),
    ),
    "AWSDirectConnect": (
        CreationEventRule("directconnect.amazonaws.com", "CreateConnection"),
        CreationEventRule("directconnect.amazonaws.com", "CreateDirectConnectGateway"),
    ),
    "AWSDirectoryService": (
        CreationEventRule("ds.amazonaws.com", "CreateDirectory"),
        CreationEventRule("ds.amazonaws.com", "CreateMicrosoftAD"),
    ),
    # DocumentDB e Neptune usam o eventSource do RDS; o `engine` do evento
    # separa os dois (product_code_resolver em event_parser.py).
    "AmazonDocDB": (
        CreationEventRule("rds.amazonaws.com", "CreateDBCluster"),
        CreationEventRule("rds.amazonaws.com", "CreateDBInstance"),
        CreationEventRule("rds.amazonaws.com", "RestoreDBClusterFromSnapshot"),
        CreationEventRule("rds.amazonaws.com", "RestoreDBClusterToPointInTime"),
    ),
    "AmazonDynamoDB": (
        CreationEventRule("dynamodb.amazonaws.com", "CreateTable"),
        CreationEventRule("dynamodb.amazonaws.com", "RestoreTableFromBackup"),
        CreationEventRule("dynamodb.amazonaws.com", "RestoreTableToPointInTime"),
    ),
    # Instâncias entram pela regra nativa "running" (NATIVE_EVENT_PATTERNS),
    # que cobre qualquer origem (RunInstances, CreateFleet/Karpenter, Auto
    # Scaling, node groups do EKS) e não depende de trail.
    "AmazonEC2": (
        CreationEventRule("ec2.amazonaws.com", "CreateVolume"),
        CreationEventRule("ec2.amazonaws.com", "CreateSnapshot"),
        CreationEventRule("ec2.amazonaws.com", "CreateSnapshots"),
        CreationEventRule("ec2.amazonaws.com", "CopySnapshot"),
        CreationEventRule("ec2.amazonaws.com", "CreateNatGateway"),
        CreationEventRule("ec2.amazonaws.com", "AllocateAddress"),
    ),
    "AmazonECR": (CreationEventRule("ecr.amazonaws.com", "CreateRepository"),),
    "AmazonECS": (
        CreationEventRule("ecs.amazonaws.com", "CreateCluster"),
        CreationEventRule("ecs.amazonaws.com", "CreateService"),
    ),
    "AmazonEKS": (
        CreationEventRule("eks.amazonaws.com", "CreateCluster"),
        CreationEventRule("eks.amazonaws.com", "CreateNodegroup"),
    ),
    "AWSElasticBeanstalk": (
        CreationEventRule("elasticbeanstalk.amazonaws.com", "CreateApplication"),
        CreationEventRule("elasticbeanstalk.amazonaws.com", "CreateEnvironment"),
    ),
    # Source servers nascem na instalação do agente (sem API de criação):
    # ficam com a varredura. A replicação (EC2, EBS, snapshots) é pega pelas
    # regras de EC2; na origem, `stagingAreaTags` (recomendacoes.py).
    "AWSElasticDisasterRecovery": None,
    "AmazonEFS": (CreationEventRule("elasticfilesystem.amazonaws.com", "CreateFileSystem"),),
    "AmazonElastiCache": (
        CreationEventRule("elasticache.amazonaws.com", "CreateCacheCluster"),
        CreationEventRule("elasticache.amazonaws.com", "CreateReplicationGroup"),
        CreationEventRule("elasticache.amazonaws.com", "CreateServerlessCache"),
    ),
    "AWSElementalMediaConvert": (CreationEventRule("mediaconvert.amazonaws.com", "CreateQueue"),),
    "AWSElementalMediaLive": (CreationEventRule("medialive.amazonaws.com", "CreateChannel"),),
    "AWSElementalMediaPackage": (CreationEventRule("mediapackage.amazonaws.com", "CreateChannel"),),
    "ElasticMapReduce": (CreationEventRule("elasticmapreduce.amazonaws.com", "RunJobFlow"),),
    "AmazonFinSpace": (CreationEventRule("finspace.amazonaws.com", "CreateEnvironment"),),
    "AmazonFSx": (CreationEventRule("fsx.amazonaws.com", "CreateFileSystem"),),
    "AmazonGameLift": (CreationEventRule("gamelift.amazonaws.com", "CreateFleet"),),
    # O custo das execuções cai no job e no crawler.
    "AWSGlue": (
        CreationEventRule("glue.amazonaws.com", "CreateJob"),
        CreationEventRule("glue.amazonaws.com", "CreateCrawler"),
    ),
    "AmazonMedicalImaging": (CreationEventRule("medical-imaging.amazonaws.com", "CreateDatastore"),),
    "AmazonHealthLake": (CreationEventRule("healthlake.amazonaws.com", "CreateFHIRDatastore"),),
    # Billing groups atribuem o consumo das things dentro deles; pôr things
    # no grupo é configuração do cliente (recomendacoes.py).
    "AWSIoT": (
        CreationEventRule("iot.amazonaws.com", "CreateBillingGroup"),
    ),
    # Assets, asset models e gateways.
    "AWSIoTSiteWise": (
        CreationEventRule("iotsitewise.amazonaws.com", "CreateAsset"),
        CreationEventRule("iotsitewise.amazonaws.com", "CreateAssetModel"),
        CreationEventRule("iotsitewise.amazonaws.com", "CreateGateway"),
    ),
    "AmazonKendra": (CreationEventRule("kendra.amazonaws.com", "CreateIndex"),),
    "awskms": (CreationEventRule("kms.amazonaws.com", "CreateKey"),),
    # Amazon Keyspaces: control-plane REST, eventSource "cassandra".
    "AmazonMCS": (
        CreationEventRule("cassandra.amazonaws.com", "CreateKeyspace"),
        CreationEventRule("cassandra.amazonaws.com", "CreateTable"),
    ),
    # eventSource "kinesisanalytics" (o pacote do SDK é kinesisanalyticsv2).
    "AmazonKinesisAnalytics": (CreationEventRule("kinesisanalytics.amazonaws.com", "CreateApplication"),),
    "AmazonKinesisFirehose": (CreationEventRule("firehose.amazonaws.com", "CreateDeliveryStream"),),
    "AmazonKinesis": (CreationEventRule("kinesis.amazonaws.com", "CreateStream"),),
    "AmazonKinesisVideo": (CreationEventRule("kinesisvideo.amazonaws.com", "CreateStream"),),
    # O CloudTrail grava "CreateFunction20150331" (sufixo de versão).
    "AWSLambda": (
        CreationEventRule("lambda.amazonaws.com", "CreateFunction"),
        CreationEventRule("lambda.amazonaws.com", "CreateFunction20150331"),
    ),
    "AWSELB": (CreationEventRule("elasticloadbalancing.amazonaws.com", "CreateLoadBalancer"),),
    "AWSM2": (
        CreationEventRule("m2.amazonaws.com", "CreateEnvironment"),
        CreationEventRule("m2.amazonaws.com", "CreateApplication"),
    ),
    "AmazonMemoryDB": (CreationEventRule("memorydb.amazonaws.com", "CreateCluster"),),
    "AmazonMQ": (CreationEventRule("mq.amazonaws.com", "CreateBroker"),),
    "AmazonMSK": (CreationEventRule("kafka.amazonaws.com", "CreateClusterV2"),),
    # Mesmo mecanismo de "AmazonDocDB" acima (engine = neptune).
    "AmazonNeptune": (
        CreationEventRule("rds.amazonaws.com", "CreateDBCluster"),
        CreationEventRule("rds.amazonaws.com", "CreateDBInstance"),
        CreationEventRule("rds.amazonaws.com", "RestoreDBClusterFromSnapshot"),
        CreationEventRule("rds.amazonaws.com", "RestoreDBClusterToPointInTime"),
    ),
    "AWSNetworkFirewall": (CreationEventRule("network-firewall.amazonaws.com", "CreateFirewall"),),
    # Stores persistentes e workflows (annotation/variant stores não trazem
    # o ARN na resposta).
    "AmazonOmics": (
        CreationEventRule("omics.amazonaws.com", "CreateSequenceStore"),
        CreationEventRule("omics.amazonaws.com", "CreateReferenceStore"),
        CreationEventRule("omics.amazonaws.com", "CreateWorkflow"),
    ),
    # OpenSearch: nome novo e legado da operação, mesma fonte "es".
    "AmazonES": (
        CreationEventRule("es.amazonaws.com", "CreateDomain"),
        CreationEventRule("es.amazonaws.com", "CreateElasticsearchDomain"),
    ),
    "PaymentCryptography": (CreationEventRule("payment-cryptography.amazonaws.com", "CreateKey"),),
    # Users, data sources, data sets, dashboards e templates.
    "AmazonQuickSight": (
        CreationEventRule("quicksight.amazonaws.com", "RegisterUser"),
        CreationEventRule("quicksight.amazonaws.com", "CreateDataSource"),
        CreationEventRule("quicksight.amazonaws.com", "CreateDataSet"),
        CreationEventRule("quicksight.amazonaws.com", "CreateDashboard"),
        CreationEventRule("quicksight.amazonaws.com", "CreateTemplate"),
    ),
    "AmazonRedshift": (
        CreationEventRule("redshift.amazonaws.com", "CreateCluster"),
        CreationEventRule("redshift-serverless.amazonaws.com", "CreateWorkgroup"),
    ),
    "AmazonRDS": (
        CreationEventRule("rds.amazonaws.com", "CreateDBInstance"),
        CreationEventRule("rds.amazonaws.com", "CreateDBCluster"),
        # Restores criam instância/cluster sem passar por Create*
        CreationEventRule("rds.amazonaws.com", "RestoreDBInstanceFromDBSnapshot"),
        CreationEventRule("rds.amazonaws.com", "RestoreDBInstanceToPointInTime"),
        CreationEventRule("rds.amazonaws.com", "RestoreDBInstanceFromS3"),
        CreationEventRule("rds.amazonaws.com", "CreateDBInstanceReadReplica"),
        CreationEventRule("rds.amazonaws.com", "RestoreDBClusterFromSnapshot"),
        CreationEventRule("rds.amazonaws.com", "RestoreDBClusterToPointInTime"),
        CreationEventRule("rds.amazonaws.com", "RestoreDBClusterFromS3"),
        # Snapshots manuais (armazenamento de backup é cobrado)
        CreationEventRule("rds.amazonaws.com", "CreateDBSnapshot"),
        CreationEventRule("rds.amazonaws.com", "CreateDBClusterSnapshot"),
    ),
    "AWSResilienceHub": (CreationEventRule("resiliencehub.amazonaws.com", "CreateApp"),),
    "AmazonRoute53": (CreationEventRule("route53.amazonaws.com", "CreateHostedZone"),),
    "AmazonS3": (CreationEventRule("s3.amazonaws.com", "CreateBucket"),),
    "AmazonGlacier": (CreationEventRule("glacier.amazonaws.com", "CreateVault"),),
    # Recursos persistentes com cobrança por hora; jobs são efêmeros
    # (terminam antes do debounce) e recebem a tag na origem.
    "AmazonSageMaker": (
        CreationEventRule("sagemaker.amazonaws.com", "CreateEndpoint"),
        CreationEventRule("sagemaker.amazonaws.com", "CreateNotebookInstance"),
        CreationEventRule("sagemaker.amazonaws.com", "CreateDomain"),
        CreationEventRule("sagemaker.amazonaws.com", "CreateApp"),
    ),
    "AWSSecretsManager": (CreationEventRule("secretsmanager.amazonaws.com", "CreateSecret"),),
    # O recurso é o hub da conta/região (ARN fixo hub/default).
    "AWSSecurityHub": (
        CreationEventRule("securityhub.amazonaws.com", "EnableSecurityHub"),
    ),
    "AmazonSNS": (CreationEventRule("sns.amazonaws.com", "CreateTopic"),),
    "AWSQueueService": (CreationEventRule("sqs.amazonaws.com", "CreateQueue"),),
    "AmazonStates": (CreationEventRule("states.amazonaws.com", "CreateStateMachine"),),
    "AWSStorageGateway": (
        CreationEventRule("storagegateway.amazonaws.com", "CreateNFSFileShare"),
        CreationEventRule("storagegateway.amazonaws.com", "CreateSMBFileShare"),
        CreationEventRule("storagegateway.amazonaws.com", "CreateStorediSCSIVolume"),
        CreationEventRule("storagegateway.amazonaws.com", "CreateCachediSCSIVolume"),
    ),
    "AWSSystemsManager": (CreationEventRule("ssm.amazonaws.com", "CreateOpsItem"),),  # CSV: "OpsCenter only"
    # eventSource "timestream".
    "AmazonTimestream": (CreationEventRule("timestream.amazonaws.com", "CreateDatabase"),),
    "AWSTransfer": (CreationEventRule("transfer.amazonaws.com", "CreateServer"),),
    "AmazonVPC": (
        CreationEventRule("ec2.amazonaws.com", "CreateTransitGateway"),
        CreationEventRule("ec2.amazonaws.com", "CreateVpc"),
        CreationEventRule("vpc-lattice.amazonaws.com", "CreateServiceNetwork"),
    ),
    "AmazonWorkSpaces": (CreationEventRule("workspaces.amazonaws.com", "CreateWorkspaces"),),
}


def load_event_mapping() -> dict[str, tuple[CreationEventRule, ...] | None]:
    """Cópia da tabela — nunca devolve a referência interna, para que quem
    chamar não possa mutar `_EVENT_MAPPING` por engano."""
    return dict(_EVENT_MAPPING)


def mapped_services() -> dict[str, tuple[CreationEventRule, ...]]:
    """Só os serviços com mapeamento definido (exclui as entradas `None`)."""
    return {code: rules for code, rules in _EVENT_MAPPING.items() if rules is not None}


def unmapped_service_codes() -> list[str]:
    """`product_service_code`s presentes na tabela, mas ainda sem mapeamento
    de evento (`None`) — ver docstring do módulo."""
    return sorted(code for code, rules in _EVENT_MAPPING.items() if rules is None)


def validate_against_csv(services: list[Service] | None = None) -> list[str]:
    """Códigos do CSV sem entrada nesta tabela (nem mapeada, nem `None`).
        Lista vazia é o estado esperado (travado em `test_event_mapping.py`)."""
    services = services if services is not None else load_services()
    csv_codes = {s.product_service_code for s in services}
    return sorted(csv_codes - set(_EVENT_MAPPING.keys()))


def build_event_pattern() -> dict:
    """Pattern único com todos os serviços (um `$or` por `event_source`).

        Só para referência/debug: já passa da quota de 2.048 caracteres. As
        regras usam `build_event_patterns()`."""
    return {"detail-type": [_DETAIL_TYPE], "$or": _alternativas_por_fonte()}


_DETAIL_TYPE = "AWS API Call via CloudTrail"

# Limite padrão do tamanho de event pattern do EventBridge: 2.048 caracteres.
# Fica-se abaixo dele (várias regras) para não depender de aumento de quota
# em cada conta de cliente.
_MAX_EVENT_PATTERN_CHARS = 2000


def _alternativas_por_fonte() -> list[dict]:
    por_fonte: dict[str, set[str]] = {}
    for rules in mapped_services().values():
        for rule in rules:
            por_fonte.setdefault(rule.event_source, set()).add(rule.event_name)
    return [
        {
            "source": [f"aws.{event_source.split('.')[0]}"],
            "detail": {
                "eventName": sorted(event_names),
                # Chamada que falhou também gera evento (com errorCode);
                # o recurso não existe, então é filtrada.
                "errorCode": [{"exists": False}],
            },
        }
        for event_source, event_names in sorted(por_fonte.items())
    ]


def build_event_patterns(max_chars: int = _MAX_EVENT_PATTERN_CHARS) -> list[dict]:
    """Divide as alternativas em patterns de até `max_chars` (JSON compacto).

        Bin-packing guloso sobre a ordem estável de `event_source`. Cada pattern
        alimenta uma regra `Etapa3EventRule<N>` do template regional."""
    alternativas = _alternativas_por_fonte()
    patterns: list[dict] = []
    atual: list[dict] = []

    def _tamanho(alts: list[dict]) -> int:
        import json

        return len(json.dumps({"detail-type": [_DETAIL_TYPE], "$or": alts}, separators=(",", ":")))

    for alt in alternativas:
        candidato = atual + [alt]
        if atual and _tamanho(candidato) > max_chars:
            patterns.append({"detail-type": [_DETAIL_TYPE], "$or": atual})
            atual = [alt]
        else:
            atual = candidato
    if atual:
        patterns.append({"detail-type": [_DETAIL_TYPE], "$or": atual})
    return patterns


# Eventos nativos de serviço (não dependem de trail); uma regra própria
# cada no template regional.
NATIVE_EVENT_PATTERNS: dict[str, dict] = {
    "ec2_instance_running": {
        "source": ["aws.ec2"],
        "detail-type": ["EC2 Instance State-change Notification"],
        "detail": {"state": ["running"]},
    },
    # No Fargate o custo fica na task (no launch type EC2, na instância).
    # Sem debounce: a task pode durar menos que ele.
    "ecs_task_running": {
        "source": ["aws.ecs"],
        "detail-type": ["ECS Task State Change"],
        "detail": {"lastStatus": ["RUNNING"], "desiredStatus": ["RUNNING"], "launchType": ["FARGATE"]},
    },
}


# Etapa 4: remoção/substituição de tags de qualquer serviço (o handler
# descarta o que está fora do CSV). `prefix` cobre nomes com sufixo de versão.
DRIFT_EVENT_PATTERN: dict = {
    "detail-type": ["AWS API Call via CloudTrail"],
    "detail": {
        "eventName": [
            {"prefix": "Untag"},
            {"prefix": "RemoveTags"},
            "DeleteTags",
            "PutBucketTagging",
            "DeleteBucketTagging",
            "ChangeTagsForResource",
            "UpdateTagsForResource",
        ],
        "errorCode": [{"exists": False}],
    },
}


def _regenerate_infra_files() -> None:
    """Regrava `infra/event_pattern.<N>.generated.json` a partir de
        `_EVENT_MAPPING`; rodar sempre que a tabela mudar."""
    import glob
    import json
    from pathlib import Path

    infra_dir = Path(__file__).parent / "infra"
    for antigo in glob.glob(str(infra_dir / "event_pattern.*.generated.json")):
        Path(antigo).unlink()

    patterns = build_event_patterns()
    for i, pattern in enumerate(patterns):
        destino = infra_dir / f"event_pattern.{i}.generated.json"
        destino.write_text(json.dumps(pattern, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"{len(patterns)} arquivo(s) de pattern gerado(s) em {infra_dir}")
    template_path = infra_dir / "template-regional.yaml"
    n = sync_template_event_patterns(template_path, patterns)
    print(f"{template_path.name}: {n} bloco(s) EventPattern sincronizado(s)")
    print(
        "IMPORTANTE: se o número de patterns mudou, ainda é preciso adicionar/remover "
        "manualmente a regra Etapa3EventRule<N> no template regional; "
        "test_infra_event_patterns.py aponta quando."
    )


def _pattern_to_flow_yaml(pattern: dict, indent: str = "        ") -> str:
    """Serializa um pattern no MESMO estilo YAML já usado no template
    (flow style por alternativa) — assim o diff do template fica legível."""
    import json

    linhas = [f"{indent}detail-type:", f'{indent}  - "{pattern["detail-type"][0]}"', f"{indent}$or:"]
    for alt in pattern["$or"]:
        nomes = ", ".join(json.dumps(n) for n in alt["detail"]["eventName"])
        linhas.append(f'{indent}  - source: [{json.dumps(alt["source"][0])}]')
        linhas.append(f"{indent}    detail: {{eventName: [{nomes}], errorCode: [{{exists: false}}]}}")
    return "\n".join(linhas) + "\n"


def sync_template_event_patterns(template_path, patterns: list[dict]) -> int:
    """Substitui, no texto do template, o bloco `EventPattern:` de cada
    `Etapa3EventRule<N>` pelo pattern gerado correspondente (até o próximo
    `      Targets:`). Não cria nem remove regras — só sincroniza as que já
    existem. Devolve quantos blocos foram substituídos."""
    import re as _re
    from pathlib import Path

    texto = Path(template_path).read_text(encoding="utf-8")
    substituidos = 0
    for i, pattern in enumerate(patterns):
        regex = _re.compile(
            rf"(  Etapa3EventRule{i}:\n(?:.*\n)*?      EventPattern:\n)(?:.*\n)*?(      Targets:)",
        )
        novo, n = regex.subn(lambda m: m.group(1) + _pattern_to_flow_yaml(pattern) + m.group(2), texto, count=1)
        if n:
            texto = novo
            substituidos += 1
    Path(template_path).write_text(texto, encoding="utf-8")
    return substituidos


if __name__ == "__main__":
    _regenerate_infra_files()
