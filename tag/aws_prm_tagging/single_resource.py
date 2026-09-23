"""Leitura do estado atual de UM recurso, a partir do seu ARN — usado pela
Etapa 3 (automação contínua) para montar, no momento em que processa um
evento de criação, o mesmo formato de dict que `resource_discovery.py`
(Etapa 1) produziria para aquele recurso — sem rodar uma descoberta
completa da região/conta, que a Etapa 3 não tem (e não precisa, já que o
evento já entrega o ARN).

`build_resource_from_arn` é o único ponto de entrada — devolve um dict no
schema exato de `resource_discovery._build_resource` (`arn`, `servico`,
`regiao`, `tipo_recurso`, `status_tag`, `valor_tag_encontrado`,
`tag_similar_encontrada`, `tag_similar_chaves`, `iac`), para que
`decision.classify_resource` (Etapa 2a) possa ser chamado sem nenhuma
adaptação.

**Duplicação conhecida com `tag_execution.py`**: as quatro funções de
leitura abaixo (`_read_tags_generic`/`_read_tags_eks`/`_read_tags_bedrock`/
`_read_tags_elb`) fazem essencialmente a mesma chamada de API que
`tag_execution._revalidate_generic`/`_revalidate_eks_or_bedrock`/
`_revalidate_elb` já fazem — só que para um ARN de cada vez, no formato de
saída da Etapa 1, não no formato interno de revalidação da Etapa 2b/2c. Não
extraí um helper compartilhado agora para não mexer em `tag_execution.py`
(código já testado e usado em execução real/`--live`) só para esta
reorganização — registrado como melhoria futura em
`docs/melhorias-futuras.md`.

Sobre a limitação conhecida de `resourcegroupstaggingapi:GetResources` não
devolver recursos SEM NENHUMA tag (ver `resource_discovery.py`): aqui isso
não é um problema — um recurso recém-criado que ainda não tem nenhuma tag
simplesmente não aparece na resposta, e o código abaixo já trata "ARN
ausente da resposta" como `tags={}` (mesma leitura de `status_tag="sem_tag"`
que seria o resultado correto de qualquer forma). A limitação da Etapa 1 é
sobre *descoberta* (encontrar ARNs desconhecidos); aqui o ARN já é
conhecido (veio do evento), então o caso não se aplica.
"""
from __future__ import annotations

import boto3
from botocore.exceptions import ClientError

from .iac_detection import detect_iac
from .retry import with_backoff
from .tag_status import find_similar_tag_keys, get_tag_status, tags_list_to_dict

_SERVICO_EKS = "Amazon EKS"
_SERVICO_BEDROCK = "Amazon Bedrock"

_EKS_TIPOS_RECURSO_DEDICADOS = {"cluster", "node_group"}
_EKS_TIPO_RECURSO_LOAD_BALANCER = "load_balancer"
_BEDROCK_TIPO_RECURSO_APPLICATION_INFERENCE_PROFILE = "application_inference_profile"


@with_backoff()
def _get_resources_single(client, arn: str) -> dict:
    return client.get_resources(ResourceARNList=[arn])


def _read_tags_generic(session: boto3.Session, regiao: str, arn: str) -> dict[str, str]:
    client = session.client("resourcegroupstaggingapi", region_name=regiao)
    resp = _get_resources_single(client, arn)
    mappings = resp.get("ResourceTagMappingList") or []
    if not mappings:
        return {}
    return tags_list_to_dict(mappings[0].get("Tags", []))


@with_backoff()
def _eks_list_tags(client, arn: str) -> dict:
    return client.list_tags_for_resource(resourceArn=arn)


def _read_tags_eks(session: boto3.Session, regiao: str, arn: str) -> dict[str, str]:
    client = session.client("eks", region_name=regiao)
    resp = _eks_list_tags(client, arn)
    return resp.get("tags", {})


@with_backoff()
def _bedrock_list_tags(client, arn: str) -> dict:
    return client.list_tags_for_resource(resourceARN=arn)


def _read_tags_bedrock(session: boto3.Session, regiao: str, arn: str) -> dict[str, str]:
    client = session.client("bedrock", region_name=regiao)
    resp = _bedrock_list_tags(client, arn)
    return tags_list_to_dict(resp.get("tags", []))


@with_backoff()
def _elb_describe_tags(client, arn: str) -> dict:
    return client.describe_tags(ResourceArns=[arn])


def _read_tags_elb(session: boto3.Session, regiao: str, arn: str) -> dict[str, str]:
    client = session.client("elbv2", region_name=regiao)
    resp = _elb_describe_tags(client, arn)
    descriptions = resp.get("TagDescriptions") or []
    if not descriptions:
        return {}
    return tags_list_to_dict(descriptions[0].get("Tags", []))


def _read_tags(
    session: boto3.Session, servico: str, regiao: str, arn: str, tipo_recurso: str | None
) -> dict[str, str]:
    if servico == _SERVICO_EKS and tipo_recurso in _EKS_TIPOS_RECURSO_DEDICADOS:
        return _read_tags_eks(session, regiao, arn)
    if servico == _SERVICO_EKS and tipo_recurso == _EKS_TIPO_RECURSO_LOAD_BALANCER:
        return _read_tags_elb(session, regiao, arn)
    if servico == _SERVICO_BEDROCK and tipo_recurso == _BEDROCK_TIPO_RECURSO_APPLICATION_INFERENCE_PROFILE:
        return _read_tags_bedrock(session, regiao, arn)
    # Genérico — cobre a maioria dos serviços, incluindo nodes/volumes EBS
    # de EKS (que usam o caminho genérico de propósito, mesma razão já
    # documentada em tag_execution.py).
    return _read_tags_generic(session, regiao, arn)


def build_resource_from_arn(
    session: boto3.Session,
    arn: str,
    servico: str,
    regiao: str,
    expected_tag_value: str,
    tipo_recurso: str | None = None,
) -> dict:
    """Lê o estado atual de tags de um único ARN e monta o dict no formato
    da Etapa 1 — ponto de entrada usado pelo handler da Etapa 3 antes de
    chamar `decision.classify_resource`.

    Propaga `ClientError` para o chamador (ex.: `AccessDenied`, recurso
    ainda não encontrado logo após a criação) — decidir se tenta de novo ou
    reporta como falha é responsabilidade de quem chama (ver
    `handler_continuous_tagging.py`), não deste módulo."""
    tags = _read_tags(session, servico, regiao, arn, tipo_recurso)
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
