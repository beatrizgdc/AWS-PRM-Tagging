"""Descoberta de recursos por região — 100% somente-leitura.

Estratégia:
  1. Passo genérico via Resource Groups Tagging API (`get_resources`), sem
     filtro de tipo, classificando cada ARN retornado pelo namespace
     (ver `services.classify_arn`). Cobre a maioria dos serviços do CSV de
     uma só vez, já trazendo ARN + tags atuais.
  2. Casos especiais que a API genérica não cobre corretamente (conforme o
     guia oficial): Amazon Bedrock (application inference profiles) e Amazon
     EKS (cluster, node groups, nodes/instâncias EC2, volumes EBS, load
     balancers — excluindo Fargate). Esses namespaces são deliberadamente
     omitidos do mapeamento genérico para não gerar duplicatas — e, como as
     instâncias EC2/volumes EBS dos nodes SÃO capturadas pelo passo genérico
     de qualquer forma (não estão em `_DEDICATED_SERVICE_CODES`), o
     resultado final é deduplicado por ARN em `report.dedupe_by_arn`,
     mantendo a entrada mais específica (a do EKS).

Nenhuma função aqui cria, altera ou remove recursos/tags.
"""
from __future__ import annotations

import logging

import boto3
from botocore.exceptions import ClientError

from .iac_detection import detect_iac
from .services import Service, classify_arn
from .tag_status import find_similar_tag_keys, get_tag_status, tags_list_to_dict
from .retry import with_backoff

logger = logging.getLogger(__name__)

# Serviços cobertos por lógica dedicada — nunca reclassificados no passo genérico.
#
# Defesa em profundidade: hoje "eks" e "bedrock" (plano, não AgentCore) nem
# existem em `services._NAMESPACE_TO_CODE`, então `classify_arn` já devolve
# `None` pra esses namespaces antes de qualquer coisa chegar aqui — este set
# não é o que efetivamente exclui EKS/Bedrock da descoberta genérica agora.
# Ele existe para continuar protegendo contra duplicata caso alguém adicione
# "eks"/"bedrock" a `_NAMESPACE_TO_CODE` no futuro (ex.: pra cobrir um tipo de
# recurso desses serviços que não seja tratado por resource_discovery.py).
_DEDICATED_SERVICE_CODES = {"AmazonBedrock", "AmazonEKS"}

# Valores possíveis para o campo "tipo_recurso" — só preenchido para os
# sub-recursos de EKS e Bedrock, onde o "servico" sozinho (nome da linha do
# CSV) não diferencia qual ARN é qual. None no passo genérico, onde o
# "servico" já identifica o recurso sem ambiguidade.
TIPO_RECURSO_CLUSTER = "cluster"
TIPO_RECURSO_NODE_GROUP = "node_group"
TIPO_RECURSO_NODE = "node"
TIPO_RECURSO_EBS_VOLUME = "ebs_volume"
TIPO_RECURSO_LOAD_BALANCER = "load_balancer"
TIPO_RECURSO_APPLICATION_INFERENCE_PROFILE = "application_inference_profile"


def _build_resource(
    arn: str,
    servico: str,
    regiao: str,
    tags: dict,
    expected_tag_value: str,
    tipo_recurso: str | None = None,
) -> dict:
    status_tag, valor_encontrado = get_tag_status(tags, expected_tag_value)
    iac = detect_iac(tags)
    tag_similar_chaves = find_similar_tag_keys(tags)
    return {
        "arn": arn,
        "servico": servico,
        "regiao": regiao,
        "tipo_recurso": tipo_recurso,
        "status_tag": status_tag,
        "valor_tag_encontrado": valor_encontrado,
        "tag_similar_encontrada": bool(tag_similar_chaves),
        "tag_similar_chaves": tag_similar_chaves,
        "iac": iac,
    }


# ---------------------------------------------------------------------------
# Passo genérico: Resource Groups Tagging API
# ---------------------------------------------------------------------------


@with_backoff()
def _get_resources_page(client, pagination_token: str | None) -> dict:
    kwargs: dict = {"ResourcesPerPage": 100}
    if pagination_token:
        kwargs["PaginationToken"] = pagination_token
    return client.get_resources(**kwargs)


def discover_generic_resources(
    session: boto3.Session,
    region: str,
    services: list[Service],
    expected_tag_value: str,
) -> tuple[list[dict], list[dict]]:
    """Devolve `(recursos, falhas)`. `falhas` é uma lista de dicts
    `{"regiao", "etapa", "erro"}` no mesmo formato que `main.py` já grava em
    `falhas_descoberta` — antes, uma falha aqui (ex.: `AccessDenied`,
    throttling esgotado, erro no meio da paginação) só ia para o log; o
    `try/except Exception` de `main.py` nunca via essas falhas porque eram
    capturadas aqui dentro. Como a Etapa 4/dashboard só consome o JSON, não
    o log da execução, isso escondia falhas reais em escala."""
    client = session.client("resourcegroupstaggingapi", region_name=region)
    results: list[dict] = []
    falhas: list[dict] = []
    token = None
    pages = 0
    try:
        while True:
            resp = _get_resources_page(client, token)
            pages += 1
            for mapping in resp.get("ResourceTagMappingList", []):
                arn = mapping["ResourceARN"]
                svc = classify_arn(arn, services)
                if svc is None or svc.product_service_code in _DEDICATED_SERVICE_CODES:
                    continue
                tags = tags_list_to_dict(mapping.get("Tags", []))
                results.append(
                    _build_resource(arn, svc.name, region, tags, expected_tag_value)
                )
            token = resp.get("PaginationToken")
            if not token:
                break
    except ClientError as exc:
        logger.exception(
            "Falha ao consultar Resource Groups Tagging API em %s (página %d) — "
            "prosseguindo com o que já foi coletado nesta região",
            region,
            pages,
        )
        erro = exc.response.get("Error", {})
        falhas.append(
            {
                "regiao": region,
                "etapa": "generico",
                "erro": f"get_resources (página {pages + 1}): {erro.get('Code', '')}: "
                f"{erro.get('Message', str(exc))}",
            }
        )
    logger.info(
        "Região %s: %d recursos elegíveis encontrados via Resource Groups Tagging API",
        region,
        len(results),
    )
    return results, falhas


# ---------------------------------------------------------------------------
# Amazon Bedrock — application inference profiles
# ---------------------------------------------------------------------------


@with_backoff()
def _list_inference_profiles_page(client, next_token: str | None) -> dict:
    kwargs: dict = {"typeEquals": "APPLICATION", "maxResults": 100}
    if next_token:
        kwargs["nextToken"] = next_token
    return client.list_inference_profiles(**kwargs)


@with_backoff()
def _list_tags_for_bedrock_resource(client, resource_arn: str) -> dict:
    return client.list_tags_for_resource(resourceARN=resource_arn)


def discover_bedrock_resources(
    session: boto3.Session,
    region: str,
    services: list[Service],
    expected_tag_value: str,
) -> tuple[list[dict], list[dict]]:
    """Enumera Amazon Bedrock application inference profiles. Devolve
    `(recursos, falhas)` — ver docstring de `discover_generic_resources`.

    Profiles do tipo sistema (cross-region) não suportam tag e são ignorados
    por construção: `typeEquals=APPLICATION` já os exclui do list_inference_profiles.

    Falha ao ler as tags de UM profile é tratada como `falha`, não só um
    log: sem essa leitura o recurso entra no relatório com `tags={}` (ou
    seja, "sem tag"), quando na verdade o estado real é desconhecido — a
    Etapa 2a decidiria `taguear` em cima de uma suposição, não de um fato
    confirmado (a revalidação da Etapa 2c ainda impede a escrita errada,
    mas o relatório de compliance da Etapa 1 ficaria enganoso sem esta
    falha registrada)."""
    bedrock_service = next(
        (s for s in services if s.product_service_code == "AmazonBedrock"), None
    )
    servico_nome = bedrock_service.name if bedrock_service else "Amazon Bedrock"

    client = session.client("bedrock", region_name=region)
    results: list[dict] = []
    falhas: list[dict] = []
    next_token = None
    try:
        while True:
            resp = _list_inference_profiles_page(client, next_token)
            for profile in resp.get("inferenceProfileSummaries", []):
                arn = profile["inferenceProfileArn"]
                try:
                    tags_resp = _list_tags_for_bedrock_resource(client, arn)
                    tags = tags_list_to_dict(tags_resp.get("tags", []))
                except ClientError as exc:
                    logger.exception(
                        "Falha ao obter tags do inference profile %s em %s — "
                        "tratando como sem tags e registrando falha de descoberta",
                        arn,
                        region,
                    )
                    tags = {}
                    erro = exc.response.get("Error", {})
                    falhas.append(
                        {
                            "regiao": region,
                            "etapa": "bedrock",
                            "erro": f"list_tags_for_resource({arn}): {erro.get('Code', '')}: "
                            f"{erro.get('Message', str(exc))}",
                        }
                    )
                results.append(
                    _build_resource(
                        arn,
                        servico_nome,
                        region,
                        tags,
                        expected_tag_value,
                        tipo_recurso=TIPO_RECURSO_APPLICATION_INFERENCE_PROFILE,
                    )
                )
            next_token = resp.get("nextToken")
            if not next_token:
                break
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("AccessDeniedException", "UnrecognizedClientException"):
            # NÃO é tratado como benigno silenciosamente: `AccessDenied`
            # tipicamente indica falta de permissão IAM, não "região sem
            # Bedrock" — as duas coisas não dão pra distinguir com certeza
            # só pelo código do erro (precisaria de confirmação em sandbox,
            # ver melhorias-futuras.md). Uma versão anterior assumia que era
            # sempre benigno e não registrava falha — testado e comprovado
            # errado: uma role sem `bedrock:ListInferenceProfiles` produz
            # exatamente esse erro em TODAS as regiões, e o relatório
            # mostraria "0 profiles" com confiança total, escondendo um
            # problema de permissão real na conta inteira. Por isso sempre
            # vira falha agora, só com um nível de log mais baixo.
            logger.warning(
                "Falha ao listar Bedrock application inference profiles em %s (%s) — "
                "pode ser região sem Bedrock ou falta de permissão IAM, não dá para "
                "distinguir com certeza só pelo código de erro",
                region,
                error_code,
            )
        else:
            logger.exception(
                "Falha ao listar Bedrock application inference profiles em %s", region
            )
        erro = exc.response.get("Error", {})
        falhas.append(
            {
                "regiao": region,
                "etapa": "bedrock",
                "erro": f"list_inference_profiles: {erro.get('Code', '')}: {erro.get('Message', str(exc))}",
            }
        )
    logger.info(
        "Região %s: %d Bedrock application inference profiles encontrados",
        region,
        len(results),
    )
    return results, falhas


# ---------------------------------------------------------------------------
# Amazon EKS — cluster, node groups, load balancers, volumes EBS
# ---------------------------------------------------------------------------


@with_backoff()
def _list_clusters_page(client, next_token: str | None) -> dict:
    kwargs: dict = {}
    if next_token:
        kwargs["nextToken"] = next_token
    return client.list_clusters(**kwargs)


@with_backoff()
def _describe_cluster(client, name: str) -> dict:
    return client.describe_cluster(name=name)["cluster"]


@with_backoff()
def _list_nodegroups_page(client, cluster_name: str, next_token: str | None) -> dict:
    kwargs: dict = {"clusterName": cluster_name}
    if next_token:
        kwargs["nextToken"] = next_token
    return client.list_nodegroups(**kwargs)


@with_backoff()
def _describe_nodegroup(client, cluster_name: str, nodegroup_name: str) -> dict:
    return client.describe_nodegroup(clusterName=cluster_name, nodegroupName=nodegroup_name)[
        "nodegroup"
    ]


@with_backoff()
def _describe_auto_scaling_group(client, asg_name: str) -> dict:
    return client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# Tamanho de lote conservador para os filtros "instance-id"/"attachment.instance-id"
# abaixo — a AWS não documenta um limite fixo e único para o número de
# valores num filtro de describe_instances/describe_volumes, então 100 é
# uma escolha conservadora (bem abaixo de qualquer limite conhecido) em vez
# de assumir que a lista de instance_ids de um cluster nunca vai ser grande
# o suficiente para importar.
_TAMANHO_LOTE_FILTRO_EC2 = 100


@with_backoff()
def _describe_instances_page(client, filters: list[dict], next_token: str | None) -> dict:
    kwargs: dict = {"Filters": filters}
    if next_token:
        kwargs["NextToken"] = next_token
    return client.describe_instances(**kwargs)


def _describe_instances_all(client, filters: list[dict]) -> list[dict]:
    """Pagina `describe_instances` inteiro — devolve todas as `Reservations`.

    Sem isso, uma resposta paginada (conta/cluster com muitas instâncias)
    perderia silenciosamente as instâncias das páginas seguintes."""
    reservations: list[dict] = []
    token = None
    while True:
        resp = _describe_instances_page(client, filters, token)
        reservations.extend(resp.get("Reservations", []))
        token = resp.get("NextToken")
        if not token:
            break
    return reservations


@with_backoff()
def _describe_volumes_page(client, instance_ids: list[str], next_token: str | None) -> dict:
    kwargs: dict = {"Filters": [{"Name": "attachment.instance-id", "Values": instance_ids}]}
    if next_token:
        kwargs["NextToken"] = next_token
    return client.describe_volumes(**kwargs)


def _describe_volumes_all(client, instance_ids: list[str]) -> list[dict]:
    """Pagina `describe_volumes` inteiro — devolve todos os `Volumes`."""
    volumes: list[dict] = []
    token = None
    while True:
        resp = _describe_volumes_page(client, instance_ids, token)
        volumes.extend(resp.get("Volumes", []))
        token = resp.get("NextToken")
        if not token:
            break
    return volumes


@with_backoff()
def _describe_load_balancers_page(client, marker: str | None) -> dict:
    kwargs: dict = {}
    if marker:
        kwargs["Marker"] = marker
    return client.describe_load_balancers(**kwargs)


@with_backoff()
def _describe_lb_tags(client, arns: list[str]) -> dict:
    return client.describe_tags(ResourceArns=arns)


def _list_clusters(eks_client) -> list[str]:
    names: list[str] = []
    token = None
    while True:
        resp = _list_clusters_page(eks_client, token)
        names.extend(resp.get("clusters", []))
        token = resp.get("nextToken")
        if not token:
            break
    return names


def _list_nodegroups(eks_client, cluster_name: str) -> list[str]:
    names: list[str] = []
    token = None
    while True:
        resp = _list_nodegroups_page(eks_client, cluster_name, token)
        names.extend(resp.get("nodegroups", []))
        token = resp.get("nextToken")
        if not token:
            break
    return names


def _get_asg_instance_ids(autoscaling_client, asg_name: str) -> list[str]:
    try:
        resp = _describe_auto_scaling_group(autoscaling_client, asg_name)
    except ClientError:
        logger.exception("Falha ao descrever Auto Scaling Group %s", asg_name)
        return []
    groups = resp.get("AutoScalingGroups", [])
    if not groups:
        return []
    return [i["InstanceId"] for i in groups[0].get("Instances", [])]


def _get_node_instance_ids_by_cluster_tags(ec2_client, cluster_name: str) -> list[str]:
    """Fallback best-effort para nodes self-managed: usa as tags que o EKS e o
    Kubernetes cloud-provider aplicam automaticamente nas instâncias EC2 dos
    nodes ("eks:cluster-name" em managed nodes; "kubernetes.io/cluster/<nome>"
    em self-managed). Instâncias Fargate não aparecem aqui pois não são
    instâncias EC2 (Fargate on EKS é explicitamente não elegível)."""
    instance_ids: set[str] = set()
    filter_sets = [
        [
            {"Name": "tag:eks:cluster-name", "Values": [cluster_name]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
        ],
        [
            {"Name": f"tag:kubernetes.io/cluster/{cluster_name}", "Values": ["owned", "shared"]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
        ],
    ]
    for filters in filter_sets:
        try:
            reservations = _describe_instances_all(ec2_client, filters)
        except ClientError:
            logger.exception(
                "Falha ao buscar instâncias EC2 do cluster EKS %s com filtro %s",
                cluster_name,
                filters,
            )
            continue
        for reservation in reservations:
            for instance in reservation.get("Instances", []):
                instance_ids.add(instance["InstanceId"])
    return sorted(instance_ids)


def _instances_arns_and_tags(
    ec2_client, account_id: str, region: str, instance_ids: list[str]
) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for lote in _chunk(instance_ids, _TAMANHO_LOTE_FILTRO_EC2):
        try:
            reservations = _describe_instances_all(ec2_client, [{"Name": "instance-id", "Values": lote}])
        except ClientError:
            logger.exception("Falha ao obter tags das instâncias EKS %s", lote)
            continue
        for reservation in reservations:
            for instance in reservation.get("Instances", []):
                arn = f"arn:aws:ec2:{region}:{account_id}:instance/{instance['InstanceId']}"
                tags = tags_list_to_dict(instance.get("Tags", []))
                out.append((arn, tags))
    return out


def _volumes_arns_and_tags(
    ec2_client, account_id: str, region: str, instance_ids: list[str]
) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for lote in _chunk(instance_ids, _TAMANHO_LOTE_FILTRO_EC2):
        try:
            volumes = _describe_volumes_all(ec2_client, lote)
        except ClientError:
            logger.exception("Falha ao obter volumes EBS dos nodes EKS %s", lote)
            continue
        for volume in volumes:
            arn = f"arn:aws:ec2:{region}:{account_id}:volume/{volume['VolumeId']}"
            tags = tags_list_to_dict(volume.get("Tags", []))
            out.append((arn, tags))
    return out


def _list_region_load_balancers_with_tags(elbv2_client) -> tuple[list[tuple[str, dict]], str | None]:
    """Lista TODOS os load balancers da região com suas tags, uma vez só —
    reaproveitado por `_filter_load_balancers_for_cluster` para cada
    cluster, em vez de relistar a região inteira a cada cluster (uma conta
    com N clusters EKS fazia N varreduras completas da região antes desta
    correção).

    Devolve `(resultado, falha_msg)` — `falha_msg` só é preenchido quando a
    listagem falhou de forma a comprometer o resultado inteiro (não a falha
    de um batch de tags isolado, tratada à parte abaixo)."""
    all_arns: list[str] = []
    marker = None
    try:
        while True:
            resp = _describe_load_balancers_page(elbv2_client, marker)
            all_arns.extend(lb["LoadBalancerArn"] for lb in resp.get("LoadBalancers", []))
            marker = resp.get("NextMarker")
            if not marker:
                break
    except ClientError as exc:
        logger.exception("Falha ao listar load balancers da região para descoberta EKS")
        erro = exc.response.get("Error", {})
        return [], f"describe_load_balancers: {erro.get('Code', '')}: {erro.get('Message', str(exc))}"

    resultado: list[tuple[str, dict]] = []
    for batch in [all_arns[i : i + 20] for i in range(0, len(all_arns), 20)]:  # describe_tags aceita até 20 ARNs
        try:
            resp = _describe_lb_tags(elbv2_client, batch)
        except ClientError:
            logger.exception("Falha ao obter tags de load balancers (batch %s)", batch)
            continue
        for desc in resp.get("TagDescriptions", []):
            resultado.append((desc["ResourceArn"], tags_list_to_dict(desc.get("Tags", []))))
    return resultado, None


def _filter_load_balancers_for_cluster(
    region_load_balancers: list[tuple[str, dict]], cluster_name: str
) -> list[tuple[str, dict]]:
    """Heurística best-effort, sem nenhuma chamada de API (filtro puro sobre
    o resultado já coletado por `_list_region_load_balancers_with_tags`):
    identifica load balancers do AWS Load Balancer Controller pelas tags de
    convenção `elbv2.k8s.aws/cluster` (ALB via Ingress) e
    `kubernetes.io/cluster/<nome>` (NLB via Service, e também usado pelo
    controlador legado in-tree)."""
    return [
        (arn, tags)
        for arn, tags in region_load_balancers
        if tags.get("elbv2.k8s.aws/cluster") == cluster_name
        or tags.get(f"kubernetes.io/cluster/{cluster_name}") in ("owned", "shared")
    ]


def discover_eks_resources(
    session: boto3.Session,
    region: str,
    account_id: str,
    expected_tag_value: str,
) -> tuple[list[dict], list[dict]]:
    """Devolve `(recursos, falhas)` — ver docstring de
    `discover_generic_resources`. Cobre os pontos de falha "largos" (região
    inteira, cluster inteiro, node group inteiro); as falhas mais granulares
    dentro de um cluster (ASG isolado, um chunk de instâncias/volumes EC2)
    continuam só logadas — o mesmo EC2/EBS ainda aparece no relatório via
    `discover_generic_resources` mesmo se o enriquecimento específico de EKS
    falhar aqui, então o recurso em si não fica invisível, só perde o
    `tipo_recurso` mais específico."""
    eks_client = session.client("eks", region_name=region)
    ec2_client = session.client("ec2", region_name=region)
    autoscaling_client = session.client("autoscaling", region_name=region)
    elbv2_client = session.client("elbv2", region_name=region)

    results: list[dict] = []
    falhas: list[dict] = []
    servico_nome = "Amazon EKS"

    try:
        cluster_names = _list_clusters(eks_client)
    except ClientError as exc:
        logger.exception("Falha ao listar clusters EKS em %s", region)
        erro = exc.response.get("Error", {})
        falhas.append(
            {
                "regiao": region,
                "etapa": "eks",
                "erro": f"list_clusters: {erro.get('Code', '')}: {erro.get('Message', str(exc))}",
            }
        )
        return results, falhas

    # Uma varredura só de load balancers da região para todos os clusters
    # (ver docstring de `_list_region_load_balancers_with_tags`) — só roda
    # se houver pelo menos um cluster, para não pagar o custo em contas sem
    # EKS.
    region_load_balancers: list[tuple[str, dict]] = []
    if cluster_names:
        region_load_balancers, falha_lb = _list_region_load_balancers_with_tags(elbv2_client)
        if falha_lb is not None:
            falhas.append({"regiao": region, "etapa": "eks", "erro": falha_lb})

    for cluster_name in cluster_names:
        try:
            cluster = _describe_cluster(eks_client, cluster_name)
        except ClientError as exc:
            logger.exception("Falha ao descrever cluster EKS %s em %s", cluster_name, region)
            erro = exc.response.get("Error", {})
            falhas.append(
                {
                    "regiao": region,
                    "etapa": "eks",
                    "erro": f"describe_cluster({cluster_name}): {erro.get('Code', '')}: "
                    f"{erro.get('Message', str(exc))}",
                }
            )
            continue

        results.append(
            _build_resource(
                cluster["arn"],
                servico_nome,
                region,
                cluster.get("tags", {}),
                expected_tag_value,
                tipo_recurso=TIPO_RECURSO_CLUSTER,
            )
        )

        node_instance_ids: set[str] = set()
        try:
            nodegroup_names = _list_nodegroups(eks_client, cluster_name)
        except ClientError as exc:
            logger.exception("Falha ao listar node groups do cluster %s", cluster_name)
            erro = exc.response.get("Error", {})
            falhas.append(
                {
                    "regiao": region,
                    "etapa": "eks",
                    "erro": f"list_nodegroups({cluster_name}): {erro.get('Code', '')}: "
                    f"{erro.get('Message', str(exc))}",
                }
            )
            nodegroup_names = []

        for ng_name in nodegroup_names:
            try:
                nodegroup = _describe_nodegroup(eks_client, cluster_name, ng_name)
            except ClientError as exc:
                logger.exception(
                    "Falha ao descrever node group %s do cluster %s", ng_name, cluster_name
                )
                erro = exc.response.get("Error", {})
                falhas.append(
                    {
                        "regiao": region,
                        "etapa": "eks",
                        "erro": f"describe_nodegroup({cluster_name}/{ng_name}): {erro.get('Code', '')}: "
                        f"{erro.get('Message', str(exc))}",
                    }
                )
                continue
            results.append(
                _build_resource(
                    nodegroup["nodegroupArn"],
                    servico_nome,
                    region,
                    nodegroup.get("tags", {}),
                    expected_tag_value,
                    tipo_recurso=TIPO_RECURSO_NODE_GROUP,
                )
            )
            for asg in nodegroup.get("resources", {}).get("autoScalingGroups", []):
                asg_name = asg.get("name")
                if asg_name:
                    node_instance_ids.update(
                        _get_asg_instance_ids(autoscaling_client, asg_name)
                    )

        # Nodes self-managed (fora de qualquer node group gerenciado pelo EKS)
        node_instance_ids.update(
            _get_node_instance_ids_by_cluster_tags(ec2_client, cluster_name)
        )

        instance_ids = sorted(node_instance_ids)
        for arn, tags in _instances_arns_and_tags(ec2_client, account_id, region, instance_ids):
            results.append(
                _build_resource(
                    arn, servico_nome, region, tags, expected_tag_value, tipo_recurso=TIPO_RECURSO_NODE
                )
            )
        for arn, tags in _volumes_arns_and_tags(ec2_client, account_id, region, instance_ids):
            results.append(
                _build_resource(
                    arn,
                    servico_nome,
                    region,
                    tags,
                    expected_tag_value,
                    tipo_recurso=TIPO_RECURSO_EBS_VOLUME,
                )
            )

        for arn, tags in _filter_load_balancers_for_cluster(region_load_balancers, cluster_name):
            results.append(
                _build_resource(
                    arn,
                    servico_nome,
                    region,
                    tags,
                    expected_tag_value,
                    tipo_recurso=TIPO_RECURSO_LOAD_BALANCER,
                )
            )

    logger.info(
        "Região %s: %d recursos EKS encontrados (%d clusters)",
        region,
        len(results),
        len(cluster_names),
    )
    return results, falhas
