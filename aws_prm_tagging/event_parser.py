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

import re
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
    # MVP2: recurso derivado (ex.: volume EBS de uma instância) aponta para
    # o recurso de origem — herda dele a decisão de IaC na Etapa 3.
    origem_arn: str | None = None


ArnBuilder = Callable[[dict, str, str], list[str]]


@dataclass(frozen=True)
class _SpecificExtractor:
    product_service_code: str
    arn_builder: ArnBuilder
    tipo_recurso: str | None = None
    # Opcional: decide o product_service_code a partir do próprio evento,
    # quando o eventSource é compartilhado por mais de uma linha do CSV
    # (hoje: rds.amazonaws.com para RDS/DocumentDB/Neptune — o campo
    # `requestParameters.engine` desambigua sem nenhuma chamada de API).
    # Devolve None para cair no `product_service_code` fixo acima.
    product_code_resolver: Callable[[dict], str | None] | None = None


# Ambiguidade conhecida, mesma de `services.classify_arn`/`_service_by_name`:
# o código "AmazonVPC" é compartilhado por 2 linhas do CSV ("AWS Transit
# Gateway" e "Amazon VPC Lattice"). Resolver por código pega sempre a
# PRIMEIRA linha ("AWS Transit Gateway") — correto para os eventos de EC2
# (`CreateVpc`/`CreateTransitGateway`, que são mesmo Transit Gateway/rede),
# mas errado para `vpc-lattice.amazonaws.com`, cujo evento já identifica o
# serviço sem ambiguidade nenhuma. Bug real encontrado em code review: essa
# correção existia em `services.py` mas nunca tinha chegado aqui — todo
# evento de VPC Lattice saía rotulado "AWS Transit Gateway" no relatório
# (a tag aplicada nunca estava errada, só o rótulo `servico`).
_SERVICE_NAME_OVERRIDE_POR_EVENT_SOURCE = {
    "vpc-lattice.amazonaws.com": "Amazon VPC Lattice",
}


def _svc_name(services: list[Service], product_service_code: str, event_source: str | None = None) -> str | None:
    override_name = _SERVICE_NAME_OVERRIDE_POR_EVENT_SOURCE.get(event_source or "")
    if override_name is not None:
        for svc in services:
            if svc.name == override_name:
                return svc.name
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


def _direct(*path: str, tolerant_casing: bool = True) -> ArnBuilder:
    # Default mudou para True (2026-09-30): o CloudTrail grava vários
    # serviços de protocolo json/rest-json em lowerCamelCase no
    # `responseElements` (ex.: Lambda `functionArn`, DynamoDB
    # `tableDescription.tableArn`, KMS `keyMetadata.arn`), diferente do
    # shape PascalCase do botocore. `_read_path` tenta a capitalização
    # exata PRIMEIRO e só depois a variante — nunca piora um caso que já
    # funcionava. Ver docs/plano-finalizacao.md, achado A2.
    return _DirectPath(path, tolerant_casing)


def _direct_ci(*path: str) -> ArnBuilder:
    """Como `_direct`, mas tolerante à capitalização (ver docstring do
    módulo — serviços de protocolo query/ec2/rest-xml)."""
    return _direct(*path, tolerant_casing=True)


def _constructed(template: str, source: str, *path: str, tolerant_casing: bool = True) -> ArnBuilder:
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
    #
    # `isinstance` defensivo (bug real encontrado em code review): `.get(
    # "instancesSet", {})` só usa o default `{}` quando a CHAVE não existe —
    # se vier presente com valor `None` (`{"instancesSet": null}`), o
    # default nunca entra em jogo e o `.get("items")` seguinte quebra com
    # `AttributeError`, derrubando a invocação Lambda inteira em vez de
    # degradar para lista vazia (o princípio que a docstring do módulo
    # declara). Mesmo raciocínio para `item` não ser um dict.
    instances_set = (detail.get("responseElements") or {}).get("instancesSet")
    items = instances_set.get("items") if isinstance(instances_set, dict) else None
    items = items if isinstance(items, list) else []
    return [
        f"arn:aws:ec2:{region}:{account_id}:instance/{item['instanceId']}"
        for item in items
        if isinstance(item, dict) and item.get("instanceId")
    ]


def _ext_ec2_create_volume(detail: dict, account_id: str, region: str) -> list[str]:
    volume_id = (detail.get("responseElements") or {}).get("volumeId")
    return [f"arn:aws:ec2:{region}:{account_id}:volume/{volume_id}"] if volume_id else []


def _ext_ec2_create_vpc(detail: dict, account_id: str, region: str) -> list[str]:
    # `isinstance` defensivo — mesmo motivo de `_ext_ec2_run_instances`
    # acima: `responseElements` = `{"vpc": null}` quebraria com
    # `AttributeError` sem essa checagem.
    vpc = (detail.get("responseElements") or {}).get("vpc")
    vpc_id = vpc.get("vpcId") if isinstance(vpc, dict) else None
    return [f"arn:aws:ec2:{region}:{account_id}:vpc/{vpc_id}"] if vpc_id else []


def _ext_ec2_create_transit_gateway(detail: dict, account_id: str, region: str) -> list[str]:
    # `isinstance` defensivo — mesmo motivo de `_ext_ec2_create_vpc` acima.
    tgw = (detail.get("responseElements") or {}).get("transitGateway")
    tgw_id = tgw.get("transitGatewayId") if isinstance(tgw, dict) else None
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
    # elasticloadbalancing.amazonaws.com/CreateLoadBalancer é compartilhado
    # por DOIS serviços — elbv2 (ALB/NLB) e o Classic ELB (pacote `elb`) —
    # com o MESMO eventSource e eventName (confirmado via botocore:
    # signingName/endpointPrefix idênticos), mas shapes de resposta
    # completamente diferentes. Bug real encontrado em code review: o
    # extractor só sabia ler o shape do elbv2
    # (`responseElements.LoadBalancers[...]`); Classic ELB devolve só
    # `{"DNSName": ...}`, sem ARN nenhum na resposta — todo Classic ELB
    # (ainda comum via Terraform `aws_elb`) nunca era tagueado,
    # silenciosamente (lista vazia, sem erro). Os dois protocolos usam
    # nomes de campo DIFERENTES no request (`Name` no elbv2, confirmado
    # em evento real capturado em sandbox como `"name"`; `LoadBalancerName`
    # no Classic ELB, verificado contra o shape do botocore) — isso serve
    # de sinal confiável de qual dos dois é, sem ambiguidade.
    lbs = _read_path(detail.get("responseElements") or {}, ("LoadBalancers",), tolerant_casing=True) or []
    if isinstance(lbs, list) and lbs:
        arn = lbs[0].get("LoadBalancerArn", lbs[0].get("loadBalancerArn")) if isinstance(lbs[0], dict) else None
        if isinstance(arn, str) and arn.startswith("arn:"):
            return [arn]

    # Não veio no shape do elbv2 — tenta Classic ELB, construindo o ARN a
    # partir do nome pedido (formato estável documentado pela AWS:
    # arn:...:loadbalancer/<nome>, sem o segmento "app/"/"net/" que
    # ALB/NLB usam).
    nome_classic = _read_path(detail.get("requestParameters") or {}, ("LoadBalancerName",), tolerant_casing=True)
    if isinstance(nome_classic, str) and nome_classic:
        return [f"arn:aws:elasticloadbalancing:{region}:{account_id}:loadbalancer/{nome_classic}"]
    return []


def _ext_workspaces_create_workspaces(detail: dict, account_id: str, region: str) -> list[str]:
    # CreateWorkspaces é uma API de LOTE (cria N workspaces numa chamada) —
    # cada item pendente vira um recurso próprio.
    pending = (detail.get("responseElements") or {}).get("PendingRequests") or []
    return [
        f"arn:aws:workspaces:{region}:{account_id}:workspace/{item['WorkspaceId']}"
        for item in pending
        if isinstance(item, dict) and item.get("WorkspaceId")
    ]


# ---------------------------------------------------------------------------
# Extractors adicionados em 2026-09-30 (docs/plano-finalizacao.md, Lote 1).
# Princípio comum: tentar primeiro o ARN pronto na resposta (tolerante a
# capitalização) e, se não vier, CONSTRUIR a partir de um identificador
# pedido na requisição — a mesma estratégia já validada em sandbox para
# S3/Elastic Beanstalk/Redshift. Nunca inventa um valor: sem identificador,
# devolve lista vazia.
# ---------------------------------------------------------------------------

_INSTANCE_ID_RE = re.compile(r"^i-[0-9a-f]{8,17}$")


def _first_of(*builders: ArnBuilder) -> ArnBuilder:
    """Tenta cada builder em ordem e devolve o primeiro resultado não vazio."""

    def _builder(detail: dict, account_id: str, region: str) -> list[str]:
        for b in builders:
            arns = b(detail, account_id, region)
            if arns:
                return arns
        return []

    return _builder


def _find_strings(obj, predicate, depth: int = 0) -> list[str]:
    """Varre recursivamente dicts/listas (até profundidade 8) coletando
    strings que satisfazem `predicate`, sem repetir, na ordem encontrada.
    Usado só onde o VALOR tem um formato inconfundível (ex.: ID de
    instância `i-...`), nunca para adivinhar um ARN por nome de chave."""
    found: list[str] = []
    if depth > 8 or obj is None:
        return found
    if isinstance(obj, str):
        return [obj] if predicate(obj) else []
    if isinstance(obj, dict):
        values = obj.values()
    elif isinstance(obj, list):
        values = obj
    else:
        return found
    for v in values:
        for item in _find_strings(v, predicate, depth + 1):
            if item not in found:
                found.append(item)
    return found


def _find_key_values(obj, key_lower: str, depth: int = 0) -> list[str]:
    """Coleta valores string de qualquer chave cujo nome, ignorando
    capitalização, seja exatamente `key_lower` — para os eventos EC2 cujo
    empacotamento XML→JSON varia (`xSet.items`, `item` único vs. lista,
    wrapper `...Response`)."""
    found: list[str] = []
    if depth > 8 or obj is None:
        return found
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() == key_lower and isinstance(v, str) and v:
                if v not in found:
                    found.append(v)
            else:
                for item in _find_key_values(v, key_lower, depth + 1):
                    if item not in found:
                        found.append(item)
    elif isinstance(obj, list):
        for v in obj:
            for item in _find_key_values(v, key_lower, depth + 1):
                if item not in found:
                    found.append(item)
    return found


def _ext_ec2_by_id_key(key_lower: str, arn_template: str, id_prefix: str) -> ArnBuilder:
    def _builder(detail: dict, account_id: str, region: str) -> list[str]:
        ids = [
            v for v in _find_key_values(detail.get("responseElements") or {}, key_lower) if v.startswith(id_prefix)
        ]
        return [arn_template.format(region=region, account_id=account_id, value=v) for v in ids]

    return _builder


def _ext_ec2_create_fleet(detail: dict, account_id: str, region: str) -> list[str]:
    # CreateFleet (Karpenter, ASG com MixedInstancesPolicy/Spot, EC2 Fleet
    # tipo "instant") — o shape do CloudTrail para esta operação tem
    # empacotamento XML→JSON próprio e não foi capturado em sandbox ainda.
    # Em vez de depender de um caminho exato, coleta qualquer string com o
    # formato inconfundível de ID de instância dentro de responseElements.
    # OBS: a regra nativa "EC2 Instance State-change Notification" (ver
    # event_mapping.NATIVE_EVENT_PATTERNS) já cobre instâncias de QUALQUER
    # origem — este extractor é defesa em profundidade, não o caminho
    # principal.
    ids = _find_strings(detail.get("responseElements") or {}, lambda v: bool(_INSTANCE_ID_RE.match(v)))
    return [f"arn:aws:ec2:{region}:{account_id}:instance/{i}" for i in ids]


def _ext_lambda_function(detail: dict, account_id: str, region: str) -> list[str]:
    arn = _read_path(detail.get("responseElements") or {}, ("FunctionArn",), tolerant_casing=True)
    if isinstance(arn, str) and arn.startswith("arn:"):
        # Remove qualificador de versão (":1", ":$LATEST") se vier — a tag
        # é aplicada na função, não numa versão publicada.
        partes = arn.split(":")
        return [":".join(partes[:7])]
    nome = _read_path(detail.get("requestParameters") or {}, ("FunctionName",), tolerant_casing=True)
    if isinstance(nome, str) and nome:
        if nome.startswith("arn:"):
            return [":".join(nome.split(":")[:7])]
        return [f"arn:aws:lambda:{region}:{account_id}:function:{nome}"]
    return []


def _ext_dynamodb_table(*request_keys: str) -> ArnBuilder:
    def _builder(detail: dict, account_id: str, region: str) -> list[str]:
        arn = _read_path(
            detail.get("responseElements") or {}, ("TableDescription", "TableArn"), tolerant_casing=True
        )
        if isinstance(arn, str) and arn.startswith("arn:"):
            return [arn]
        for key in request_keys:
            nome = _read_path(detail.get("requestParameters") or {}, (key,), tolerant_casing=True)
            if isinstance(nome, str) and nome:
                return [f"arn:aws:dynamodb:{region}:{account_id}:table/{nome}"]
        return []

    return _builder


def _ext_rds(arn_field: str, wrapper: str, id_keys: tuple[str, ...], arn_kind: str) -> ArnBuilder:
    """RDS/Aurora/DocumentDB/Neptune. Confirmado em sandbox (CreateDBInstance)
    que o CloudTrail ACHATA a resposta (`dBInstanceArn` na raiz); por
    segurança também tenta o shape com wrapper do botocore e, por último,
    constrói a partir do identificador pedido."""
    return _first_of(
        _direct(arn_field),
        _direct(wrapper, arn_field),
        *[
            _constructed(f"arn:aws:rds:{{region}}:{{account_id}}:{arn_kind}:{{value}}", "requestParameters", k)
            for k in id_keys
        ],
    )


def _rds_engine_resolver(detail: dict) -> str | None:
    engine = _read_path(detail.get("requestParameters") or {}, ("Engine",), tolerant_casing=True)
    if not isinstance(engine, str):
        return None
    engine = engine.lower()
    if engine == "docdb" or engine.startswith("docdb"):
        return "AmazonDocDB"
    if engine == "neptune" or engine.startswith("neptune"):
        return "AmazonNeptune"
    return None


def _ext_cloudfront_distribution(detail: dict, account_id: str, region: str) -> list[str]:
    resp = detail.get("responseElements") or {}
    arn = _read_path(resp, ("Distribution", "ARN"), tolerant_casing=True)
    if isinstance(arn, str) and arn.startswith("arn:"):
        return [arn]
    dist_id = _read_path(resp, ("Distribution", "Id"), tolerant_casing=True)
    if isinstance(dist_id, str) and dist_id:
        return [f"arn:aws:cloudfront::{account_id}:distribution/{dist_id}"]
    return []


def _ext_deadline_filho(tipo: str, campo_id: str) -> ArnBuilder:
    """Queue/fleet do Deadline: ARN = farm/<farmId do request>/<tipo>/<id da resposta>."""

    def _builder(detail: dict, account_id: str, region: str) -> list[str]:
        farm = _read_path(detail.get("requestParameters") or {}, ("farmId",), tolerant_casing=True)
        filho = _read_path(detail.get("responseElements") or {}, (campo_id,), tolerant_casing=True)
        if isinstance(farm, str) and farm and isinstance(filho, str) and filho:
            return [f"arn:aws:deadline:{region}:{account_id}:farm/{farm}/{tipo}/{filho}"]
        return []

    return _builder


_REGISTRY: dict[tuple[str, str], _SpecificExtractor] = {
    # --- EC2 (protocolo "ec2" — estrutura de CloudTrail conhecida à parte) ---
    ("ec2.amazonaws.com", "RunInstances"): _SpecificExtractor("AmazonEC2", _ext_ec2_run_instances),
    ("ec2.amazonaws.com", "CreateVolume"): _SpecificExtractor("AmazonEC2", _ext_ec2_create_volume),
    ("ec2.amazonaws.com", "CreateVpc"): _SpecificExtractor("AmazonVPC", _ext_ec2_create_vpc),
    ("ec2.amazonaws.com", "CreateTransitGateway"): _SpecificExtractor("AmazonVPC", _ext_ec2_create_transit_gateway),
    ("ec2.amazonaws.com", "CreateFleet"): _SpecificExtractor("AmazonEC2", _ext_ec2_create_fleet),
    ("ec2.amazonaws.com", "CreateSnapshot"): _SpecificExtractor(
        "AmazonEC2", _ext_ec2_by_id_key("snapshotid", "arn:aws:ec2:{region}::snapshot/{value}", "snap-")
    ),
    ("ec2.amazonaws.com", "CreateSnapshots"): _SpecificExtractor(
        "AmazonEC2", _ext_ec2_by_id_key("snapshotid", "arn:aws:ec2:{region}::snapshot/{value}", "snap-")
    ),
    ("ec2.amazonaws.com", "CopySnapshot"): _SpecificExtractor(
        "AmazonEC2", _ext_ec2_by_id_key("snapshotid", "arn:aws:ec2:{region}::snapshot/{value}", "snap-")
    ),
    ("ec2.amazonaws.com", "CreateNatGateway"): _SpecificExtractor(
        "AmazonEC2",
        _ext_ec2_by_id_key("natgatewayid", "arn:aws:ec2:{region}:{account_id}:natgateway/{value}", "nat-"),
    ),
    ("ec2.amazonaws.com", "AllocateAddress"): _SpecificExtractor(
        "AmazonEC2",
        _ext_ec2_by_id_key("allocationid", "arn:aws:ec2:{region}:{account_id}:elastic-ip/{value}", "eipalloc-"),
    ),
    # --- RDS: formas de criar instância/cluster/snapshot sem Create* ---
    ("rds.amazonaws.com", "RestoreDBInstanceFromDBSnapshot"): _SpecificExtractor(
        "AmazonRDS", _ext_rds("DBInstanceArn", "DBInstance", ("DBInstanceIdentifier",), "db"),
        product_code_resolver=_rds_engine_resolver,
    ),
    ("rds.amazonaws.com", "RestoreDBInstanceToPointInTime"): _SpecificExtractor(
        "AmazonRDS", _ext_rds("DBInstanceArn", "DBInstance", ("TargetDBInstanceIdentifier",), "db"),
        product_code_resolver=_rds_engine_resolver,
    ),
    ("rds.amazonaws.com", "RestoreDBInstanceFromS3"): _SpecificExtractor(
        "AmazonRDS", _ext_rds("DBInstanceArn", "DBInstance", ("DBInstanceIdentifier",), "db"),
    ),
    ("rds.amazonaws.com", "CreateDBInstanceReadReplica"): _SpecificExtractor(
        "AmazonRDS", _ext_rds("DBInstanceArn", "DBInstance", ("DBInstanceIdentifier",), "db"),
        product_code_resolver=_rds_engine_resolver,
    ),
    ("rds.amazonaws.com", "RestoreDBClusterFromSnapshot"): _SpecificExtractor(
        "AmazonRDS", _ext_rds("DBClusterArn", "DBCluster", ("DBClusterIdentifier",), "cluster"),
        product_code_resolver=_rds_engine_resolver,
    ),
    ("rds.amazonaws.com", "RestoreDBClusterToPointInTime"): _SpecificExtractor(
        "AmazonRDS", _ext_rds("DBClusterArn", "DBCluster", ("DBClusterIdentifier",), "cluster"),
        product_code_resolver=_rds_engine_resolver,
    ),
    ("rds.amazonaws.com", "RestoreDBClusterFromS3"): _SpecificExtractor(
        "AmazonRDS", _ext_rds("DBClusterArn", "DBCluster", ("DBClusterIdentifier",), "cluster"),
    ),
    ("rds.amazonaws.com", "CreateDBSnapshot"): _SpecificExtractor(
        "AmazonRDS", _ext_rds("DBSnapshotArn", "DBSnapshot", ("DBSnapshotIdentifier",), "snapshot"),
    ),
    ("rds.amazonaws.com", "CreateDBClusterSnapshot"): _SpecificExtractor(
        "AmazonRDS",
        _ext_rds("DBClusterSnapshotArn", "DBClusterSnapshot", ("DBClusterSnapshotIdentifier",), "cluster-snapshot"),
    ),
    # --- ElastiCache: Redis/Valkey em produção quase sempre é replication
    # group (ou serverless), não CreateCacheCluster ---
    ("elasticache.amazonaws.com", "CreateReplicationGroup"): _SpecificExtractor(
        "AmazonElastiCache",
        _first_of(
            _direct("ARN"),
            _direct("ReplicationGroup", "ARN"),
            _constructed(
                "arn:aws:elasticache:{region}:{account_id}:replicationgroup:{value}",
                "requestParameters",
                "ReplicationGroupId",
            ),
        ),
    ),
    ("elasticache.amazonaws.com", "CreateServerlessCache"): _SpecificExtractor(
        "AmazonElastiCache",
        _first_of(
            _direct("ServerlessCache", "ARN"),
            _direct("ARN"),
            _constructed(
                "arn:aws:elasticache:{region}:{account_id}:serverlesscache:{value}",
                "requestParameters",
                "ServerlessCacheName",
            ),
        ),
    ),
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
    ("rds.amazonaws.com", "CreateDBInstance"): _SpecificExtractor(
        "AmazonRDS", _direct_ci("DBInstanceArn"), product_code_resolver=_rds_engine_resolver
    ),
    ("rds.amazonaws.com", "CreateDBCluster"): _SpecificExtractor(
        "AmazonRDS", _direct_ci("DBClusterArn"), product_code_resolver=_rds_engine_resolver
    ),
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
    # Variante que já nasce com tags — mesmo recurso, outra operação (o
    # console usa esta). O eventName real no CloudTrail pode vir com sufixo
    # de versão de API (ex.: "...2020_05_31") — normalizado em
    # `normalize_event_name`.
    ("cloudfront.amazonaws.com", "CreateDistributionWithTags"): _SpecificExtractor(
        "AmazonCloudFront", _ext_cloudfront_distribution
    ),
    ("elasticloadbalancing.amazonaws.com", "CreateLoadBalancer"): _SpecificExtractor(
        "AWSELB", _ext_elbv2_create_load_balancer
    ),
    ("sqs.amazonaws.com", "CreateQueue"): _SpecificExtractor("AWSQueueService", _ext_sqs_create_queue),
    ("workspaces.amazonaws.com", "CreateWorkspaces"): _SpecificExtractor("AmazonWorkSpaces", _ext_workspaces_create_workspaces),
    # --- Protocolo json/rest-json/smithy — capitalização do botocore confiável ---
    # BUG CORRIGIDO (2026-09-30): o eventName real do Lambda no CloudTrail é
    # "CreateFunction20150331" (sufixo de versão da API — documentado pela
    # AWS), então a regra do EventBridge com "CreateFunction" nunca casava
    # e nenhuma função Lambda era tagueada pela Etapa 3. O parser agora
    # normaliza o sufixo (`normalize_event_name`) e o event pattern lista os
    # dois nomes. O CloudTrail também grava `functionArn` em lowerCamelCase
    # — extractor tolerante + fallback pelo nome pedido.
    # --- Deadline Cloud e WorkSpaces Core Managed Instances: formatos de ARN
    # confirmados na Service Authorization Reference da AWS (2026-09-30) —
    # a resposta da API só traz IDs. ---
    ("deadline.amazonaws.com", "CreateFarm"): _SpecificExtractor(
        "AWSDeadlineCloud", _constructed("arn:aws:deadline:{region}:{account_id}:farm/{value}", "responseElements", "farmId")
    ),
    ("deadline.amazonaws.com", "CreateQueue"): _SpecificExtractor("AWSDeadlineCloud", _ext_deadline_filho("queue", "queueId")),
    ("deadline.amazonaws.com", "CreateFleet"): _SpecificExtractor("AWSDeadlineCloud", _ext_deadline_filho("fleet", "fleetId")),
    ("workspaces-instances.amazonaws.com", "CreateWorkspaceInstance"): _SpecificExtractor(
        "AmazonWorkSpacesInstances",
        _constructed(
            "arn:aws:workspaces-instances:{region}:{account_id}:workspaceinstance/{value}", "responseElements", "WorkspaceInstanceId"
        ),
    ),
    # --- Item 10 (aprovado em 2026-09-30) ---
    ("appstream.amazonaws.com", "CreateFleet"): _SpecificExtractor("AmazonAppStream", _direct("Fleet", "Arn")),
    ("appstream.amazonaws.com", "CreateImageBuilder"): _SpecificExtractor("AmazonAppStream", _direct("ImageBuilder", "Arn")),
    ("bedrock-agentcore.amazonaws.com", "CreateAgentRuntime"): _SpecificExtractor("AmazonBedrockAgentCore", _direct("agentRuntimeArn")),
    ("bedrock-agentcore.amazonaws.com", "CreateBrowser"): _SpecificExtractor("AmazonBedrockAgentCore", _direct("browserArn")),
    ("bedrock-agentcore.amazonaws.com", "CreateCodeInterpreter"): _SpecificExtractor("AmazonBedrockAgentCore", _direct("codeInterpreterArn")),
    ("bedrock-agentcore.amazonaws.com", "CreateGateway"): _SpecificExtractor("AmazonBedrockAgentCore", _direct("gatewayArn")),
    ("bedrock-agentcore.amazonaws.com", "CreateMemory"): _SpecificExtractor("AmazonBedrockAgentCore", _direct("memory", "arn")),
    ("bedrock-agentcore.amazonaws.com", "CreateWorkloadIdentity"): _SpecificExtractor("AmazonBedrockAgentCore", _direct("workloadIdentityArn")),
    ("comprehend.amazonaws.com", "CreateEndpoint"): _SpecificExtractor("comprehend", _direct("EndpointArn")),
    ("comprehend.amazonaws.com", "CreateDocumentClassifier"): _SpecificExtractor("comprehend", _direct("DocumentClassifierArn")),
    ("comprehend.amazonaws.com", "CreateEntityRecognizer"): _SpecificExtractor("comprehend", _direct("EntityRecognizerArn")),
    ("datasync.amazonaws.com", "CreateTask"): _SpecificExtractor("AWSDataSync", _direct("TaskArn")),
    # Glue: CreateJob só devolve o nome; CreateCrawler não devolve nada.
    ("glue.amazonaws.com", "CreateJob"): _SpecificExtractor(
        "AWSGlue",
        _first_of(
            _constructed("arn:aws:glue:{region}:{account_id}:job/{value}", "responseElements", "Name"),
            _constructed("arn:aws:glue:{region}:{account_id}:job/{value}", "requestParameters", "Name"),
        ),
    ),
    ("glue.amazonaws.com", "CreateCrawler"): _SpecificExtractor(
        "AWSGlue", _constructed("arn:aws:glue:{region}:{account_id}:crawler/{value}", "requestParameters", "Name")
    ),
    ("iot.amazonaws.com", "CreateBillingGroup"): _SpecificExtractor("AWSIoT", _direct("billingGroupArn")),
    ("iotsitewise.amazonaws.com", "CreateAsset"): _SpecificExtractor("AWSIoTSiteWise", _direct("assetArn")),
    ("iotsitewise.amazonaws.com", "CreateAssetModel"): _SpecificExtractor("AWSIoTSiteWise", _direct("assetModelArn")),
    ("iotsitewise.amazonaws.com", "CreateGateway"): _SpecificExtractor("AWSIoTSiteWise", _direct("gatewayArn")),
    ("omics.amazonaws.com", "CreateSequenceStore"): _SpecificExtractor("AmazonOmics", _direct("arn")),
    ("omics.amazonaws.com", "CreateReferenceStore"): _SpecificExtractor("AmazonOmics", _direct("arn")),
    ("omics.amazonaws.com", "CreateWorkflow"): _SpecificExtractor("AmazonOmics", _direct("arn")),
    ("quicksight.amazonaws.com", "RegisterUser"): _SpecificExtractor("AmazonQuickSight", _direct("User", "Arn")),
    ("quicksight.amazonaws.com", "CreateDataSource"): _SpecificExtractor("AmazonQuickSight", _direct("Arn")),
    ("quicksight.amazonaws.com", "CreateDataSet"): _SpecificExtractor("AmazonQuickSight", _direct("Arn")),
    ("quicksight.amazonaws.com", "CreateDashboard"): _SpecificExtractor("AmazonQuickSight", _direct("Arn")),
    ("quicksight.amazonaws.com", "CreateTemplate"): _SpecificExtractor("AmazonQuickSight", _direct("Arn")),
    ("sagemaker.amazonaws.com", "CreateEndpoint"): _SpecificExtractor("AmazonSageMaker", _direct("EndpointArn")),
    ("sagemaker.amazonaws.com", "CreateNotebookInstance"): _SpecificExtractor("AmazonSageMaker", _direct("NotebookInstanceArn")),
    ("sagemaker.amazonaws.com", "CreateDomain"): _SpecificExtractor("AmazonSageMaker", _direct("DomainArn")),
    ("sagemaker.amazonaws.com", "CreateApp"): _SpecificExtractor("AmazonSageMaker", _direct("AppArn")),
    # Security Hub: EnableSecurityHub não devolve nada; o hub tem ARN fixo
    # por conta/região (formato que o próprio DescribeHub devolve).
    ("securityhub.amazonaws.com", "EnableSecurityHub"): _SpecificExtractor(
        "AWSSecurityHub", lambda detail, account_id, region: [f"arn:aws:securityhub:{region}:{account_id}:hub/default"]
    ),
    # --- Serviços que entraram no CSV em 2026-09-30 ---
    ("scn.amazonaws.com", "CreateInstance"): _SpecificExtractor(
        "AWSSupplyChain",
        # Formato confirmado pelo pattern de ARN do modelo botocore
        # (arn:aws:scn:<regiao>:<conta>:instance/<uuid>); a resposta só traz o ID.
        _constructed("arn:aws:scn:{region}:{account_id}:instance/{value}", "responseElements", "instance", "instanceId"),
    ),
    ("dax.amazonaws.com", "CreateCluster"): _SpecificExtractor("AmazonDAX", _direct("Cluster", "ClusterArn")),
    ("sms-voice.amazonaws.com", "CreateConfigurationSet"): _SpecificExtractor("AmazonPinpoint", _direct("ConfigurationSetArn")),
    ("sms-voice.amazonaws.com", "CreatePool"): _SpecificExtractor("AmazonPinpoint", _direct("PoolArn")),
    ("sms-voice.amazonaws.com", "RequestPhoneNumber"): _SpecificExtractor("AmazonPinpoint", _direct("PhoneNumberArn")),
    ("sms-voice.amazonaws.com", "RequestSenderId"): _SpecificExtractor("AmazonPinpoint", _direct("SenderIdArn")),
    # Pinpoint (rest-json com payload): o CloudTrail pode gravar o corpo da
    # resposta com ou sem o wrapper ApplicationResponse — tenta os dois.
    ("mobiletargeting.amazonaws.com", "CreateApp"): _SpecificExtractor(
        "AmazonPinpoint", _first_of(_direct("ApplicationResponse", "Arn"), _direct("Arn"))
    ),
    ("pinpoint.amazonaws.com", "CreateApp"): _SpecificExtractor(
        "AmazonPinpoint", _first_of(_direct("ApplicationResponse", "Arn"), _direct("Arn"))
    ),
    ("gameliftstreams.amazonaws.com", "CreateStreamGroup"): _SpecificExtractor("AmazonGameLiftStreams", _direct("Arn")),
    ("gameliftstreams.amazonaws.com", "CreateApplication"): _SpecificExtractor("AmazonGameLiftStreams", _direct("Arn")),
    # RTB Fabric: formato confirmado pelo pattern de ARN do modelo
    # (arn:aws:rtbfabric:<regiao>:<conta>:gateway/<id>); resposta só traz o ID.
    ("rtbfabric.amazonaws.com", "CreateRequesterGateway"): _SpecificExtractor(
        "AWSRTBFabric", _constructed("arn:aws:rtbfabric:{region}:{account_id}:gateway/{value}", "responseElements", "gatewayId")
    ),
    ("rtbfabric.amazonaws.com", "CreateResponderGateway"): _SpecificExtractor(
        "AWSRTBFabric", _constructed("arn:aws:rtbfabric:{region}:{account_id}:gateway/{value}", "responseElements", "gatewayId")
    ),
    # SES — nenhuma operação de criação devolve ARN (exceto CreateTenant).
    # Construído a partir do nome pedido; formato de identidade confirmado
    # pela documentação do modelo sesv2 (arn:aws:ses:<regiao>:<conta>:identity/<nome>).
    ("ses.amazonaws.com", "CreateEmailIdentity"): _SpecificExtractor(
        "AmazonSES", _constructed("arn:aws:ses:{region}:{account_id}:identity/{value}", "requestParameters", "EmailIdentity")
    ),
    ("ses.amazonaws.com", "VerifyEmailIdentity"): _SpecificExtractor(
        "AmazonSES", _constructed("arn:aws:ses:{region}:{account_id}:identity/{value}", "requestParameters", "EmailAddress")
    ),
    ("ses.amazonaws.com", "VerifyDomainIdentity"): _SpecificExtractor(
        "AmazonSES", _constructed("arn:aws:ses:{region}:{account_id}:identity/{value}", "requestParameters", "Domain")
    ),
    ("ses.amazonaws.com", "CreateConfigurationSet"): _SpecificExtractor(
        "AmazonSES",
        _first_of(
            # v2: ConfigurationSetName; v1: ConfigurationSet.Name
            _constructed("arn:aws:ses:{region}:{account_id}:configuration-set/{value}", "requestParameters", "ConfigurationSetName"),
            _constructed("arn:aws:ses:{region}:{account_id}:configuration-set/{value}", "requestParameters", "ConfigurationSet", "Name"),
        ),
    ),
    ("ses.amazonaws.com", "CreateTenant"): _SpecificExtractor("AmazonSES", _first_of(_direct("TenantArn"))),
    ("lambda.amazonaws.com", "CreateFunction"): _SpecificExtractor("AWSLambda", _ext_lambda_function),
    # DynamoDB grava `tableDescription.tableArn` (lowerCamelCase) — o
    # extractor anterior (PascalCase exato) nunca casaria com evento real.
    ("dynamodb.amazonaws.com", "CreateTable"): _SpecificExtractor("AmazonDynamoDB", _ext_dynamodb_table("TableName")),
    ("dynamodb.amazonaws.com", "RestoreTableFromBackup"): _SpecificExtractor(
        "AmazonDynamoDB", _ext_dynamodb_table("TargetTableName")
    ),
    ("dynamodb.amazonaws.com", "RestoreTableToPointInTime"): _SpecificExtractor(
        "AmazonDynamoDB", _ext_dynamodb_table("TargetTableName")
    ),
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
    # Mantém a PRIMEIRA ocorrência: AmazonDocDB/AmazonNeptune reaproveitam
    # regras do RDS (mesmo eventSource) e não podem sobrescrever AmazonRDS
    # aqui — a desambiguação real é feita por `product_code_resolver`.
    index: dict[tuple[str, str], str] = {}
    for code, rules in mapped_services().items():
        for rule in rules:
            index.setdefault((rule.event_source, normalize_event_name(rule.event_name)), code)
    return index


# Sufixos de versão de API que o CloudTrail acrescenta ao eventName de
# alguns serviços (documentado pela AWS para Lambda: "CreateFunction20150331",
# "UpdateFunctionConfiguration20150331v2"; CloudFront usa o formato de data
# da API, ex.: "CreateDistribution2020_05_31").
_SUFIXO_VERSAO_EVENT_NAME = re.compile(r"(\d{8}(v\d+)?|\d{4}_\d{2}_\d{2})$")


def normalize_event_name(event_name: str) -> str:
    """Remove o sufixo de versão de API do eventName, se houver."""
    return _SUFIXO_VERSAO_EVENT_NAME.sub("", event_name or "")


_MAPPED_EVENTS = _build_reverse_index()


# ---------------------------------------------------------------------------
# Eventos NATIVOS de serviço (não CloudTrail) — não exigem trail e cobrem o
# recurso independentemente de qual API o criou.
# ---------------------------------------------------------------------------

DETAIL_TYPE_EC2_STATE_CHANGE = "EC2 Instance State-change Notification"
DETAIL_TYPE_ECS_TASK_STATE_CHANGE = "ECS Task State Change"
TIPO_RECURSO_ECS_TASK = "task"


def _parse_ecs_task_change(event: dict, services: list[Service]) -> list[ExtractedResource]:
    """`ECS Task State Change` (RUNNING, Fargate) — item 14. O ARN da task
    vem pronto no evento. Só ARN no formato longo (com o nome do cluster)
    aceita tag; o formato curto antigo é ignorado aqui."""
    detail = event.get("detail") or {}
    task_arn = detail.get("taskArn")
    region = event.get("region")
    if not (isinstance(task_arn, str) and task_arn.startswith("arn:") and ":task/" in task_arn and region):
        return []
    if task_arn.split(":task/", 1)[1].count("/") < 1:
        return []  # formato curto (sem cluster) — não taggeável
    servico = _svc_name(services, "AmazonECS")
    if servico is None:
        return []
    return [ExtractedResource(arn=task_arn, servico=servico, regiao=region, tipo_recurso=TIPO_RECURSO_ECS_TASK)]


def _parse_ec2_state_change(event: dict, services: list[Service]) -> list[ExtractedResource]:
    """`EC2 Instance State-change Notification` (estado `running`): cobre
    instâncias criadas por RunInstances, CreateFleet (Karpenter), Auto
    Scaling (inclusive MixedInstancesPolicy/Spot, que usa CreateFleet por
    baixo), Spot Fleet e node groups do EKS — todos caminhos que o
    evento de CloudTrail `RunInstances` sozinho não cobre (itens 11 e 13 do
    plano). Também dispara em cada START de uma instância parada — a
    revalidação da Etapa 2c torna isso idempotente (vira `ja_ok`)."""
    detail = event.get("detail") or {}
    instance_id = detail.get("instance-id")
    account_id = event.get("account")
    region = event.get("region")
    if not (isinstance(instance_id, str) and _INSTANCE_ID_RE.match(instance_id) and account_id and region):
        return []
    servico = _svc_name(services, "AmazonEC2")
    if servico is None:
        return []
    return [
        ExtractedResource(
            arn=f"arn:aws:ec2:{region}:{account_id}:instance/{instance_id}",
            servico=servico,
            regiao=region,
            tipo_recurso=None,
        )
    ]


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------


def parse_creation_event(event: dict, services: list[Service] | None = None) -> list[ExtractedResource]:
    """Extrai 0, 1 ou N `ExtractedResource` de um evento do EventBridge —
    formato "AWS API Call via CloudTrail" ou um evento nativo de serviço
    suportado (hoje: EC2 Instance State-change Notification). Devolve lista
    vazia sempre que não há confiança suficiente para montar um ARN — nunca
    um valor parcial/adivinhado."""
    services = services if services is not None else load_services()

    if event.get("detail-type") == DETAIL_TYPE_EC2_STATE_CHANGE:
        return _parse_ec2_state_change(event, services)
    if event.get("detail-type") == DETAIL_TYPE_ECS_TASK_STATE_CHANGE:
        return _parse_ecs_task_change(event, services)

    detail = event.get("detail") or {}
    event_source = detail.get("eventSource")
    event_name = detail.get("eventName")
    if not event_source or not event_name:
        return []

    account_id = detail.get("recipientAccountId") or event.get("account")
    region = detail.get("awsRegion") or event.get("region")
    if not account_id or not region:
        return []

    key = (event_source, normalize_event_name(event_name))
    spec = _REGISTRY.get(key)

    if spec is not None:
        arns = spec.arn_builder(detail, account_id, region)
        codigo = (spec.product_code_resolver(detail) if spec.product_code_resolver else None) or spec.product_service_code
        servico = _svc_name(services, codigo, event_source)
        tipo_recurso = spec.tipo_recurso
    else:
        product_service_code = _MAPPED_EVENTS.get(key)
        if product_service_code is None:
            return []  # evento não está em event_mapping.py — nunca deveria acontecer se o pattern do EventBridge está correto, mas defensivo
        arns = _extract_generic(detail)
        servico = _svc_name(services, product_service_code, event_source)
        tipo_recurso = None

    if not arns or servico is None:
        return []

    return [ExtractedResource(arn=arn, servico=servico, regiao=region, tipo_recurso=tipo_recurso) for arn in arns]
