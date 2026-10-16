"""Recomendações de tagueamento NA ORIGEM — itens 12 e 14 do plano (decisão
de 2026-09-30). Somente leitura.

## Por que existe

Para alguns recursos, taguear depois de criados não é durável ou não
alcança o que gera a cobrança:

- **EKS** (guia PRM, págs. 69–70): nodes são substituídos o tempo todo
  (rolling update, Karpenter, Spot); load balancers e volumes são criados
  por controllers dentro do cluster. O caminho recomendado é configurar a
  origem: `TagSpecifications` no launch template, `tags` no `EC2NodeClass`
  do Karpenter, `defaultTags` do AWS Load Balancer Controller e
  `extraVolumeTags` do driver EBS CSI.
- **ECS/Fargate**: o custo fica na task; tag no service só chega às tasks
  com `propagateTags`. A automação NUNCA altera o service
  (`ecs:UpdateService` não é restringível ao parâmetro) — recomenda.
- **DRS**: os recursos de replicação nascem com as tags de
  `stagingAreaTags` do replication configuration template.
- **IoT Core**: o consumo é atribuído pelo billing group — as things
  precisam estar num billing group tagueado (validação de 2026-09-30).

A Etapa 3 continua tagueando reativamente como rede de segurança; estas
recomendações tornam o resultado durável e cobrem o que a reação não
alcança. Alterar launch template de node group dispara rolling update dos
nodes — por isso é sempre recomendação para o cliente aplicar no IaC dele.

Saída: lista de dicts (JSON) + um Markdown legível para enviar ao cliente.
"""
from __future__ import annotations

import json
import logging
from typing import Iterable

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

from .tag_status import TAG_KEY, tags_list_to_dict

logger = logging.getLogger(__name__)

_CFG = Config(retries={"mode": "adaptive", "max_attempts": 10})

IAM_LEITURA = sorted(
    {
        "eks:ListClusters", "eks:DescribeCluster", "eks:ListNodegroups", "eks:DescribeNodegroup",
        "ec2:DescribeLaunchTemplateVersions", "ec2:DescribeInstances", "ec2:DescribeVolumes",
        "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTags",
        "ecs:ListClusters", "ecs:ListServices", "ecs:DescribeServices",
        "drs:DescribeReplicationConfigurationTemplates",
        "iot:ListThings", "iot:ListBillingGroups", "iot:ListTagsForResource",
    }
)


def _rec(regiao, servico, alvo, tipo, situacao, recomendacao, artefato=None, formato=None, impacto=None) -> dict:
    return {
        "regiao": regiao,
        "servico": servico,
        "alvo": alvo,
        "tipo": tipo,
        "situacao": situacao,
        "recomendacao": recomendacao,
        "artefato": {"formato": formato, "conteudo": artefato} if artefato else None,
        "impacto": impacto,
    }


def _tag_specs(valor: str) -> list[dict]:
    return [
        {"ResourceType": rt, "Tags": [{"Key": TAG_KEY, "Value": valor}]}
        for rt in ("instance", "volume", "network-interface")
    ]


def _tem_tag_prm(specs: Iterable[dict], resource_type: str, valor: str) -> bool:
    for spec in specs or []:
        if spec.get("ResourceType") == resource_type:
            if tags_list_to_dict(spec.get("Tags") or []).get(TAG_KEY) == valor:
                return True
    return False


# ---------------------------------------------------------------------------
# EKS (item 12)
# ---------------------------------------------------------------------------


def _eks(session, regiao: str, valor: str) -> list[dict]:
    eks = session.client("eks", region_name=regiao, config=_CFG)
    ec2 = session.client("ec2", region_name=regiao, config=_CFG)
    recs: list[dict] = []
    clusters = [c for p in eks.get_paginator("list_clusters").paginate() for c in p.get("clusters", [])]
    for cluster in clusters:
        # --- node groups gerenciados: launch template com TagSpecifications
        for p in eks.get_paginator("list_nodegroups").paginate(clusterName=cluster):
            for ng in p.get("nodegroups", []):
                info = eks.describe_nodegroup(clusterName=cluster, nodegroupName=ng)["nodegroup"]
                lt = info.get("launchTemplate")
                alvo = f"{cluster}/{ng}"
                if not lt:
                    recs.append(_rec(
                        regiao, "Amazon EKS", alvo, "eks_node_group_sem_launch_template",
                        "Node group gerenciado sem launch template: as instâncias e os volumes root não herdam a tag.",
                        "Criar um launch template com TagSpecifications (instance, volume, network-interface) contendo "
                        f"{TAG_KEY} e associá-lo ao node group no IaC.",
                        {"TagSpecifications": _tag_specs(valor)}, "json (LaunchTemplateData)",
                        "Trocar o launch template de um node group dispara rolling update dos nodes — agendar janela.",
                    ))
                    continue
                versao = lt.get("version") or "$Default"
                kwargs = {"Versions": [versao]}
                kwargs["LaunchTemplateId" if lt.get("id") else "LaunchTemplateName"] = lt.get("id") or lt.get("name")
                try:
                    versoes = ec2.describe_launch_template_versions(**kwargs).get("LaunchTemplateVersions", [])
                except ClientError as exc:
                    logger.warning("Launch template de %s ilegível: %s", alvo, exc.response.get("Error", {}).get("Code"))
                    continue
                specs = ((versoes[0] if versoes else {}).get("LaunchTemplateData") or {}).get("TagSpecifications", [])
                faltando = [rt for rt in ("instance", "volume") if not _tem_tag_prm(specs, rt, valor)]
                if faltando:
                    recs.append(_rec(
                        regiao, "Amazon EKS", alvo, "eks_launch_template_sem_tag",
                        f"Launch template {lt.get('name') or lt.get('id')} (versão {versao}) sem {TAG_KEY} para: "
                        + ", ".join(faltando) + ".",
                        "Publicar nova versão do launch template com as TagSpecifications abaixo e apontar o node group "
                        "para ela no IaC.",
                        {"TagSpecifications": _tag_specs(valor)}, "json (LaunchTemplateData)",
                        "Nova versão no node group dispara rolling update dos nodes — agendar janela.",
                    ))

        # --- Karpenter: instâncias com karpenter.sh/nodepool deste cluster sem a tag
        filtros = [
            {"Name": "tag-key", "Values": ["karpenter.sh/nodepool"]},
            {"Name": "tag-key", "Values": [f"kubernetes.io/cluster/{cluster}"]},
            {"Name": "instance-state-name", "Values": ["pending", "running"]},
        ]
        sem = total = 0
        for p in ec2.get_paginator("describe_instances").paginate(Filters=filtros):
            for res in p.get("Reservations", []):
                for inst in res.get("Instances", []):
                    total += 1
                    sem += tags_list_to_dict(inst.get("Tags") or []).get(TAG_KEY) != valor
        if sem:
            recs.append(_rec(
                regiao, "Amazon EKS", cluster, "eks_karpenter_sem_tag",
                f"{sem} de {total} node(s) do Karpenter sem {TAG_KEY}.",
                "Adicionar a tag em spec.tags de cada EC2NodeClass (o Karpenter aplica em instâncias, volumes e ENIs "
                "que cria).",
                f"apiVersion: karpenter.k8s.aws/v1\nkind: EC2NodeClass\nspec:\n  tags:\n    {TAG_KEY}: \"{valor}\"\n",
                "yaml (trecho de EC2NodeClass)",
                "Vale para nodes novos; os atuais são substituídos no ciclo normal do Karpenter (drift).",
            ))

    if not clusters:
        return recs

    # --- AWS Load Balancer Controller: LBs com elbv2.k8s.aws/cluster sem a tag
    elb = session.client("elbv2", region_name=regiao, config=_CFG)
    arns = [lb["LoadBalancerArn"] for p in elb.get_paginator("describe_load_balancers").paginate() for lb in p.get("LoadBalancers", [])]
    por_cluster: dict[str, list[str]] = {}
    for i in range(0, len(arns), 20):
        for d in elb.describe_tags(ResourceArns=arns[i : i + 20]).get("TagDescriptions", []):
            tags = tags_list_to_dict(d.get("Tags") or [])
            dono = tags.get("elbv2.k8s.aws/cluster")
            if dono and tags.get(TAG_KEY) != valor:
                por_cluster.setdefault(dono, []).append(d["ResourceArn"])
    for cluster, lbs in sorted(por_cluster.items()):
        recs.append(_rec(
            regiao, "Amazon EKS", cluster, "eks_load_balancer_controller_sem_tag",
            f"{len(lbs)} load balancer(s) criados pelo AWS Load Balancer Controller sem {TAG_KEY}.",
            "Configurar defaultTags no controller (vale para todo LB que ele criar) — ou, por recurso, as anotações "
            "alb.ingress.kubernetes.io/tags (Ingress) e service.beta.kubernetes.io/aws-load-balancer-additional-"
            "resource-tags (Service). O controller reconcilia as tags dos LBs que gerencia, então tag aplicada por "
            "fora pode não ser durável — a origem é o caminho confiável.",
            f"# values.yaml do Helm chart aws-load-balancer-controller\ndefaultTags:\n  {TAG_KEY}: \"{valor}\"\n",
            "yaml (Helm values)",
            "Aplicar os values reinicia o pod do controller; os LBs recebem a tag na próxima reconciliação.",
        ))

    # --- Driver EBS CSI: volumes de PVC sem a tag (nível região: o volume não
    #     carrega o nome do cluster de forma padronizada)
    sem = 0
    for p in ec2.get_paginator("describe_volumes").paginate(Filters=[{"Name": "tag:ebs.csi.aws.com/cluster", "Values": ["true"]}]):
        for v in p.get("Volumes", []):
            sem += tags_list_to_dict(v.get("Tags") or []).get(TAG_KEY) != valor
    if sem:
        recs.append(_rec(
            regiao, "Amazon EKS", "(região)", "eks_ebs_csi_sem_tag",
            f"{sem} volume(s) criados pelo driver EBS CSI sem {TAG_KEY}.",
            "Configurar extraVolumeTags no driver (equivale à flag --extra-tags do controller).",
            f"# values.yaml do Helm chart aws-ebs-csi-driver (ou configuração do add-on gerenciado)\ncontroller:\n  extraVolumeTags:\n    {TAG_KEY}: \"{valor}\"\n",
            "yaml (Helm values / configuração do add-on)",
            "Vale para volumes novos; os existentes são tagueados pela automação (Etapa 2c/3).",
        ))
    return recs


# ---------------------------------------------------------------------------
# ECS (item 14)
# ---------------------------------------------------------------------------


def _ecs(session, regiao: str, valor: str) -> list[dict]:
    ecs = session.client("ecs", region_name=regiao, config=_CFG)
    recs: list[dict] = []
    for p in ecs.get_paginator("list_clusters").paginate():
        for cluster in p.get("clusterArns", []):
            servicos = [a for q in ecs.get_paginator("list_services").paginate(cluster=cluster) for a in q.get("serviceArns", [])]
            for i in range(0, len(servicos), 10):
                for svc in ecs.describe_services(cluster=cluster, services=servicos[i : i + 10]).get("services", []):
                    if (svc.get("propagateTags") or "NONE") != "NONE":
                        continue
                    recs.append(_rec(
                        regiao, "Amazon ECS", svc["serviceArn"], "ecs_service_sem_propagate_tags",
                        f"Service sem propagateTags: as tasks não herdam a tag {TAG_KEY} do service (no Fargate o "
                        "custo fica na task).",
                        "Definir propagateTags = SERVICE no IaC do service (a automação tagueia o service e as tasks "
                        "em execução, mas não altera a configuração do service).",
                        "# CloudFormation\nPropagateTags: SERVICE\n\n# Terraform (aws_ecs_service)\npropagate_tags = \"SERVICE\"\n",
                        "yaml/hcl (trecho)",
                        "Vale para tasks novas; as atuais recebem a tag pela automação até serem substituídas. Não "
                        "força novo deploy.",
                    ))
    return recs


# ---------------------------------------------------------------------------
# DRS e IoT (item 10, validação de 2026-09-30)
# ---------------------------------------------------------------------------


# Códigos que significam "o serviço não está em uso nesta conta/região" —
# estado normal, não falha. Achado na sandbox em 2026-09-30: o DRS devolve
# UninitializedAccountException em toda conta que nunca o habilitou.
_SERVICO_NAO_INICIALIZADO = frozenset({"UninitializedAccountException"})


def _drs(session, regiao: str, valor: str) -> list[dict]:
    drs = session.client("drs", region_name=regiao, config=_CFG)
    recs: list[dict] = []
    try:
        paginas = list(drs.get_paginator("describe_replication_configuration_templates").paginate())
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in _SERVICO_NAO_INICIALIZADO:
            return []
        raise
    for p in paginas:
        for t in p.get("items", []):
            if (t.get("stagingAreaTags") or {}).get(TAG_KEY) == valor:
                continue
            recs.append(_rec(
                regiao, "AWS Elastic Disaster Recovery (DRS)", t.get("arn") or t.get("replicationConfigurationTemplateID"),
                "drs_staging_area_sem_tag",
                f"Replication configuration template sem {TAG_KEY} em stagingAreaTags: servidores de replicação, "
                "volumes e snapshots da staging area nascem sem a tag.",
                "Adicionar a tag em stagingAreaTags do template (e dos source servers já replicando, na configuração "
                "de replicação de cada um).",
                json.dumps({"stagingAreaTags": {TAG_KEY: valor}}, indent=2), "json",
                "Vale para recursos de replicação criados depois da mudança.",
            ))
    return recs


def _iot(session, regiao: str, valor: str) -> list[dict]:
    iot = session.client("iot", region_name=regiao, config=_CFG)
    tem_things = bool(iot.list_things(maxResults=1).get("things"))
    if not tem_things:
        return []
    grupos_prm = 0
    for p in iot.get_paginator("list_billing_groups").paginate():
        for g in p.get("billingGroups", []):
            tags = {t["Key"]: t["Value"] for t in iot.list_tags_for_resource(resourceArn=g["groupArn"]).get("tags", [])}
            grupos_prm += tags.get(TAG_KEY) == valor
    if grupos_prm:
        return []
    return [_rec(
        regiao, "AWS IoT Core", "(região)", "iot_sem_billing_group_tagueado",
        f"Há things na região, mas nenhum billing group com {TAG_KEY}: o consumo do IoT Core é atribuído pelo "
        "billing group em que a thing está.",
        "Criar um billing group com a tag (a automação tagueia billing groups novos) e associar as things da "
        "solução a ele (AddThingToBillingGroup).",
        f"aws iot create-billing-group --billing-group-name prm --tags Key={TAG_KEY},Value={valor}\n"
        "aws iot add-thing-to-billing-group --billing-group-name prm --thing-name <thing>\n",
        "cli",
        "Associar things a um billing group não afeta o funcionamento delas.",
    )]


_COLETORES = (("eks", _eks), ("ecs", _ecs), ("drs", _drs), ("iot", _iot))


def gerar_recomendacoes(session: boto3.Session, regioes: list[str], valor: str) -> tuple[list[dict], list[dict]]:
    recs: list[dict] = []
    falhas: list[dict] = []
    for regiao in regioes:
        for nome, coletor in _COLETORES:
            try:
                recs.extend(coletor(session, regiao, valor))
            except EndpointConnectionError:
                continue
            except ClientError as exc:
                erro = exc.response.get("Error", {})
                falhas.append({"regiao": regiao, "etapa": f"recomendacoes:{nome}", "erro": f"{erro.get('Code', '')}: {erro.get('Message', '')}"})
            except (BotoCoreError, Exception) as exc:  # noqa: BLE001 — isolamento por coletor
                logger.exception("Falha no coletor de recomendações %s em %s", nome, regiao)
                falhas.append({"regiao": regiao, "etapa": f"recomendacoes:{nome}", "erro": f"{type(exc).__name__}: {exc}"})
    return recs, falhas


def para_markdown(recs: list[dict], conta_id: str, valor: str) -> str:
    linhas = [
        f"# Recomendações de tagueamento na origem — conta {conta_id}",
        "",
        f"Tag: `{TAG_KEY}` = `{valor}`. Cada item abaixo é uma mudança de configuração para aplicar no IaC; "
        "a automação não faz essas alterações.",
        "",
    ]
    if not recs:
        linhas.append("Nenhuma recomendação — a configuração de origem já propaga a tag onde verificado.")
    for i, r in enumerate(recs, 1):
        linhas += [
            f"## {i}. {r['servico']} — {r['alvo']} ({r['regiao']})",
            "",
            f"**Situação:** {r['situacao']}",
            "",
            f"**Recomendação:** {r['recomendacao']}",
            "",
        ]
        if r.get("impacto"):
            linhas += [f"**Impacto:** {r['impacto']}", ""]
        if r.get("artefato"):
            conteudo = r["artefato"]["conteudo"]
            if not isinstance(conteudo, str):
                conteudo = json.dumps(conteudo, indent=2)
            linhas += [f"Formato: {r['artefato']['formato']}", "", "```", conteudo.rstrip(), "```", ""]
    return "\n".join(linhas) + "\n"
