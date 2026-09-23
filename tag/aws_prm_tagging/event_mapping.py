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
novo sem que a tabela tenha sido atualizada ainda: ainda não implementado —
registrado como melhoria futura em
[docs/melhorias-futuras.md](docs/melhorias-futuras.md) (ideia: alerta via
SNS/e-mail apontando o(s) `product_service_code` sem mapeamento). Por ora, o
comportamento é silencioso: um serviço sem mapeamento simplesmente não gera
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
**Nenhum extractor foi validado contra um evento CloudTrail real capturado
em sandbox** — ver [docs/melhorias-futuras.md](docs/melhorias-futuras.md).

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
    "AmazonAppStream": None,  # 17 operações Create* (verificado via botocore), sem "o" recurso óbvio
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
    "AmazonBedrockAgentCore": None,  # 29 operações Create* (verificado via botocore), múltiplos sub-recursos novos
    "AWSCertificateManager": (
        CreationEventRule("acm.amazonaws.com", "RequestCertificate"),
        CreationEventRule("acm-pca.amazonaws.com", "CreateCertificateAuthority"),
    ),
    "AmazonCloudDirectory": (CreationEventRule("clouddirectory.amazonaws.com", "CreateDirectory"),),
    "AWSCloudWAN": (
        CreationEventRule("networkmanager.amazonaws.com", "CreateCoreNetwork"),
        CreationEventRule("networkmanager.amazonaws.com", "CreateGlobalNetwork"),
    ),
    "AmazonCloudFront": (CreationEventRule("cloudfront.amazonaws.com", "CreateDistribution"),),
    # CORRIGIDO: fonte real é "cloudhsm" (confirmado via signingName do
    # botocore) — uma versão anterior deste módulo usava
    # "cloudhsmv2.amazonaws.com", errado (esse é só o nome do pacote SDK).
    "CloudHSM": (CreationEventRule("cloudhsm.amazonaws.com", "CreateCluster"),),
    "AmazonCloudWatch": (CreationEventRule("logs.amazonaws.com", "CreateLogGroup"),),  # CSV: "Logs only"
    "CodeBuild": (CreationEventRule("codebuild.amazonaws.com", "CreateProject"),),
    "AWSCodePipeline": (CreationEventRule("codepipeline.amazonaws.com", "CreatePipeline"),),
    "AWSCodeStar": None,  # serviço descontinuado pela AWS (sem novos clientes) e sem definição no botocore instalado
    "AmazonCognito": (
        CreationEventRule("cognito-idp.amazonaws.com", "CreateUserPool"),
        CreationEventRule("cognito-identity.amazonaws.com", "CreateIdentityPool"),
    ),
    "comprehend": None,  # 5 operações Create* (verificado via botocore), mas jobs/endpoints, sem recurso persistente óbvio
    "AmazonConnect": (CreationEventRule("connect.amazonaws.com", "CreateInstance"),),
    "datapipeline": (CreationEventRule("datapipeline.amazonaws.com", "CreatePipeline"),),
    "AWSDatabaseMigrationSvc": (
        CreationEventRule("dms.amazonaws.com", "CreateReplicationInstance"),
        CreationEventRule("dms.amazonaws.com", "CreateReplicationTask"),
    ),
    "AWSDataSync": None,  # 13 operações Create* (verificado via botocore), quase todas "Location*", sem recurso único óbvio
    "AWSDeadlineCloud": None,  # 13 operações Create* (verificado via botocore), múltiplos sub-recursos
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
    "AmazonDocDB": None,
    "AmazonDynamoDB": (CreationEventRule("dynamodb.amazonaws.com", "CreateTable"),),
    "AmazonEC2": (
        CreationEventRule("ec2.amazonaws.com", "RunInstances"),
        CreationEventRule("ec2.amazonaws.com", "CreateVolume"),
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
    "AWSElasticDisasterRecovery": None,  # 6 operações Create* (verificado via botocore), nenhuma é "criar recurso" no sentido usual — é replicação contínua
    "AmazonEFS": (CreationEventRule("elasticfilesystem.amazonaws.com", "CreateFileSystem"),),
    "AmazonElastiCache": (CreationEventRule("elasticache.amazonaws.com", "CreateCacheCluster"),),
    "AWSElementalMediaConvert": (CreationEventRule("mediaconvert.amazonaws.com", "CreateQueue"),),
    "AWSElementalMediaLive": (CreationEventRule("medialive.amazonaws.com", "CreateChannel"),),
    "AWSElementalMediaPackage": (CreationEventRule("mediapackage.amazonaws.com", "CreateChannel"),),
    "ElasticMapReduce": (CreationEventRule("elasticmapreduce.amazonaws.com", "RunJobFlow"),),
    "AmazonFinSpace": (CreationEventRule("finspace.amazonaws.com", "CreateEnvironment"),),
    "AmazonFSx": (CreationEventRule("fsx.amazonaws.com", "CreateFileSystem"),),
    "AmazonGameLift": (CreationEventRule("gamelift.amazonaws.com", "CreateFleet"),),
    "AWSGlue": None,  # 31 operações Create* (verificado via botocore), sem "o" recurso óbvio para PRM
    "AmazonMedicalImaging": (CreationEventRule("medical-imaging.amazonaws.com", "CreateDatastore"),),
    "AmazonHealthLake": (CreationEventRule("healthlake.amazonaws.com", "CreateFHIRDatastore"),),
    "AWSIoT": None,  # 32 operações Create* (verificado via botocore), sem prioridade clara para o lote inicial
    "AWSIoTSiteWise": None,  # AssetModel/Asset/Gateway, sem prioridade clara ainda
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
    "AWSLambda": (CreationEventRule("lambda.amazonaws.com", "CreateFunction"),),
    "AWSELB": (CreationEventRule("elasticloadbalancing.amazonaws.com", "CreateLoadBalancer"),),
    "AWSM2": (
        CreationEventRule("m2.amazonaws.com", "CreateEnvironment"),
        CreationEventRule("m2.amazonaws.com", "CreateApplication"),
    ),
    "AmazonMemoryDB": (CreationEventRule("memorydb.amazonaws.com", "CreateCluster"),),
    "AmazonMQ": (CreationEventRule("mq.amazonaws.com", "CreateBroker"),),
    "AmazonMSK": (CreationEventRule("kafka.amazonaws.com", "CreateClusterV2"),),
    "AmazonNeptune": None,  # mesma ambiguidade CONFIRMADA de "AmazonDocDB" acima (endpointPrefix/signingName = "rds")
    "AWSNetworkFirewall": (CreationEventRule("network-firewall.amazonaws.com", "CreateFirewall"),),
    "AmazonOmics": None,  # 12 operações Create* (verificado via botocore), múltiplos tipos de recurso
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
    "AmazonQuickSight": None,  # 34 operações Create* (verificado via botocore) — de longe a mais ambígua depois do SageMaker
    "AmazonRedshift": (
        CreationEventRule("redshift.amazonaws.com", "CreateCluster"),
        CreationEventRule("redshift-serverless.amazonaws.com", "CreateWorkgroup"),
    ),
    "AmazonRDS": (
        CreationEventRule("rds.amazonaws.com", "CreateDBInstance"),
        CreationEventRule("rds.amazonaws.com", "CreateDBCluster"),
    ),
    "AWSResilienceHub": (CreationEventRule("resiliencehub.amazonaws.com", "CreateApp"),),
    "AmazonRoute53": (CreationEventRule("route53.amazonaws.com", "CreateHostedZone"),),
    "AmazonS3": (CreationEventRule("s3.amazonaws.com", "CreateBucket"),),
    "AmazonGlacier": (CreationEventRule("glacier.amazonaws.com", "CreateVault"),),
    "AmazonSageMaker": None,  # 72(!) operações Create* (verificado via botocore) — a mais ambígua de todo o CSV
    "AWSSecretsManager": (CreationEventRule("secretsmanager.amazonaws.com", "CreateSecret"),),
    "AWSSecurityHub": None,  # 11 operações Create* (verificado via botocore), nível de conta, não "criação de recurso" no sentido usual
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
        {"source": [f"aws.{event_source.split('.')[0]}"], "detail": {"eventName": sorted(event_names)}}
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
    print(
        "IMPORTANTE: se este número mudou desde a última geração, ajuste manualmente "
        "o número de recursos AWS::Events::Rule em infra/template.yaml (ver infra/README.md)."
    )


if __name__ == "__main__":
    _regenerate_infra_files()
