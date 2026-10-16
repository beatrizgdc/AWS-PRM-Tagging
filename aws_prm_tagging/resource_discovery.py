"""Descoberta de recursos por região (somente leitura).

1. Passo genérico: Resource Groups Tagging API (`get_resources`), sem filtro
   de tipo, classificando cada ARN pelo namespace (`services.classify_arn`).
   Traz ARN e tags atuais da maioria dos serviços do CSV.
2. Casos que a API genérica não cobre bem: Amazon Bedrock (application
   inference profiles) e Amazon EKS (cluster, node groups, nodes EC2,
   volumes EBS e load balancers, sem Fargate). Nodes e volumes também vêm
   do passo genérico; `report.dedupe_by_arn` mantém a entrada do EKS.
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

# Serviços com lógica dedicada, nunca reclassificados no passo genérico.
# Hoje `classify_arn` já não conhece "eks"/"bedrock"; o set protege contra
# duplicata se alguém os adicionar a `_NAMESPACE_TO_CODE`.
_DEDICATED_SERVICE_CODES = {"AmazonBedrock", "AmazonEKS"}

# Valores de "tipo_recurso": só para sub-recursos de EKS e Bedrock, onde o
# "servico" sozinho não diferencia o ARN.
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
    cluster_eks: str | None = None,
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
        "tag_similar_valores": {k: tags.get(k) for k in tag_similar_chaves},
        "iac": iac,
        # Node, volume e LB de EKS são cobrados como EC2/ELB. `servico` continua
        # "Amazon EKS" (roteia a escrita); este campo é para o relatório.
        "servico_faturamento": _SERVICO_FATURAMENTO_EKS.get(tipo_recurso, servico)
        if servico == _SERVICO_EKS_NOME
        else servico,
        "cluster_eks": cluster_eks,
    }


_SERVICO_EKS_NOME = "Amazon EKS"
_SERVICO_FATURAMENTO_EKS = {
    TIPO_RECURSO_NODE: "Amazon EC2",
    TIPO_RECURSO_EBS_VOLUME: "Amazon EC2",
    TIPO_RECURSO_LOAD_BALANCER: "Elastic Load Balancing",
}


# --- Passo genérico: Resource Groups Tagging API ---


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
    """Devolve `(recursos, falhas)`. `falhas` são dicts
        `{"regiao", "etapa", "erro"}`, gravados em `falhas_descoberta` no relatório."""
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


# --- Amazon Bedrock: application inference profiles ---


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
    """Application inference profiles do Bedrock. Devolve `(recursos, falhas)`.

        Profiles de sistema não aceitam tag e são excluídos por
        `typeEquals=APPLICATION`. Falha ao ler as tags de um profile vira falha
        (o estado real é desconhecido, não "sem tag")."""
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
            # AccessDenied vira falha: costuma ser falta de permissão, não "região sem
            # Bedrock", e esconder isso mostraria "0 profiles" com falsa confiança.
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


# --- Amazon EKS: cluster, node groups, load balancers, volumes EBS ---


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


# Lote conservador para filtros "instance-id" (a AWS não documenta limite).
_TAMANHO_LOTE_FILTRO_EC2 = 100


@with_backoff()
def _describe_instances_page(client, filters: list[dict], next_token: str | None) -> dict:
    kwargs: dict = {"Filters": filters}
    if next_token:
        kwargs["NextToken"] = next_token
    return client.describe_instances(**kwargs)


def _describe_instances_all(client, filters: list[dict]) -> list[dict]:
    """Pagina `describe_instances` e devolve todas as `Reservations`."""
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
    """Fallback para nodes self-managed, pelas tags que EKS e Kubernetes
        aplicam nas instâncias ("eks:cluster-name" e
        "kubernetes.io/cluster/<nome>"). Fargate não aparece (não é EC2)."""
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
    """Lista os load balancers da região com tags, uma vez para todos os
        clusters. Devolve `(resultado, falha_msg)`; `falha_msg` só quando a
        listagem inteira ficou comprometida."""
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
    """Filtro local: LBs do AWS Load Balancer Controller pelas tags
        `elbv2.k8s.aws/cluster` (ALB) e `kubernetes.io/cluster/<nome>` (NLB e
        controlador legado)."""
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
    """Devolve `(recursos, falhas)`. Registra falhas de região, cluster e
        node group; falhas mais granulares (um ASG, um lote de instâncias) só
        vão para o log, pois os mesmos EC2/EBS ainda vêm do passo genérico."""
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

    # Uma listagem de LBs para todos os clusters, só se houver cluster.
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
                cluster_eks=cluster_name,
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
                    cluster_eks=cluster_name,
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
                    arn,
                    servico_nome,
                    region,
                    tags,
                    expected_tag_value,
                    tipo_recurso=TIPO_RECURSO_NODE,
                    cluster_eks=cluster_name,
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
                    cluster_eks=cluster_name,
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
                    cluster_eks=cluster_name,
                )
            )

    logger.info(
        "Região %s: %d recursos EKS encontrados (%d clusters)",
        region,
        len(results),
        len(cluster_names),
    )
    return results, falhas
