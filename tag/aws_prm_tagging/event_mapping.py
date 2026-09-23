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
EventBridge, construído por `build_event_pattern()`, só cobre os serviços
mapeados) — a Etapa 4 (varredura periódica, fora do escopo deste módulo)
continua sendo o backstop para qualquer lacuna de cobertura aqui.

## Confiabilidade dos mapeamentos abaixo

**Importante, para quem for revisar/estender esta tabela**: os `eventName`/
`eventSource` abaixo vêm do conhecimento de documentação pública das APIs da
AWS, não de um evento CloudTrail real capturado em sandbox. A maioria tem
alta confiança (operações de criação bem estabelecidas e estáveis: EC2, S3,
Lambda, DynamoDB, RDS, EKS, Bedrock, SNS, SQS, etc.), mas **nenhuma foi
validada contra uma conta AWS real ainda** — mesma ressalva que já existe em
outros pontos do projeto (ver, por exemplo, "Códigos de erro 'não
encontrado' do ELBv2 sem confirmação em sandbox" em
[docs/melhorias-futuras.md](docs/melhorias-futuras.md)). Recomendação:
validar cada entrada contra um evento real antes de habilitar aquele
serviço em produção.

Serviços deliberadamente deixados como `None` (não é preguiça — é porque
mapear errado aqui tem custo real, ver `services.py`/`melhorias-futuras.md`
sobre o mesmo princípio para `_NAMESPACE_TO_CODE`): serviços muito novos
(pouca confiança na API pública), serviços com múltiplos tipos de recurso
sem um "recurso principal" óbvio para fins de tagueamento PRM, serviços
descontinuados/legados, e serviços cujo modelo de API não é o REST/CloudTrail
management-event padrão (ex.: Keyspaces/Cassandra, que usa API CQL).
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
    "AmazonApiGateway": None,  # REST API (v1) e HTTP/WebSocket API (v2) são recursos distintos — mapear os dois com cuidado depois
    "AmazonAppStream": None,  # múltiplos tipos de recurso (Stack/Fleet/ImageBuilder), sem "o" recurso óbvio
    "AWSAppSync": (CreationEventRule("appsync.amazonaws.com", "CreateGraphqlApi"),),
    "AmazonAthena": (
        CreationEventRule("athena.amazonaws.com", "CreateWorkGroup"),
        CreationEventRule("athena.amazonaws.com", "CreateDataCatalog"),
    ),
    "AuroraDSQL": None,  # serviço novo (2025); API ainda não bem estabelecida na minha base de conhecimento
    "AWSBackup": (
        CreationEventRule("backup.amazonaws.com", "CreateBackupVault"),
        CreationEventRule("backup.amazonaws.com", "CreateBackupPlan"),
    ),
    "AmazonBedrock": (CreationEventRule("bedrock.amazonaws.com", "CreateInferenceProfile"),),
    "AmazonBedrockAgentCore": None,  # múltiplos sub-recursos novos (Runtime/Gateway/Memory/...), baixa confiança
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
    "CloudHSM": (CreationEventRule("cloudhsmv2.amazonaws.com", "CreateCluster"),),
    "AmazonCloudWatch": (CreationEventRule("logs.amazonaws.com", "CreateLogGroup"),),  # CSV: "Logs only"
    "CodeBuild": (CreationEventRule("codebuild.amazonaws.com", "CreateProject"),),
    "AWSCodePipeline": (CreationEventRule("codepipeline.amazonaws.com", "CreatePipeline"),),
    "AWSCodeStar": None,  # serviço descontinuado pela AWS (sem novos clientes) — baixa prioridade
    "AmazonCognito": (
        CreationEventRule("cognito-idp.amazonaws.com", "CreateUserPool"),
        CreationEventRule("cognito-identity.amazonaws.com", "CreateIdentityPool"),
    ),
    "comprehend": None,  # recursos são majoritariamente jobs/endpoints, sem "o" recurso persistente óbvio
    "AmazonConnect": (CreationEventRule("connect.amazonaws.com", "CreateInstance"),),
    "datapipeline": None,  # serviço legado/baixo uso, sem prioridade de mapear agora
    "AWSDatabaseMigrationSvc": (
        CreationEventRule("dms.amazonaws.com", "CreateReplicationInstance"),
        CreationEventRule("dms.amazonaws.com", "CreateReplicationTask"),
    ),
    "AWSDataSync": None,  # múltiplos tipos de "Location" (S3/NFS/SMB/...), sem recurso único óbvio
    "AWSDeadlineCloud": None,  # serviço novo, baixa confiança na API
    "AWSDirectConnect": (
        CreationEventRule("directconnect.amazonaws.com", "CreateConnection"),
        CreationEventRule("directconnect.amazonaws.com", "CreateDirectConnectGateway"),
    ),
    "AWSDirectoryService": (
        CreationEventRule("ds.amazonaws.com", "CreateDirectory"),
        CreationEventRule("ds.amazonaws.com", "CreateMicrosoftAD"),
    ),
    # DocumentDB e Neptune usam rds:CreateDBCluster (mesma ambiguidade de
    # namespace "rds" já documentada em services.py) — deixados `None` aqui
    # de propósito: mapear via este evento também dispararia para
    # AmazonRDS/AmazonNeptune, sem forma de diferenciar sem uma chamada de
    # API extra (mesmo problema, mesma decisão de não resolver agora).
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
    "AWSElasticDisasterRecovery": None,  # fluxo de "recurso criado" não mapeia bem ao modelo de replicação contínua do DRS
    "AmazonEFS": (CreationEventRule("elasticfilesystem.amazonaws.com", "CreateFileSystem"),),
    "AmazonElastiCache": (CreationEventRule("elasticache.amazonaws.com", "CreateCacheCluster"),),
    "AWSElementalMediaConvert": None,  # recurso taggável mais relevante (Queue/JobTemplate) ambíguo, baixa prioridade
    "AWSElementalMediaLive": (CreationEventRule("medialive.amazonaws.com", "CreateChannel"),),
    "AWSElementalMediaPackage": (CreationEventRule("mediapackage.amazonaws.com", "CreateChannel"),),
    "ElasticMapReduce": (CreationEventRule("elasticmapreduce.amazonaws.com", "RunJobFlow"),),
    "AmazonFinSpace": (CreationEventRule("finspace.amazonaws.com", "CreateEnvironment"),),
    "AmazonFSx": (CreationEventRule("fsx.amazonaws.com", "CreateFileSystem"),),
    "AmazonGameLift": (CreationEventRule("gamelift.amazonaws.com", "CreateFleet"),),
    "AWSGlue": None,  # muitos tipos de recurso (Job/Crawler/Database/...), sem "o" recurso óbvio para PRM
    "AmazonMedicalImaging": (CreationEventRule("medical-imaging.amazonaws.com", "CreateDatastore"),),
    "AmazonHealthLake": (CreationEventRule("healthlake.amazonaws.com", "CreateFHIRDatastore"),),
    "AWSIoT": None,  # IoT Core tem muitos tipos de recurso, sem prioridade clara para o lote inicial
    "AWSIoTSiteWise": None,  # idem — AssetModel/Asset/Gateway, sem prioridade clara ainda
    "AmazonKendra": (CreationEventRule("kendra.amazonaws.com", "CreateIndex"),),
    "awskms": (CreationEventRule("kms.amazonaws.com", "CreateKey"),),
    "AmazonMCS": None,  # Keyspaces usa API CQL (Cassandra), não segue o modelo de CloudTrail management event padrão
    "AmazonKinesisAnalytics": (CreationEventRule("kinesisanalyticsv2.amazonaws.com", "CreateApplication"),),
    "AmazonKinesisFirehose": (CreationEventRule("firehose.amazonaws.com", "CreateDeliveryStream"),),
    "AmazonKinesis": (CreationEventRule("kinesis.amazonaws.com", "CreateStream"),),
    "AmazonKinesisVideo": (CreationEventRule("kinesisvideo.amazonaws.com", "CreateStream"),),
    "AWSLambda": (CreationEventRule("lambda.amazonaws.com", "CreateFunction20150331v2"),),
    "AWSELB": (CreationEventRule("elasticloadbalancing.amazonaws.com", "CreateLoadBalancer"),),
    "AWSM2": None,  # serviço de nicho (mainframe modernization), baixa prioridade para o lote inicial
    "AmazonMemoryDB": (CreationEventRule("memorydb.amazonaws.com", "CreateCluster"),),
    "AmazonMQ": (CreationEventRule("mq.amazonaws.com", "CreateBroker"),),
    "AmazonMSK": (CreationEventRule("kafka.amazonaws.com", "CreateClusterV2"),),
    "AmazonNeptune": None,  # mesma ambiguidade de namespace "rds" que AmazonDocDB, ver acima
    "AWSNetworkFirewall": (CreationEventRule("network-firewall.amazonaws.com", "CreateFirewall"),),
    "AmazonOmics": None,  # serviço de nicho, múltiplos tipos de recurso, baixa prioridade
    "AmazonES": (CreationEventRule("es.amazonaws.com", "CreateDomain"),),  # inclui OpenSearch Service
    "PaymentCryptography": (CreationEventRule("payment-cryptography.amazonaws.com", "CreateKey"),),
    "AmazonQuickSight": None,  # muitos tipos de recurso (dashboard/dataset/analysis/...), ambíguo para PRM
    "AmazonRedshift": (
        CreationEventRule("redshift.amazonaws.com", "CreateCluster"),
        CreationEventRule("redshift-serverless.amazonaws.com", "CreateWorkgroup"),
    ),
    # CreateDBCluster também é usado por DocumentDB/Neptune (ver acima) —
    # aqui o evento SEMPRE é rotulado como "Amazon RDS" na extração, mesma
    # limitação de rótulo já documentada em services.py/melhorias-futuras.md
    # (não afeta a tag aplicada, só o campo "servico" do relatório).
    "AmazonRDS": (
        CreationEventRule("rds.amazonaws.com", "CreateDBInstance"),
        CreationEventRule("rds.amazonaws.com", "CreateDBCluster"),
    ),
    "AWSResilienceHub": (CreationEventRule("resiliencehub.amazonaws.com", "CreateApp"),),
    "AmazonRoute53": (CreationEventRule("route53.amazonaws.com", "CreateHostedZone"),),
    "AmazonS3": (CreationEventRule("s3.amazonaws.com", "CreateBucket"),),
    "AmazonGlacier": (CreationEventRule("glacier.amazonaws.com", "CreateVault"),),
    "AmazonSageMaker": None,  # superfície de recurso enorme (notebook/endpoint/model/training job/...), sem prioridade clara ainda
    "AWSSecretsManager": (CreationEventRule("secretsmanager.amazonaws.com", "CreateSecret"),),
    "AWSSecurityHub": None,  # habilitação em nível de conta, não um fluxo de "recurso criado" no sentido usual
    "AmazonSNS": (CreationEventRule("sns.amazonaws.com", "CreateTopic"),),
    "AWSQueueService": (CreationEventRule("sqs.amazonaws.com", "CreateQueue"),),
    "AmazonStates": (CreationEventRule("states.amazonaws.com", "CreateStateMachine"),),
    "AWSStorageGateway": None,  # múltiplos tipos de recurso (gateway/volume/share), ambíguo, baixa prioridade
    "AWSSystemsManager": (CreationEventRule("ssm.amazonaws.com", "CreateOpsItem"),),  # CSV: "OpsCenter only"
    "AmazonTimestream": (CreationEventRule("timestream-write.amazonaws.com", "CreateDatabase"),),
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
