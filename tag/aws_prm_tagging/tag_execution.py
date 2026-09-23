"""Execução do tagueamento (Etapa 2b: dry-run; Etapa 2c: escrita real) a
partir do relatório de decisão da Etapa 2a (`decision.build_decision_report`).

Esta Etapa 2b em si nunca escreve nada na conta AWS — só chamadas de
leitura (revalidação, ver abaixo). O módulo é escrito para ser reaproveitado
tal-qual pela Etapa 2c (ainda não implementada), trocando apenas o
`Executor` usado pelo orquestrador: hoje só `DryRunExecutor` existe; a
Etapa 2c adiciona um `LiveExecutor` que implementa o mesmo `Protocol`
chamando boto3 de verdade. Toda a lógica de roteamento de API, agrupamento
em lotes e revalidação é comum às duas etapas e não é duplicada.

## Garantia estrutural de que só `decisao == "taguear"` gera uma chamada

`select_taggable()` é o único ponto de entrada aceito pelo resto do módulo.
Ela converte cada recurso do relatório de decisão em um `TaggableResource`
— um tipo que **não tem campo `decisao`**. Todas as funções abaixo (
`route_strategy`, `group_into_batches`, os métodos de `Executor`) recebem
`TaggableResource`, nunca o dict bruto do relatório 2a. Não existe caminho
de código que aceite `pular_iac`/`ja_ok`/`conflito` como parâmetro — é um
erro de tipo, não uma condição que alguém possa esquecer de checar.

## Mapeamento de API por tipo de recurso

Confirmado na documentação oficial da AWS (API references, não a página-
índice de "supported services", que é renderizada via JS):

- Caminho genérico (`tag:TagResources`, Resource Groups Tagging API): cobre
  a maioria dos serviços do CSV, incluindo os nodes EC2 e volumes EBS
  descobertos como sub-recursos de EKS (são instâncias/volumes comuns por
  baixo do capô — mesmo caminho de escrita que qualquer EC2 genérico).
  Aceita até 20 ARNs por chamada (`ResourceARNList`, limite documentado —
  não 50, como uma fonte não oficial sugeriu numa busca inicial). Resposta
  traz `FailedResourcesMap` para falha parcial dentro de um lote "bem
  sucedido" no nível HTTP.
- `eks:TagResource` — cluster e node group do EKS. API dedicada porque a
  Resource Groups Tagging API não cobre esses dois tipos (mesmo motivo pelo
  qual `resource_discovery.py` já usa um client `eks` dedicado para
  *ler* esses recursos, em vez do passo genérico). `resourceArn` é
  singular — 1 recurso por chamada, sem batch.
- `bedrock:TagResource` — application inference profiles. Mesma razão e
  mesma limitação de 1 recurso por chamada que EKS.
- `elasticloadbalancing:AddTags` — load balancers associados a um cluster
  EKS. O parâmetro `ResourceArns` é formalmente um array, mas não há
  confirmação oficial de que múltiplos ARNs num único `AddTags` funcionem
  (há relatos de rejeição). Decisão conservadora: tratado como 1 ARN por
  chamada, igual EKS/Bedrock — otimizar para lote fica para validação
  empírica em sandbox na Etapa 2c, não é suposição usada aqui.

## Revalidação e idempotência

Toda execução (dry-run ou, futuramente, live) revalida o estado atual da
tag imediatamente antes de agir sobre cada recurso — controlável via
`revalidate=False` em `run_stage2b`, default `True`. Dois motivos:

1. Condição de corrida: o recurso pode ter sido deletado, ou tido a tag
   alterada por outra automação, entre a execução da Etapa 1/2a e a
   execução desta etapa.
2. Idempotência entre execuções: se a Etapa 2c já tiver tagueado um
   recurso numa execução anterior (ou outra automação já tiver aplicado a
   tag), rodar de novo sobre o mesmo relatório de decisão não deve gerar
   uma nova chamada de escrita para esse recurso — ele é reportado como
   `"ja_tagueado"` e pulado. Isso é o que torna a Etapa 2c idempotente por
   construção: o estado atual da tag na AWS *é* a fonte da verdade de
   "já foi feito ou não", sem precisar de nenhum arquivo de progresso
   separado. Combinado com o fato de que todas as APIs de tagging usadas
   aqui são operações de conjunto (aplicar o mesmo valor duas vezes é
   no-op, não erro), reexecuções repetidas do mesmo relatório de decisão
   nunca duplicam nem conflitam uma ação já feita.

A leitura de revalidação usa, por estratégia:

- Genérico: uma varredura por região via `tag:GetResources` com
  `TagFilters=[{"Key": "aws-apn-id"}]`, cobrindo TODOS os recursos
  genéricos da região revalidados de uma vez (muito mais barato que 1
  leitura por ARN). Um ARN que não aparece no resultado significa "sem a
  tag aws-apn-id hoje" — o que inclui tanto "nunca teve a tag" quanto (raro)
  "recurso foi deletado"; qualquer um dos dois casos é seguro seguir para a
  tentativa de tagueamento (se deletado, a chamada real na Etapa 2c falha
  de forma limpa e é reportada, sem dano).
- EKS (cluster/node group): `eks:ListTagsForResource`, 1 ARN por chamada
  (mesma limitação da escrita).
- Bedrock: `bedrock:ListTagsForResource`, 1 ARN por chamada — já usado do
  mesmo jeito em `resource_discovery.discover_bedrock_resources`.
- ELB: `elasticloadbalancing:DescribeTags`, até 20 ARNs por chamada (só a
  leitura aceita lote; a escrita não — ver acima). Mesmo client já usado em
  `resource_discovery.discover_eks_resources`.

Se a leitura de revalidação falhar (ex.: `AccessDenied` na permissão de
leitura), o recurso não é bloqueado: fica registrado que a revalidação
falhou, e a tentativa de tagueamento (real ou simulada) prossegue
normalmente — falhar a leitura de revalidação não deveria impedir de
tentar a ação principal.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

import boto3
from botocore.exceptions import ClientError

from .decision import DECISAO_TAGUEAR
from .retry import with_backoff
from .tag_status import TAG_KEY, tags_list_to_dict

logger = logging.getLogger(__name__)

_TAMANHO_MAX_LOTE_GENERICO = 20  # limite documentado de tag:TagResources


class ApiStrategy(str, Enum):
    """Qual API de escrita/leitura usar para um `TaggableResource` — ver
    docstring do módulo para a justificativa de cada uma."""

    GENERICO = "generico_tag_resources"
    EKS_CLUSTER = "eks_tag_resource_cluster"
    EKS_NODE_GROUP = "eks_tag_resource_node_group"
    BEDROCK_PROFILE = "bedrock_tag_resource"
    ELB_LOAD_BALANCER = "elb_add_tags"


# Estratégias que fazem 1 recurso por chamada de escrita (sem batch).
ESTRATEGIAS_SEM_BATCH_DE_ESCRITA = frozenset(
    {
        ApiStrategy.EKS_CLUSTER,
        ApiStrategy.EKS_NODE_GROUP,
        ApiStrategy.BEDROCK_PROFILE,
        ApiStrategy.ELB_LOAD_BALANCER,
    }
)

_SERVICO_EKS = "Amazon EKS"
_SERVICO_BEDROCK = "Amazon Bedrock"

# (servico, tipo_recurso) -> estratégia dedicada. Qualquer combinação fora
# desta tabela (inclui tipo_recurso=None, o caso comum) usa o caminho
# genérico — inclui de propósito os nodes/volumes EBS do EKS, que não
# entram aqui (ver docstring do módulo).
_ESTRATEGIA_DEDICADA: dict[tuple[str, str], ApiStrategy] = {
    (_SERVICO_EKS, "cluster"): ApiStrategy.EKS_CLUSTER,
    (_SERVICO_EKS, "node_group"): ApiStrategy.EKS_NODE_GROUP,
    (_SERVICO_EKS, "load_balancer"): ApiStrategy.ELB_LOAD_BALANCER,
    (_SERVICO_BEDROCK, "application_inference_profile"): ApiStrategy.BEDROCK_PROFILE,
}

RESULTADO_SIMULADO_OK = "simulado_ok"
RESULTADO_JA_TAGUEADO = "ja_tagueado"
RESULTADO_ERRO_PERMISSAO = "erro_permissao"
RESULTADO_ERRO = "erro"


@dataclass(frozen=True)
class TaggableResource:
    """Recurso elegível para tagueamento — só existe para recursos com
    `decisao == "taguear"` no relatório da Etapa 2a. Deliberadamente sem o
    campo `decisao`: ver docstring do módulo."""

    arn: str
    servico: str
    regiao: str
    tipo_recurso: str | None


def select_taggable(decision_report: dict) -> list[TaggableResource]:
    """Único ponto de entrada aceito pelo resto deste módulo. Filtra
    `decision_report["recursos"]` (saída de `decision.build_decision_report`)
    para os recursos com `decisao == "taguear"` e descarta explicitamente o
    campo `decisao` na conversão para `TaggableResource`."""
    resources = decision_report.get("recursos") or []
    return [
        TaggableResource(
            arn=r["arn"],
            servico=r["servico"],
            regiao=r["regiao"],
            tipo_recurso=r.get("tipo_recurso"),
        )
        for r in resources
        if r.get("decisao") == DECISAO_TAGUEAR
    ]


def route_strategy(resource: TaggableResource) -> ApiStrategy:
    """Decide qual API usar para tagueamento deste recurso."""
    if resource.tipo_recurso is not None:
        estrategia = _ESTRATEGIA_DEDICADA.get((resource.servico, resource.tipo_recurso))
        if estrategia is not None:
            return estrategia
    return ApiStrategy.GENERICO


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# ---------------------------------------------------------------------------
# Revalidação — leitura do estado atual da tag imediatamente antes de agir
# ---------------------------------------------------------------------------


@with_backoff()
def _get_resources_by_tag_key_page(client, tag_key: str, pagination_token: str | None) -> dict:
    kwargs: dict = {"TagFilters": [{"Key": tag_key}], "ResourcesPerPage": 100}
    if pagination_token:
        kwargs["PaginationToken"] = pagination_token
    return client.get_resources(**kwargs)


def _revalidate_generic(
    session: boto3.Session, regiao: str, arns: set[str], tag_key: str
) -> dict[str, str | None]:
    """Uma varredura via `tag:GetResources` cobre todos os ARNs genéricos
    revalidados nesta região de uma vez. Devolve `{arn: valor_atual}` —
    `None` para um ARN não encontrado na varredura (sem a tag hoje, ou
    deletado; ver docstring do módulo)."""
    client = session.client("resourcegroupstaggingapi", region_name=regiao)
    encontrados: dict[str, str] = {}
    token = None
    try:
        while True:
            resp = _get_resources_by_tag_key_page(client, tag_key, token)
            for mapping in resp.get("ResourceTagMappingList", []):
                arn = mapping["ResourceARN"]
                if arn not in arns:
                    continue
                tags = tags_list_to_dict(mapping.get("Tags", []))
                if tag_key in tags:
                    encontrados[arn] = tags[tag_key]
            token = resp.get("PaginationToken")
            if not token:
                break
    except ClientError:
        logger.exception(
            "Falha ao revalidar tags genéricas em %s — prosseguindo sem revalidação "
            "para os %d recursos desta região",
            regiao,
            len(arns),
        )
        return {}
    return {arn: encontrados.get(arn) for arn in arns}


@with_backoff()
def _eks_list_tags(client, resource_arn: str) -> dict:
    return client.list_tags_for_resource(resourceArn=resource_arn)


@with_backoff()
def _bedrock_list_tags(client, resource_arn: str) -> dict:
    return client.list_tags_for_resource(resourceARN=resource_arn)


@with_backoff()
def _elb_describe_tags(client, arns: list[str]) -> dict:
    return client.describe_tags(ResourceArns=arns)


def _revalidate_eks_or_bedrock(
    session: boto3.Session, service_name: str, regiao: str, resource: TaggableResource, tag_key: str
) -> str | None:
    client = session.client(service_name, region_name=regiao)
    try:
        if service_name == "eks":
            resp = _eks_list_tags(client, resource.arn)
            tags = resp.get("tags", {})
        else:
            resp = _bedrock_list_tags(client, resource.arn)
            tags = tags_list_to_dict(resp.get("tags", []))
        return tags.get(tag_key)
    except ClientError:
        logger.exception("Falha ao revalidar tags de %s (%s) — prosseguindo sem revalidação", resource.arn, service_name)
        return None


def _revalidate_elb(
    session: boto3.Session, regiao: str, resources: list[TaggableResource], tag_key: str
) -> dict[str, str | None]:
    client = session.client("elbv2", region_name=regiao)
    resultado: dict[str, str | None] = {r.arn: None for r in resources}
    for lote in _chunk([r.arn for r in resources], 20):  # describe_tags aceita até 20 ARNs
        try:
            resp = _elb_describe_tags(client, lote)
        except ClientError:
            logger.exception("Falha ao revalidar tags de load balancers %s — prosseguindo sem revalidação", lote)
            continue
        for desc in resp.get("TagDescriptions", []):
            tags = tags_list_to_dict(desc.get("Tags", []))
            resultado[desc["ResourceArn"]] = tags.get(tag_key)
    return resultado


# ---------------------------------------------------------------------------
# Executor — único ponto de decisão entre "logar o que seria feito" (Etapa
# 2b, `DryRunExecutor`) e "fazer de verdade" (Etapa 2c, ainda não
# implementada). Todo o roteamento/agrupamento acima é comum às duas.
# ---------------------------------------------------------------------------


class Executor(Protocol):
    def tag_generic_batch(
        self, session: boto3.Session, regiao: str, arns: list[str], tag_key: str, tag_value: str
    ) -> dict[str, str]:
        """Devolve `{arn: resultado}` para cada ARN do lote."""
        ...

    def tag_single(
        self,
        session: boto3.Session,
        estrategia: ApiStrategy,
        resource: TaggableResource,
        tag_key: str,
        tag_value: str,
    ) -> str:
        """Devolve o resultado da tentativa para este único recurso."""
        ...


class DryRunExecutor:
    """Etapa 2b: nunca chama boto3 de escrita — só loga, no mesmo formato
    estruturado que vira o relatório de saída (preview fiel do que a Etapa
    2c executaria com os mesmos dados)."""

    def tag_generic_batch(
        self, session: boto3.Session, regiao: str, arns: list[str], tag_key: str, tag_value: str
    ) -> dict[str, str]:
        logger.info(
            "[DRY-RUN] tag:TagResources em %s aplicaria {%s: %s} a %d recurso(s): %s",
            regiao,
            tag_key,
            tag_value,
            len(arns),
            arns,
        )
        return {arn: RESULTADO_SIMULADO_OK for arn in arns}

    def tag_single(
        self,
        session: boto3.Session,
        estrategia: ApiStrategy,
        resource: TaggableResource,
        tag_key: str,
        tag_value: str,
    ) -> str:
        acao_nativa = {
            ApiStrategy.EKS_CLUSTER: "eks:TagResource",
            ApiStrategy.EKS_NODE_GROUP: "eks:TagResource",
            ApiStrategy.BEDROCK_PROFILE: "bedrock:TagResource",
            ApiStrategy.ELB_LOAD_BALANCER: "elasticloadbalancing:AddTags",
        }[estrategia]
        logger.info(
            "[DRY-RUN] %s em %s (%s) aplicaria {%s: %s} a %s",
            acao_nativa,
            resource.regiao,
            estrategia.value,
            tag_key,
            tag_value,
            resource.arn,
        )
        return RESULTADO_SIMULADO_OK


# ---------------------------------------------------------------------------
# Orquestrador
# ---------------------------------------------------------------------------


def _resultado_entry(resource: TaggableResource, estrategia: ApiStrategy, resultado: str) -> dict:
    return {
        "arn": resource.arn,
        "servico": resource.servico,
        "regiao": resource.regiao,
        "tipo_recurso": resource.tipo_recurso,
        "estrategia_api": estrategia.value,
        "resultado": resultado,
    }


def _processar_genericos(
    session: boto3.Session,
    executor: Executor,
    recursos: list[TaggableResource],
    tag_key: str,
    tag_value: str,
    revalidate: bool,
) -> list[dict]:
    resultados: list[dict] = []
    por_regiao: dict[str, list[TaggableResource]] = {}
    for r in recursos:
        por_regiao.setdefault(r.regiao, []).append(r)

    for regiao, recursos_regiao in por_regiao.items():
        estado_atual = (
            _revalidate_generic(session, regiao, {r.arn for r in recursos_regiao}, tag_key)
            if revalidate
            else {}
        )
        pendentes = [r for r in recursos_regiao if estado_atual.get(r.arn) != tag_value]
        for r in recursos_regiao:
            if estado_atual.get(r.arn) == tag_value:
                resultados.append(_resultado_entry(r, ApiStrategy.GENERICO, RESULTADO_JA_TAGUEADO))

        for lote in _chunk(pendentes, _TAMANHO_MAX_LOTE_GENERICO):
            arns = [r.arn for r in lote]
            resultado_por_arn = executor.tag_generic_batch(session, regiao, arns, tag_key, tag_value)
            for r in lote:
                resultados.append(
                    _resultado_entry(r, ApiStrategy.GENERICO, resultado_por_arn.get(r.arn, RESULTADO_ERRO))
                )
    return resultados


def _revalidate_dedicado(
    session: boto3.Session, estrategia: ApiStrategy, resources: list[TaggableResource], tag_key: str
) -> dict[str, str | None]:
    if estrategia in (ApiStrategy.EKS_CLUSTER, ApiStrategy.EKS_NODE_GROUP):
        return {
            r.arn: _revalidate_eks_or_bedrock(session, "eks", r.regiao, r, tag_key) for r in resources
        }
    if estrategia is ApiStrategy.BEDROCK_PROFILE:
        return {
            r.arn: _revalidate_eks_or_bedrock(session, "bedrock", r.regiao, r, tag_key)
            for r in resources
        }
    # ELB_LOAD_BALANCER — agrupa a LEITURA por região em lotes de 20 (a
    # escrita continua 1-a-1; só describe_tags aceita lote).
    resultado: dict[str, str | None] = {}
    por_regiao: dict[str, list[TaggableResource]] = {}
    for r in resources:
        por_regiao.setdefault(r.regiao, []).append(r)
    for regiao, recursos_regiao in por_regiao.items():
        resultado.update(_revalidate_elb(session, regiao, recursos_regiao, tag_key))
    return resultado


def _processar_dedicados(
    session: boto3.Session,
    executor: Executor,
    recursos_por_estrategia: dict[ApiStrategy, list[TaggableResource]],
    tag_key: str,
    tag_value: str,
    revalidate: bool,
) -> list[dict]:
    resultados: list[dict] = []
    for estrategia, recursos in recursos_por_estrategia.items():
        estado_atual = (
            _revalidate_dedicado(session, estrategia, recursos, tag_key) if revalidate else {}
        )
        for r in recursos:
            if estado_atual.get(r.arn) == tag_value:
                resultados.append(_resultado_entry(r, estrategia, RESULTADO_JA_TAGUEADO))
                continue
            resultado = executor.tag_single(session, estrategia, r, tag_key, tag_value)
            resultados.append(_resultado_entry(r, estrategia, resultado))
    return resultados


def build_stage2b_report(account_id: str | None, expected_tag_value: str, resultados: list[dict]) -> dict:
    """Monta o relatório de saída no mesmo estilo de `report.build_report` /
    `decision.build_decision_report` — mesma base para a Etapa 2c e para o
    dashboard."""
    resultado_counts = Counter(r["resultado"] for r in resultados)
    estrategia_counts = Counter(r["estrategia_api"] for r in resultados)
    return {
        "conta_id": account_id,
        "executado_em": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "valor_tag_esperado": expected_tag_value,
        "modo": "dry_run",
        "resumo": {
            "total_recursos_processados": len(resultados),
            "por_resultado": dict(sorted(resultado_counts.items())),
            "por_estrategia_api": dict(sorted(estrategia_counts.items())),
        },
        "recursos": resultados,
    }


def run_stage2b(
    decision_report: dict,
    session: boto3.Session,
    expected_tag_value: str,
    revalidate: bool = True,
    executor: Executor | None = None,
) -> dict:
    """Ponto de entrada da Etapa 2b. `executor` é injetável para teste (e
    será o ponto de troca para `LiveExecutor` na Etapa 2c) — default
    `DryRunExecutor()`, a única implementação que existe até a Etapa 2c."""
    executor = executor or DryRunExecutor()
    taggable = select_taggable(decision_report)

    genericos = [r for r in taggable if route_strategy(r) is ApiStrategy.GENERICO]
    dedicados_por_estrategia: dict[ApiStrategy, list[TaggableResource]] = {}
    for r in taggable:
        estrategia = route_strategy(r)
        if estrategia is not ApiStrategy.GENERICO:
            dedicados_por_estrategia.setdefault(estrategia, []).append(r)

    resultados = _processar_genericos(
        session, executor, genericos, TAG_KEY, expected_tag_value, revalidate
    )
    resultados += _processar_dedicados(
        session, executor, dedicados_por_estrategia, TAG_KEY, expected_tag_value, revalidate
    )

    return build_stage2b_report(decision_report.get("conta_id"), expected_tag_value, resultados)
