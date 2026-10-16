"""Mapeamento entre serviços do CSV oficial do PRM e os eventos de criação de
recurso que a Etapa 3 (automação contínua via EventBridge) observa para cada
um.

Módulo puro: nenhuma chamada de rede, sem boto3. Só dados + validação.

## Fonte da verdade

A lista de serviços em escopo continua sendo exclusivamente
`services.load_services()` (o CSV oficial) — este módulo NUNCA declara uma
segunda lista de serviços própria. `_EVENT_MAPPING` abaixo é indexado por
`product_service_code` (a mesma chave que `services.Service` usa) e cobre
TODO código do CSV, sem exceção — só que o valor pode ser `None` quando
ainda não há um mapeamento de evento definido para aquele serviço.

`validate_against_csv()` é chamada por um teste (`test_event_mapping.py`)
que falha se algum `product_service_code` do CSV não tiver NENHUMA entrada
aqui (nem mapeada, nem `None` explícito) — a tabela nunca pode divergir
silenciosamente do CSV. Isso cobre o caso de "a AWS adicionou um serviço
novo ao CSV do PRM": o teste quebra, force alguém a decidir o que fazer
antes de mergear (mapear, ou marcar `None` conscientemente).

O que fazer quando a automação já está em produção e o CSV ganha um serviço
novo sem que a tabela tenha sido atualizada ainda: o subcomando
`verificar-csv` (csv_check.py, Etapa 4) compara a tabela oficial com o CSV
empacotado e aponta os códigos novos sem mapeamento — rodar periodicamente,
de forma central. Dentro da conta do cliente, o comportamento continua
silencioso: um serviço sem mapeamento simplesmente não gera
nenhum evento de criação observado pela Etapa 3 (o event pattern do
EventBridge, construído por `build_event_patterns()`, só cobre os serviços
mapeados) — a Etapa 4 (varredura periódica, fora do escopo deste módulo)
continua sendo o backstop para qualquer lacuna de cobertura aqui.

## Como `eventSource`/`eventName` foram verificados

Ao contrário de uma primeira versão deste módulo (que vinha só de memória),
`eventSource` e `eventName` abaixo foram checados contra os modelos de
serviço reais do `botocore` instalado localmente (`botocore.session.
get_service_model(...)`) — que dá o nome exato de cada operação de API e o
`signingName`/`endpointPrefix` real de cada serviço (a base do `eventSource`
que o CloudTrail usa). Isso já corrigiu 3 erros reais que a versão anterior
tinha (fonte errada para `AmazonKinesisAnalytics`, `AmazonTimestream` e
`CloudHSM` — os três agora corrigidos) e confirmou que `AmazonDocDB`/
`AmazonNeptune` genuinamente não são distinguíveis por evento (os pacotes
`docdb`/`neptune` do botocore têm `endpointPrefix`/`signingName` = `rds` —
usam o MESMO endpoint que RDS/Aurora, não um serviço à parte).

**O que o botocore NÃO confirma**: a forma exata de `responseElements`/
`requestParameters` que o CloudTrail de fato grava — o modelo do botocore
descreve o shape "lógico" da API (usado pelos SDKs), não necessariamente a
serialização exata que aparece no evento do CloudTrail. Para a maioria dos
serviços (protocolo `json`/`rest-json` — a maioria dos mapeados aqui), a
experiência documentada é que o CloudTrail preserva os nomes de campo tal
como a própria API os devolve, e nesses casos o shape do botocore é uma
fonte confiável. Para os poucos serviços de protocolo `query`/`ec2`/
`rest-xml` (`ec2`, `rds`, `elasticache`, `redshift`, `elasticbeanstalk`,
`sns`, `s3`, `route53`, `cloudfront`), o CloudTrail historicamente aplica
uma conversão XML→JSON com convenções próprias (ex.: primeira letra
minúscula, e no caso específico do EC2 um empacotamento adicional
`"xSet": {"items": [...]}` para listas) — `event_parser.py` documenta,
extractor por extractor, onde essa incerteza extra se aplica e usa uma
leitura tolerante a variação de capitalização nesses casos específicos.
**8 dos 9 desses serviços já foram validados contra um evento CloudTrail
real capturado em sandbox** (2026-09-23/24, achou e corrigiu 3 bugs reais)
— só CloudFront segue sem evento real capturado — ver
[docs/melhorias-futuras.md](docs/melhorias-futuras.md) e
[test/manual-live-etapa3/README.md](test/manual-live-etapa3/README.md).

## Cobertura

Hoje cobre ~69 dos 85 `product_service_code` distintos do CSV. Os que
ficam `None` têm o motivo documentado inline — quase todos por terem um
número grande de operações `Create*` sem "o" recurso óbvio para fins de
tagueamento PRM (contagem real, verificada via botocore, não estimativa):
`AmazonAppStream` (17), `AmazonBedrockAgentCore` (29),
`AmazonSageMaker` (72!), `AmazonQuickSight` (34), `AWSGlue` (31),
`AWSIoT` (32), `AWSDataSync` (13), `AWSDeadlineCloud` (13),
`AWSSecurityHub` (11, operações de nível de conta, não "criação de
recurso" no sentido usual), `AmazonOmics` (12),
`AWSElasticDisasterRecovery` (6, nenhuma é uma criação de recurso "normal"
— é replicação contínua), `comprehend` (5, mas jobs/endpoints, sem recurso
persistente óbvio) e `AWSCodeStar` (serviço descontinuado, sem definição no
botocore instalado).

Três achados que ENTRARAM na cobertura, ao contrário do que uma versão
anterior deste módulo assumia sem checar: `AmazonMCS` (Amazon Keyspaces) —
o control-plane é uma API REST normal (`CreateKeyspace`/`CreateTable`), não
CQL como a versão anterior deste módulo assumia por engano; `AuroraDSQL` e
`AWSM2` — ambos têm um número pequeno de operações `Create*` com ARN
direto na resposta.
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


# product_service_code (CSV) -> regras de evento de criação, ou `None`
# quando ainda não há mapeamento definido (ver docstring do módulo).
#
# Nota sobre "AmazonVPC": o CSV tem DUAS linhas com este código ("AWS
# Transit Gateway" e "Amazon VPC Lattice" — ver services.py). A entrada
# abaixo cobre a UNIÃO dos eventos relevantes das duas; qual delas um
# evento específico pertence é resolvido na extração (event_parser.py),
# pelo mesmo princípio de desambiguação por namespace que
# `services.classify_arn` já usa.
_EVENT_MAPPING: dict[str, tuple[CreationEventRule, ...] | None] = {
    "AmazonApiGateway": (
        CreationEventRule("apigateway.amazonaws.com", "CreateRestApi"),
        CreationEventRule("apigateway.amazonaws.com", "CreateApi"),  # HTTP/WebSocket API (v2) — mesma fonte que REST API
    ),
    # Item 10 (aprovado 2026-09-30): fleets e image builders geram a cobrança
    # por hora de instância; stacks não têm custo.
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
    # Item 10: os componentes que a própria nota do CSV lista. signingName do
    # plano de controle = "bedrock-agentcore".
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
    # O CloudTrail grava o eventName do CloudFront com sufixo de versão da
    # API em várias operações — listamos as duas formas (o parser normaliza
    # o sufixo, ver event_parser.normalize_event_name). `...WithTags` é a
    # operação que o console usa.
    "AmazonCloudFront": (
        CreationEventRule("cloudfront.amazonaws.com", "CreateDistribution"),
        CreationEventRule("cloudfront.amazonaws.com", "CreateDistribution2020_05_31"),
        CreationEventRule("cloudfront.amazonaws.com", "CreateDistributionWithTags"),
        CreationEventRule("cloudfront.amazonaws.com", "CreateDistributionWithTags2020_05_31"),
    ),
    # CORRIGIDO: fonte real é "cloudhsm" (confirmado via signingName do
    # botocore) — uma versão anterior deste módulo usava
    # "cloudhsmv2.amazonaws.com", errado (esse é só o nome do pacote SDK).
    "CloudHSM": (CreationEventRule("cloudhsm.amazonaws.com", "CreateCluster"),),
    "AmazonCloudWatch": (CreationEventRule("logs.amazonaws.com", "CreateLogGroup"),),  # CSV: "Logs only"
    "CodeBuild": (CreationEventRule("codebuild.amazonaws.com", "CreateProject"),),
    "AWSCodePipeline": (CreationEventRule("codepipeline.amazonaws.com", "CreatePipeline"),),
    # CodeStar: descontinuado em 31/07/2024 — desde então não é possível
    # criar projetos, então NUNCA haverá evento de criação para observar.
    # Projetos antigos que ainda existam são descobertos pela Etapa 1
    # (namespace "codestar" em services.py). O CodeStar em si nunca foi
    # cobrado: o custo está nos serviços por baixo (CodePipeline, CodeBuild,
    # EC2...), que têm mapeamento próprio. Sem definição no botocore atual.
    "AWSCodeStar": None,
    "AmazonCognito": (
        CreationEventRule("cognito-idp.amazonaws.com", "CreateUserPool"),
        CreationEventRule("cognito-identity.amazonaws.com", "CreateIdentityPool"),
    ),
    # Item 10: endpoints (cobrados por hora provisionada) e modelos customizados.
    # Jobs assíncronos são efêmeros — recomendação de tag na origem.
    "comprehend": (
        CreationEventRule("comprehend.amazonaws.com", "CreateEndpoint"),
        CreationEventRule("comprehend.amazonaws.com", "CreateDocumentClassifier"),
        CreationEventRule("comprehend.amazonaws.com", "CreateEntityRecognizer"),
    ),
    "AmazonConnect": (CreationEventRule("connect.amazonaws.com", "CreateInstance"),),
    # --- Serviços que entraram no CSV em 2026-09-30 (reference/FONTE-CSV.md).
    # eventSource = signingName do botocore (mesmo critério do resto da
    # tabela); todos ainda SEM evento real capturado em sandbox. ---
    # Amazon Connect Decisions (ex-AWS Supply Chain): namespace/signing "scn".
    "AWSSupplyChain": (CreationEventRule("scn.amazonaws.com", "CreateInstance"),),
    "AmazonDAX": (CreationEventRule("dax.amazonaws.com", "CreateCluster"),),
    # AWS End User Messaging: SMS/voz (sms-voice) + apps Pinpoint. O
    # signingName do Pinpoint é "mobiletargeting", mas o endpoint é
    # "pinpoint" — as duas fontes entram até um evento real confirmar qual
    # o CloudTrail usa (regra a mais que nunca casa não tem custo).
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
    # SES v1 (query) e v2 (rest-json) compartilham o eventSource
    # "ses.amazonaws.com". Identidades e configuration sets são os recursos
    # taggeáveis por onde o envio passa; tenants (v2) já têm ARN próprio.
    # ATENÇÃO: o SES cobra por mensagem — se a tag nesses recursos gera
    # atribuição é pergunta para a AWS (mesmo caso de QuickSight/IoT, ver
    # docs/plano-finalizacao.md). Taguear não tem efeito colateral.
    "AmazonSES": (
        CreationEventRule("ses.amazonaws.com", "CreateEmailIdentity"),
        CreationEventRule("ses.amazonaws.com", "VerifyEmailIdentity"),
        CreationEventRule("ses.amazonaws.com", "VerifyDomainIdentity"),
        CreationEventRule("ses.amazonaws.com", "CreateConfigurationSet"),
        CreationEventRule("ses.amazonaws.com", "CreateTenant"),
    ),
    # Formato de ARN confirmado na Service Authorization Reference
    # (workspaceinstance/<id>); a resposta só traz o ID.
    "AmazonWorkSpacesInstances": (CreationEventRule("workspaces-instances.amazonaws.com", "CreateWorkspaceInstance"),),
    "datapipeline": (CreationEventRule("datapipeline.amazonaws.com", "CreatePipeline"),),
    "AWSDatabaseMigrationSvc": (
        CreationEventRule("dms.amazonaws.com", "CreateReplicationInstance"),
        CreationEventRule("dms.amazonaws.com", "CreateReplicationTask"),
    ),
    # Item 10: a cobrança é por task (dados transferidos); agents/locations não são cobrados.
    "AWSDataSync": (
        CreationEventRule("datasync.amazonaws.com", "CreateTask"),
    ),
    # Item 10: farms, queues e fleets. A resposta só traz IDs; formato de
    # ARN confirmado na Service Authorization Reference da AWS.
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
    # DocumentDB e Neptune: CONFIRMADO via botocore (não só suposto) que os
    # pacotes SDK "docdb"/"neptune" têm endpointPrefix/signingName = "rds" —
    # usam o MESMO endpoint/eventSource que RDS/Aurora. Não há como
    # diferenciar por evento sem uma chamada de API extra (mesma ambiguidade
    # documentada em services.py para o passo genérico da Etapa 1).
    # ATUALIZADO (2026-09-30): DocumentDB/Neptune continuam sem eventSource
    # próprio, mas o `requestParameters.engine` do evento do RDS diferencia
    # (`docdb`/`neptune`) sem nenhuma chamada de API extra — os mesmos
    # eventos do RDS agora são rotulados corretamente pelo
    # `product_code_resolver` em event_parser.py. Antes disso esses
    # recursos JÁ eram tagueados (pela regra do RDS), só com o rótulo
    # "Amazon RDS" no relatório.
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
    # Instâncias NÃO entram mais por CloudTrail (`RunInstances`): a regra
    # nativa "EC2 Instance State-change Notification" (ver
    # NATIVE_EVENT_PATTERNS abaixo) cobre instâncias de QUALQUER origem —
    # RunInstances, CreateFleet/Karpenter, Auto Scaling com
    # MixedInstancesPolicy/Spot, node groups do EKS — e não depende de trail.
    # Manter as duas geraria processamento/notificação em dobro.
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
    # Item 10 (validado 2026-09-30): source servers + recursos de replicação.
    # Source servers não têm API pública de criação (nascem na instalação do
    # agente) -> varredura (Etapa 1/4). Os recursos de replicação (EC2, EBS,
    # snapshots da staging area) já são pegos pela regra nativa de EC2 e
    # pelos eventos de volume/snapshot. Caminho na origem: `stagingAreaTags`
    # do replication configuration template -> recomendacoes.py.
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
    # Item 10: o custo das execuções cai no job (persistente) e no crawler.
    "AWSGlue": (
        CreationEventRule("glue.amazonaws.com", "CreateJob"),
        CreationEventRule("glue.amazonaws.com", "CreateCrawler"),
    ),
    "AmazonMedicalImaging": (CreationEventRule("medical-imaging.amazonaws.com", "CreateDatastore"),),
    "AmazonHealthLake": (CreationEventRule("healthlake.amazonaws.com", "CreateFHIRDatastore"),),
    # Item 10 (validado 2026-09-30): billing groups — existem justamente para
    # atribuir o consumo das things que estão dentro deles. Colocar things no
    # grupo é configuração do cliente (recomendação), não tagueamento.
    "AWSIoT": (
        CreationEventRule("iot.amazonaws.com", "CreateBillingGroup"),
    ),
    # Item 10 (validado 2026-09-30): assets, asset models e gateways.
    "AWSIoTSiteWise": (
        CreationEventRule("iotsitewise.amazonaws.com", "CreateAsset"),
        CreationEventRule("iotsitewise.amazonaws.com", "CreateAssetModel"),
        CreationEventRule("iotsitewise.amazonaws.com", "CreateGateway"),
    ),
    "AmazonKendra": (CreationEventRule("kendra.amazonaws.com", "CreateIndex"),),
    "awskms": (CreationEventRule("kms.amazonaws.com", "CreateKey"),),
    # CORRIGIDO: Amazon Keyspaces (Cassandra) tem sim um control-plane REST
    # normal (achado verificando o botocore) — uma versão anterior deste
    # módulo assumia, sem checar, que só existia a API CQL (data-plane) e
    # por isso não tinha como gerar evento CloudTrail; errado. A fonte é
    # "cassandra.amazonaws.com" (confirmado via signingName/endpointPrefix
    # do pacote "keyspaces" do botocore — o nome do pacote SDK não bate com
    # o nome real do serviço no CloudTrail, mesmo padrão de armadilha que
    # já pegou CloudHSM acima).
    "AmazonMCS": (
        CreationEventRule("cassandra.amazonaws.com", "CreateKeyspace"),
        CreationEventRule("cassandra.amazonaws.com", "CreateTable"),
    ),
    # CORRIGIDO: fonte real é "kinesisanalytics" (confirmado via
    # signingName do botocore), não "kinesisanalyticsv2.amazonaws.com" como
    # uma versão anterior deste módulo tinha — o "v2" é só o nome do pacote
    # SDK que expõe a API mais nova, o serviço/eventSource no CloudTrail é
    # o mesmo de sempre.
    "AmazonKinesisAnalytics": (CreationEventRule("kinesisanalytics.amazonaws.com", "CreateApplication"),),
    "AmazonKinesisFirehose": (CreationEventRule("firehose.amazonaws.com", "CreateDeliveryStream"),),
    "AmazonKinesis": (CreationEventRule("kinesis.amazonaws.com", "CreateStream"),),
    "AmazonKinesisVideo": (CreationEventRule("kinesisvideo.amazonaws.com", "CreateStream"),),
    # BUG CORRIGIDO: o CloudTrail grava "CreateFunction20150331" (sufixo de
    # versão da API, documentado pela AWS) — com só "CreateFunction" a regra
    # nunca casava. Mantemos os dois nomes por segurança.
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
    # Item 10: stores persistentes e workflows. Annotation/variant stores
    # ficam de fora por enquanto: a resposta não traz o ARN do store.
    "AmazonOmics": (
        CreationEventRule("omics.amazonaws.com", "CreateSequenceStore"),
        CreationEventRule("omics.amazonaws.com", "CreateReferenceStore"),
        CreationEventRule("omics.amazonaws.com", "CreateWorkflow"),
    ),
    # Amazon OpenSearch Service (inclui o legado "Elasticsearch Service" —
    # dois nomes de operação diferentes, mesma fonte "es.amazonaws.com"
    # (confirmado: os pacotes botocore "es" e "opensearch" têm o mesmo
    # endpointPrefix "es") — cobre chamadas feitas com SDK antigo
    # (CreateElasticsearchDomain) e novo (CreateDomain).
    "AmazonES": (
        CreationEventRule("es.amazonaws.com", "CreateDomain"),
        CreationEventRule("es.amazonaws.com", "CreateElasticsearchDomain"),
    ),
    "PaymentCryptography": (CreationEventRule("payment-cryptography.amazonaws.com", "CreateKey"),),
    # Item 10 (validado 2026-09-30): users, data sources, data sets,
    # dashboards e templates (a doc da TagResource confirma que usuários
    # aceitam tag).
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
        # Formas de criar instância/cluster sem passar por Create* (item 11)
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
    # Item 10: recursos persistentes que geram cobrança por hora. Training,
    # processing e transform jobs são efêmeros (terminam antes do debounce)
    # -> recomendação de tag na origem (SDK/pipeline).
    "AmazonSageMaker": (
        CreationEventRule("sagemaker.amazonaws.com", "CreateEndpoint"),
        CreationEventRule("sagemaker.amazonaws.com", "CreateNotebookInstance"),
        CreationEventRule("sagemaker.amazonaws.com", "CreateDomain"),
        CreationEventRule("sagemaker.amazonaws.com", "CreateApp"),
    ),
    "AWSSecretsManager": (CreationEventRule("secretsmanager.amazonaws.com", "CreateSecret"),),
    # Item 10: o único recurso é o hub da conta/região (cobrança por conta e
    # região). EnableSecurityHub não devolve nada; ARN hub/default é o
    # formato fixo do serviço.
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
    # CORRIGIDO: fonte real é "timestream" (confirmado via signingName do
    # botocore), não "timestream-write.amazonaws.com" como uma versão
    # anterior deste módulo tinha.
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
    """Devolve os `product_service_code` do CSV oficial que não têm NENHUMA
    entrada nesta tabela (nem mapeada, nem `None` explícito). Lista vazia é
    o estado esperado — usado por `test_event_mapping.py` para travar
    qualquer divergência silenciosa entre o CSV e esta tabela."""
    services = services if services is not None else load_services()
    csv_codes = {s.product_service_code for s in services}
    return sorted(csv_codes - set(_EVENT_MAPPING.keys()))


def build_event_pattern() -> dict:
    """Monta um ÚNICO event pattern (todos os serviços mapeados, agrupados
    por `event_source` via `$or` — cada alternativa exige `source` +
    `eventName` correlacionados, evitando o falso-positivo teórico de um
    pattern "achatado" com dois arrays soltos).

    **Só para referência/debug** — o pattern único já ultrapassa a quota
    padrão de 2.048 caracteres com o conjunto atual de serviços mapeados.
    Para gerar o(s) pattern(s) de verdade usado(s) pela(s) regra(s) do
    EventBridge, use `build_event_patterns()` (plural)."""
    return {"detail-type": [_DETAIL_TYPE], "$or": _alternativas_por_fonte()}


_DETAIL_TYPE = "AWS API Call via CloudTrail"

# Limite PADRÃO de tamanho de event pattern do EventBridge é 2.048
# caracteres (ajustável via Service Quotas — ver
# https://docs.aws.amazon.com/general/latest/gr/ev.html, "Event pattern
# size"). Usamos uma margem abaixo disso (não o limite exato) de propósito:
# preferimos várias regras dentro da quota padrão a depender de um aumento
# de quota que precisaria ser solicitado e replicado em cada uma das
# ~80-90 contas de cliente via StackSet — excelência operacional/de custo,
# nada para configurar manualmente por cliente.
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
                # Uma chamada de API que FALHOU (ex.: CreateBucket negado por
                # AccessDenied, ou nome já em uso) ainda grava um evento no
                # CloudTrail com o mesmo eventName — o recurso nunca chegou a
                # existir. `errorCode` só aparece no evento quando a chamada
                # falhou; exigir sua ausência filtra esses casos antes de
                # gastar uma invocação Lambda tentando ler/taguear um ARN que
                # não existe.
                "errorCode": [{"exists": False}],
            },
        }
        for event_source, event_names in sorted(por_fonte.items())
    ]


def build_event_patterns(max_chars: int = _MAX_EVENT_PATTERN_CHARS) -> list[dict]:
    """Divide as alternativas em N patterns, cada um dentro de `max_chars`
    (medido no JSON compacto, sem espaços — a mesma forma que conta para a
    quota do EventBridge) — bin-packing guloso, simples de propósito: a
    ordem das alternativas (`sorted` por `event_source`) é estável, então a
    divisão em grupos não muda de forma alguma que dependa da ordem de
    iteração de um dict.

    Cada pattern desta lista alimenta UMA regra de EventBridge (ver
    `infra/template.yaml`) — o número de regras necessárias cresce (raramente
    encolhe) conforme `_EVENT_MAPPING` ganha serviços novos. Ver
    `infra/README.md` para o processo de regenerar `infra/event_pattern.*.generated.json`
    e ajustar o número de regras no template quando isso acontecer."""
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


# Padrões de eventos NATIVOS de serviço (não "AWS API Call via CloudTrail") —
# não exigem trail e não entram no bin-packing acima (cada um é uma regra
# própria em infra/template.yaml, ver `Etapa3NativeEc2InstanceRunningRule`).
NATIVE_EVENT_PATTERNS: dict[str, dict] = {
    "ec2_instance_running": {
        "source": ["aws.ec2"],
        "detail-type": ["EC2 Instance State-change Notification"],
        "detail": {"state": ["running"]},
    },
    # Item 14: no Fargate o custo fica na TASK — tag no service não chega à
    # task sem `propagateTags`. Só Fargate: no launch type EC2 o custo está
    # na instância (já coberta pela regra acima). Sem debounce na state
    # machine (a task pode durar menos de 30 min; com propagateTags as tags
    # já vêm no launch, então não há corrida com IaC a evitar).
    "ecs_task_running": {
        "source": ["aws.ecs"],
        "detail-type": ["ECS Task State Change"],
        "detail": {"lastStatus": ["RUNNING"], "desiredStatus": ["RUNNING"], "launchType": ["FARGATE"]},
    },
}


# Etapa 4 — remoção/substituição de tags, de QUALQUER serviço (sem filtro
# de source: os nomes variam muito entre serviços e o handler descarta o que
# está fora do CSV). `prefix` cobre os nomes versionados do CloudTrail (ex.:
# Lambda "UntagResource20170331"). Ver handler_drift.py.
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
    """Regrava `infra/event_pattern.<N>.generated.json` a partir do estado
    atual de `_EVENT_MAPPING` — rodar sempre que a tabela mudar (novo
    serviço mapeado, evento adicionado/removido). Ver infra/README.md."""
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
    for template_name in ("template.yaml", "template-regional.yaml"):
        template_path = infra_dir / template_name
        if template_path.exists():
            n = sync_template_event_patterns(template_path, patterns)
            print(f"{template_path.name}: {n} bloco(s) EventPattern sincronizado(s)")
    print(
        "IMPORTANTE: se o número de patterns mudou, ainda é preciso adicionar/remover "
        "manualmente o recurso AWS::Events::Rule correspondente (ver infra/README.md) — "
        "os testes test_infra_event_patterns.py apontam exatamente quando."
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
