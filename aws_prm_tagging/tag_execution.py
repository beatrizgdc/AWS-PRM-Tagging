"""Execução do tagueamento: Etapa 2b (dry-run) e Etapa 2c (escrita real).

`run_tagging_execution` atende as duas etapas; só o `Executor` muda
(`DryRunExecutor` loga, `LiveExecutor` chama a AWS). Roteamento de API,
lotes, revalidação, classificação de erro e relatório são o mesmo código.

Garantia estrutural: `select_taggable` converte cada recurso que tagueia
(`taguear`, `taguear_com_tag_similar`, `sobrescrever`) num
`TaggableResource`, que não tem campo `decisao`. Nada além disso chega aos
executores.

APIs de escrita por tipo de recurso:

- genérico: `tag:TagResources`, até 20 ARNs por chamada; falha parcial vem
  em `FailedResourcesMap`;
- cluster e node group do EKS: `eks:TagResource` (a API de tags não cobre);
- application inference profile do Bedrock: `bedrock:TagResource`;
- load balancer de EKS: `elasticloadbalancing:AddTags`, 1 ARN por chamada.

Revalidação: antes de agir, o estado atual de cada recurso é relido (todas
as tags, não só a `aws-apn-id`) e passa pela mesma regra de
`decision.py`:

1. valor já correto -> `ja_tagueado` (sem escrita; torna reexecuções
   idempotentes);
2. IaC detectado -> `iac_detectado_na_revalidacao` (sem escrita);
3. senão, segue para a escrita; o estado lido vira `estado_anterior`.

Se a leitura de revalidação falhar, o recurso nunca é escrito
(`revalidacao_falhou`, ou `recurso_nao_encontrado_na_revalidacao` quando o
recurso sumiu). Por isso o modo live exige revalidação
(`RevalidacaoObrigatoriaError`).

Erros de escrita: throttling é absorvido por `retry.with_backoff`; o resto
vira `erro_permissao`, `recurso_nao_encontrado` ou `erro` (código original
em `detalhe_erro`). Uma falha nunca interrompe os demais lotes.

O relatório final (`build_execution_report`) junta o resultado dos que
tagueiam, os `pular_iac`/`ja_ok` decididos na Etapa 2a e os erros de
classificação, com `categoria_final` e `origem` (`decisao`, `revalidacao`
ou `execucao`).
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

import boto3
from botocore.exceptions import ClientError

from .decision import (
    DECISAO_JA_OK,
    DECISAO_PULAR_IAC,
    DECISOES_LEGADAS,
    DECISOES_QUE_TAGUEIAM,
)
from .iac_detection import IAC_DESCONHECIDO, IAC_DETECTADO, detect_iac
from .retry import with_backoff
from .tag_reads import bedrock_list_tags, eks_list_tags, elb_describe_tags
from .tag_status import TAG_KEY, find_similar_tag_keys, tags_list_to_dict

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


_SERVICO_EKS = "Amazon EKS"
_SERVICO_BEDROCK = "Amazon Bedrock"

# IaC bloqueia a escrita com a tag ausente ou com outro valor (como em decision.py).
_IAC_DETECTADO = IAC_DETECTADO

# (servico, tipo_recurso) -> API dedicada. O resto, inclusive nodes e volumes
# do EKS (são EC2/EBS comuns), usa o caminho genérico.
_ESTRATEGIA_DEDICADA: dict[tuple[str, str], ApiStrategy] = {
    (_SERVICO_EKS, "cluster"): ApiStrategy.EKS_CLUSTER,
    (_SERVICO_EKS, "node_group"): ApiStrategy.EKS_NODE_GROUP,
    (_SERVICO_EKS, "load_balancer"): ApiStrategy.ELB_LOAD_BALANCER,
    (_SERVICO_BEDROCK, "application_inference_profile"): ApiStrategy.BEDROCK_PROFILE,
}

RESULTADO_SIMULADO_OK = "simulado_ok"
RESULTADO_TAGUEADO_SUCESSO = "tagueado_sucesso"
RESULTADO_JA_TAGUEADO = "ja_tagueado"
RESULTADO_IAC_DETECTADO_NA_REVALIDACAO = "iac_detectado_na_revalidacao"
RESULTADO_RECURSO_NAO_ENCONTRADO_NA_REVALIDACAO = "recurso_nao_encontrado_na_revalidacao"
RESULTADO_REVALIDACAO_FALHOU = "revalidacao_falhou"
RESULTADO_ERRO_PERMISSAO = "erro_permissao"
RESULTADO_RECURSO_NAO_ENCONTRADO = "recurso_nao_encontrado"
RESULTADO_ERRO = "erro"

# Categoria final, para leitura humana/dashboard. Simulado (dry-run) e
# tagueado (live) são categorias diferentes para nunca se somarem.
CATEGORIA_TAGUEADO_SUCESSO = "tagueado_sucesso"
CATEGORIA_SIMULADO_SUCESSO = "simulado_sucesso"
CATEGORIA_FALHOU = "falhou"
CATEGORIA_PULADO_IAC = "pulado_iac"
CATEGORIA_JA_OK = "ja_ok"
CATEGORIA_ERRO_CLASSIFICACAO = "erro_classificacao"
_TODAS_AS_CATEGORIAS_FINAIS = (
    CATEGORIA_TAGUEADO_SUCESSO,
    CATEGORIA_SIMULADO_SUCESSO,
    CATEGORIA_FALHOU,
    CATEGORIA_PULADO_IAC,
    CATEGORIA_JA_OK,
    CATEGORIA_ERRO_CLASSIFICACAO,
)

# Códigos com contagem própria: permissão pede ajuste de IAM; "não
# encontrado" é relatório desatualizado. O resto vira RESULTADO_ERRO.
_CODIGOS_ERRO_PERMISSAO = frozenset({"AccessDenied", "AccessDeniedException", "UnauthorizedException"})
_CODIGOS_RECURSO_NAO_ENCONTRADO = frozenset(
    {
        "ResourceNotFoundException",
        "NoSuchEntity",
        "ClusterNotFoundException",
        "NodegroupNotFoundException",
        # ELBv2 (nome do shape == código do erro).
        "LoadBalancerNotFoundException",
        "TargetGroupNotFoundException",
        "ListenerNotFoundException",
        "RuleNotFoundException",
        "TrustStoreNotFoundException",
    }
)


@dataclass(frozen=True)
class ResourceOutcome:
    """Resultado de uma tentativa sobre um recurso. `detalhe_erro` só em falha."""

    resultado: str
    detalhe_erro: dict | None = None


def _classificar_erro_aws(codigo: str, mensagem: str) -> ResourceOutcome:
    if codigo in _CODIGOS_ERRO_PERMISSAO:
        resultado = RESULTADO_ERRO_PERMISSAO
    elif codigo in _CODIGOS_RECURSO_NAO_ENCONTRADO:
        resultado = RESULTADO_RECURSO_NAO_ENCONTRADO
    else:
        resultado = RESULTADO_ERRO
    return ResourceOutcome(resultado=resultado, detalhe_erro={"codigo": codigo, "mensagem": mensagem})


@dataclass(frozen=True)
class _RevalidatedState:
    """Estado atual do recurso, lido com sucesso logo antes da escrita."""

    valor_atual: str | None
    iac_tipo: str
    tag_similar_encontrada: bool
    # "Antes" da escrita: vira `estado_anterior` no relatório e no SNS.
    tags_similares: tuple[tuple[str, str | None], ...] = ()
    iac: dict | None = field(default=None, compare=False, hash=False)

    def estado_anterior(self) -> dict | None:
        if self.valor_atual is None and not self.tags_similares:
            return None
        return {TAG_KEY: self.valor_atual, "chaves_similares": dict(self.tags_similares)}


_ESTADO_SEM_TAGS = _RevalidatedState(valor_atual=None, iac_tipo=IAC_DESCONHECIDO, tag_similar_encontrada=False)


def _revalidated_state_from_tags(tags: dict[str, str], tag_key: str) -> _RevalidatedState:
    similares = find_similar_tag_keys(tags, tag_key)
    iac = detect_iac(tags)
    return _RevalidatedState(
        valor_atual=tags.get(tag_key),
        iac_tipo=iac["tipo"],
        tag_similar_encontrada=bool(similares),
        tags_similares=tuple((k, tags.get(k)) for k in similares),
        iac=iac,
    )


@dataclass(frozen=True)
class _RevalidationFailure:
    """A leitura de revalidação falhou: o recurso não é escrito em nenhum
    modo, porque o estado atual é desconhecido."""

    detalhe_erro: dict | None = None


@dataclass(frozen=True)
class TaggableResource:
    """Recurso a taguear. Sem campo `decisao` de propósito (ver docstring do módulo)."""

    arn: str
    servico: str
    regiao: str
    tipo_recurso: str | None
    # Estado visto na Etapa 2a; usado só quando não há revalidação.
    estado_anterior: dict | None = field(default=None, compare=False, hash=False)


def select_taggable(decision_report: dict) -> list[TaggableResource]:
    """Converte os recursos que tagueiam em `TaggableResource`, descartando a `decisao`."""
    resources = decision_report.get("recursos") or []
    return [
        TaggableResource(
            arn=r["arn"],
            servico=r["servico"],
            regiao=r["regiao"],
            tipo_recurso=r.get("tipo_recurso"),
            estado_anterior=r.get("estado_anterior"),
        )
        for r in resources
        if r.get("decisao") in DECISOES_QUE_TAGUEIAM
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


_TAMANHO_MAX_LOTE_GETRESOURCES = 20  # limite documentado de ResourceARNList, mesmo de TagResources


@with_backoff()
def _get_resources_batch(client, arns: list[str]) -> dict:
    return client.get_resources(ResourceARNList=arns)


def _revalidate_generic(
    session: boto3.Session, regiao: str, arns: set[str], tag_key: str
) -> dict[str, _RevalidatedState | _RevalidationFailure]:
    """Lê as tags completas dos ARNs em lotes de 20 (`ResourceARNList`).

    Sem `TagFilters`: a revalidação precisa de todas as tags para checar IaC.
    Lote com erro vira `_RevalidationFailure` só para os ARNs daquele lote."""
    client = session.client("resourcegroupstaggingapi", region_name=regiao)
    resultado: dict[str, _RevalidatedState | _RevalidationFailure] = {}
    for lote in _chunk(sorted(arns), _TAMANHO_MAX_LOTE_GETRESOURCES):
        try:
            resp = _get_resources_batch(client, lote)
        except ClientError as exc:
            erro = exc.response.get("Error", {})
            detalhe_erro = {"codigo": erro.get("Code", ""), "mensagem": erro.get("Message", "")}
            logger.exception(
                "Falha ao revalidar tags genéricas em %s (lote de %d ARN(s)) — marcados "
                "como revalidação falha (não prosseguem para tentativa de escrita)",
                regiao,
                len(lote),
            )
            for arn in lote:
                resultado[arn] = _RevalidationFailure(detalhe_erro=detalhe_erro)
            continue

        encontrados = set()
        for mapping in resp.get("ResourceTagMappingList", []):
            arn = mapping["ResourceARN"]
            tags = tags_list_to_dict(mapping.get("Tags", []))
            resultado[arn] = _revalidated_state_from_tags(tags, tag_key)
            encontrados.add(arn)
        for arn in lote:
            if arn not in encontrados:
                # GetResources não devolve recurso sem nenhuma tag: "sem tag".
                resultado[arn] = _ESTADO_SEM_TAGS
    return resultado


def _revalidate_eks_or_bedrock(
    session: boto3.Session, service_name: str, regiao: str, resource: TaggableResource, tag_key: str
) -> _RevalidatedState | _RevalidationFailure:
    client = session.client(service_name, region_name=regiao)
    try:
        if service_name == "eks":
            resp = eks_list_tags(client, resource.arn)
            tags = resp.get("tags", {})
        else:
            resp = bedrock_list_tags(client, resource.arn)
            tags = tags_list_to_dict(resp.get("tags", []))
        return _revalidated_state_from_tags(tags, tag_key)
    except ClientError as exc:
        erro = exc.response.get("Error", {})
        logger.exception(
            "Falha ao revalidar tags de %s (%s) — recurso marcado como revalidação "
            "falha, não prossegue para tentativa de escrita",
            resource.arn,
            service_name,
        )
        return _RevalidationFailure(detalhe_erro={"codigo": erro.get("Code", ""), "mensagem": erro.get("Message", "")})




def _revalidate_elb(
    session: boto3.Session, regiao: str, resources: list[TaggableResource], tag_key: str
) -> dict[str, _RevalidatedState | _RevalidationFailure]:
    client = session.client("elbv2", region_name=regiao)
    resultado: dict[str, _RevalidatedState | _RevalidationFailure] = {
        r.arn: _ESTADO_SEM_TAGS
        for r in resources
    }
    for lote in _chunk([r.arn for r in resources], 20):  # describe_tags aceita até 20 ARNs
        try:
            resp = elb_describe_tags(client, lote)
        except ClientError:
            # Um ARN inexistente derruba o DescribeTags do lote inteiro:
            # repete 1 a 1 para não bloquear os demais.
            logger.warning(
                "Falha ao revalidar lote de load balancers %s — tentando 1 "
                "ARN por vez para não bloquear os que ainda existem",
                lote,
            )
            for arn in lote:
                try:
                    resp_individual = elb_describe_tags(client, [arn])
                except ClientError as exc_individual:
                    erro = exc_individual.response.get("Error", {})
                    logger.exception(
                        "Falha ao revalidar tags do load balancer %s — marcado "
                        "como revalidação falha, não prossegue para tentativa "
                        "de escrita",
                        arn,
                    )
                    resultado[arn] = _RevalidationFailure(
                        detalhe_erro={"codigo": erro.get("Code", ""), "mensagem": erro.get("Message", "")}
                    )
                    continue
                for desc in resp_individual.get("TagDescriptions", []):
                    tags = tags_list_to_dict(desc.get("Tags", []))
                    resultado[desc["ResourceArn"]] = _revalidated_state_from_tags(tags, tag_key)
            continue
        for desc in resp.get("TagDescriptions", []):
            tags = tags_list_to_dict(desc.get("Tags", []))
            resultado[desc["ResourceArn"]] = _revalidated_state_from_tags(tags, tag_key)
    return resultado


# ---------------------------------------------------------------------------
# Executores: dry-run (loga) e live (escreve)
# ---------------------------------------------------------------------------


class Executor(Protocol):
    def tag_generic_batch(
        self, session: boto3.Session, regiao: str, arns: list[str], tag_key: str, tag_value: str
    ) -> dict[str, ResourceOutcome]:
        """Devolve `{arn: ResourceOutcome}` para cada ARN do lote."""
        ...

    def tag_single(
        self,
        session: boto3.Session,
        estrategia: ApiStrategy,
        resource: TaggableResource,
        tag_key: str,
        tag_value: str,
    ) -> ResourceOutcome:
        """Devolve o resultado da tentativa para este único recurso."""
        ...


class DryRunExecutor:
    """Etapa 2b: só loga o que seria feito e devolve sucesso simulado."""

    def tag_generic_batch(
        self, session: boto3.Session, regiao: str, arns: list[str], tag_key: str, tag_value: str
    ) -> dict[str, ResourceOutcome]:
        logger.info(
            "[DRY-RUN] tag:TagResources em %s aplicaria {%s: %s} a %d recurso(s): %s",
            regiao,
            tag_key,
            tag_value,
            len(arns),
            arns,
        )
        return {arn: ResourceOutcome(resultado=RESULTADO_SIMULADO_OK) for arn in arns}

    def tag_single(
        self,
        session: boto3.Session,
        estrategia: ApiStrategy,
        resource: TaggableResource,
        tag_key: str,
        tag_value: str,
    ) -> ResourceOutcome:
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
        return ResourceOutcome(resultado=RESULTADO_SIMULADO_OK)


@with_backoff()
def _tag_resources_call(client, arns: list[str], tags: dict[str, str]) -> dict:
    return client.tag_resources(ResourceARNList=arns, Tags=tags)


@with_backoff()
def _eks_tag_resource_call(client, arn: str, tags: dict[str, str]) -> None:
    client.tag_resource(resourceArn=arn, tags=tags)


@with_backoff()
def _bedrock_tag_resource_call(client, arn: str, tags: list[dict]) -> None:
    client.tag_resource(resourceARN=arn, tags=tags)


@with_backoff()
def _elb_add_tags_call(client, arn: str, tags: list[dict]) -> None:
    client.add_tags(ResourceArns=[arn], Tags=tags)


class LiveExecutor:
    """Etapa 2c: chama a API de escrita de cada estratégia."""

    def tag_generic_batch(
        self, session: boto3.Session, regiao: str, arns: list[str], tag_key: str, tag_value: str
    ) -> dict[str, ResourceOutcome]:
        client = session.client("resourcegroupstaggingapi", region_name=regiao)
        try:
            resp = _tag_resources_call(client, arns, {tag_key: tag_value})
        except ClientError as exc:
            # O lote inteiro vira falha; os próximos lotes seguem.
            erro = exc.response.get("Error", {})
            outcome = _classificar_erro_aws(erro.get("Code", ""), erro.get("Message", ""))
            return {arn: outcome for arn in arns}

        falhas = resp.get("FailedResourcesMap", {})
        resultado: dict[str, ResourceOutcome] = {}
        for arn in arns:
            info_falha = falhas.get(arn)
            if info_falha is None:
                resultado[arn] = ResourceOutcome(resultado=RESULTADO_TAGUEADO_SUCESSO)
            else:
                resultado[arn] = _classificar_erro_aws(
                    info_falha.get("ErrorCode", ""), info_falha.get("ErrorMessage", "")
                )
        return resultado

    def tag_single(
        self,
        session: boto3.Session,
        estrategia: ApiStrategy,
        resource: TaggableResource,
        tag_key: str,
        tag_value: str,
    ) -> ResourceOutcome:
        try:
            if estrategia in (ApiStrategy.EKS_CLUSTER, ApiStrategy.EKS_NODE_GROUP):
                client = session.client("eks", region_name=resource.regiao)
                _eks_tag_resource_call(client, resource.arn, {tag_key: tag_value})
            elif estrategia is ApiStrategy.BEDROCK_PROFILE:
                client = session.client("bedrock", region_name=resource.regiao)
                _bedrock_tag_resource_call(client, resource.arn, [{"key": tag_key, "value": tag_value}])
            else:  # ELB_LOAD_BALANCER
                client = session.client("elbv2", region_name=resource.regiao)
                _elb_add_tags_call(client, resource.arn, [{"Key": tag_key, "Value": tag_value}])
        except ClientError as exc:
            erro = exc.response.get("Error", {})
            return _classificar_erro_aws(erro.get("Code", ""), erro.get("Message", ""))
        return ResourceOutcome(resultado=RESULTADO_TAGUEADO_SUCESSO)


# ---------------------------------------------------------------------------
# Orquestrador
# ---------------------------------------------------------------------------


def _classificar_estado_revalidado(
    estado: _RevalidatedState | _RevalidationFailure, tag_value: str
) -> ResourceOutcome | None:
    """Aplica a regra de `decision.py` ao estado relido.

    Devolve o resultado (sem escrita) ou `None` quando o recurso segue para a
    escrita. Falha de leitura vem antes de tudo: `recurso_nao_encontrado_na_revalidacao`
    se o recurso sumiu, `revalidacao_falhou` nos demais casos."""
    if isinstance(estado, _RevalidationFailure):
        codigo = (estado.detalhe_erro or {}).get("codigo", "")
        if codigo in _CODIGOS_RECURSO_NAO_ENCONTRADO:
            return ResourceOutcome(
                resultado=RESULTADO_RECURSO_NAO_ENCONTRADO_NA_REVALIDACAO, detalhe_erro=estado.detalhe_erro
            )
        return ResourceOutcome(resultado=RESULTADO_REVALIDACAO_FALHOU, detalhe_erro=estado.detalhe_erro)
    if estado.valor_atual == tag_value:
        return ResourceOutcome(resultado=RESULTADO_JA_TAGUEADO)
    if estado.iac_tipo in _IAC_DETECTADO:
        return ResourceOutcome(resultado=RESULTADO_IAC_DETECTADO_NA_REVALIDACAO)
    # Outro valor ou chave parecida seguem para a escrita (estado vira `estado_anterior`).
    return None


def _resultado_entry(
    resource: TaggableResource,
    estrategia: ApiStrategy,
    resultado: str,
    detalhe_erro: dict | None = None,
    estado: "_RevalidatedState | _RevalidationFailure | None" = None,
) -> dict:
    """`estado`: o lido na revalidação, quando houve — é a fonte mais atual
    do "antes"; sem ele, vale o que a Etapa 2a viu."""
    if isinstance(estado, _RevalidatedState):
        estado_anterior = estado.estado_anterior()
        iac = estado.iac
    else:
        estado_anterior = resource.estado_anterior
        iac = None
    return {
        "arn": resource.arn,
        "servico": resource.servico,
        "regiao": resource.regiao,
        "tipo_recurso": resource.tipo_recurso,
        "estrategia_api": estrategia.value,
        "resultado": resultado,
        "detalhe_erro": detalhe_erro,
        "estado_anterior": estado_anterior,
        "iac": iac,
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
        pendentes: list[TaggableResource] = []
        for r in recursos_regiao:
            estado = estado_atual.get(r.arn)
            outcome = _classificar_estado_revalidado(estado, tag_value) if estado is not None else None
            if outcome is not None:
                resultados.append(
                    _resultado_entry(r, ApiStrategy.GENERICO, outcome.resultado, outcome.detalhe_erro, estado)
                )
            else:
                pendentes.append(r)

        for lote in _chunk(pendentes, _TAMANHO_MAX_LOTE_GENERICO):
            arns = [r.arn for r in lote]
            resultado_por_arn = executor.tag_generic_batch(session, regiao, arns, tag_key, tag_value)
            for r in lote:
                outcome = resultado_por_arn.get(r.arn) or ResourceOutcome(resultado=RESULTADO_ERRO)
                resultados.append(
                    _resultado_entry(
                        r, ApiStrategy.GENERICO, outcome.resultado, outcome.detalhe_erro, estado_atual.get(r.arn)
                    )
                )
    return resultados


def _revalidate_dedicado(
    session: boto3.Session, estrategia: ApiStrategy, resources: list[TaggableResource], tag_key: str
) -> dict[str, _RevalidatedState | _RevalidationFailure]:
    if estrategia in (ApiStrategy.EKS_CLUSTER, ApiStrategy.EKS_NODE_GROUP):
        return {
            r.arn: _revalidate_eks_or_bedrock(session, "eks", r.regiao, r, tag_key) for r in resources
        }
    if estrategia is ApiStrategy.BEDROCK_PROFILE:
        return {
            r.arn: _revalidate_eks_or_bedrock(session, "bedrock", r.regiao, r, tag_key)
            for r in resources
        }
    # ELB: leitura em lotes de 20 por região; a escrita é 1 a 1.
    resultado: dict[str, _RevalidatedState] = {}
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
            estado = estado_atual.get(r.arn)
            outcome_revalidacao = _classificar_estado_revalidado(estado, tag_value) if estado is not None else None
            if outcome_revalidacao is not None:
                resultados.append(
                    _resultado_entry(
                        r, estrategia, outcome_revalidacao.resultado, outcome_revalidacao.detalhe_erro, estado
                    )
                )
                continue
            outcome = executor.tag_single(session, estrategia, r, tag_key, tag_value)
            resultados.append(_resultado_entry(r, estrategia, outcome.resultado, outcome.detalhe_erro, estado))
    return resultados


# ---------------------------------------------------------------------------
# Salvaguardas
# ---------------------------------------------------------------------------


class DecisionReportDesatualizadoError(Exception):
    """A descoberta por trás do relatório de decisão é mais velha que o limite pedido."""


class RevalidacaoObrigatoriaError(Exception):
    """Modo live sem revalidação não é permitido (ver docstring do módulo)."""


def _idade_em_horas(timestamp_iso: str) -> float:
    momento = datetime.strptime(timestamp_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - momento).total_seconds() / 3600


def _checar_idade_do_relatorio(decision_report: dict, max_decision_age_hours: float | None) -> None:
    if max_decision_age_hours is None:
        return
    timestamp = decision_report.get("descoberta_executada_em")
    if not timestamp:
        raise DecisionReportDesatualizadoError(
            "Relatório de decisão sem 'descoberta_executada_em' — não é possível "
            "verificar a idade da descoberta subjacente (gerado por uma versão "
            "antiga de decision.py?)."
        )
    idade = _idade_em_horas(timestamp)
    if idade > max_decision_age_hours:
        raise DecisionReportDesatualizadoError(
            f"A descoberta (Etapa 1) que embasa este relatório de decisão tem "
            f"{idade:.1f}h (limite: {max_decision_age_hours}h) — rode a Etapa "
            "1/2a novamente antes de aplicar."
        )


# ---------------------------------------------------------------------------
# Relatório final
# ---------------------------------------------------------------------------

_CATEGORIA_POR_DECISAO_2A = {
    DECISAO_PULAR_IAC: CATEGORIA_PULADO_IAC,
    DECISAO_JA_OK: CATEGORIA_JA_OK,
}

# resultado -> (categoria_final, origem)
_CATEGORIA_POR_RESULTADO = {
    RESULTADO_SIMULADO_OK: (CATEGORIA_SIMULADO_SUCESSO, "execucao"),
    RESULTADO_TAGUEADO_SUCESSO: (CATEGORIA_TAGUEADO_SUCESSO, "execucao"),
    RESULTADO_JA_TAGUEADO: (CATEGORIA_JA_OK, "revalidacao"),
    RESULTADO_IAC_DETECTADO_NA_REVALIDACAO: (CATEGORIA_PULADO_IAC, "revalidacao"),
    RESULTADO_RECURSO_NAO_ENCONTRADO_NA_REVALIDACAO: (CATEGORIA_FALHOU, "revalidacao"),
    RESULTADO_REVALIDACAO_FALHOU: (CATEGORIA_FALHOU, "revalidacao"),
}


def _categoria_final_do_resultado(resultado: str) -> tuple[str, str]:
    """`(categoria_final, origem)` de um resultado; o que não está mapeado é falha."""
    return _CATEGORIA_POR_RESULTADO.get(resultado, (CATEGORIA_FALHOU, "execucao"))


ACAO_ADICIONADA = "adicionada"
ACAO_ADICIONADA_COM_TAG_SIMILAR = "adicionada_com_tag_similar"
ACAO_SOBRESCRITA = "sobrescrita"


def _acao(categoria: str, estado_anterior: dict | None) -> str | None:
    """O que a escrita fez (ou faria, em dry-run) com a tag. Só para
    sucesso — numa falha nada mudou."""
    if categoria not in (CATEGORIA_TAGUEADO_SUCESSO, CATEGORIA_SIMULADO_SUCESSO):
        return None
    if not estado_anterior:
        return ACAO_ADICIONADA
    if estado_anterior.get(TAG_KEY) is not None:
        return ACAO_SOBRESCRITA
    return ACAO_ADICIONADA_COM_TAG_SIMILAR


def _entrada_erro_classificacao(arn, servico, mensagem) -> dict:
    return {
        "arn": arn,
        "servico": servico,
        "regiao": None,
        "tipo_recurso": None,
        "categoria_final": CATEGORIA_ERRO_CLASSIFICACAO,
        "origem": "decisao",
        "estrategia_api": None,
        "resultado": None,
        "detalhe_erro": {"codigo": None, "mensagem": mensagem},
        "motivo": None,
        "estado_anterior": None,
        "acao": None,
        "iac": None,
    }


def build_execution_report(
    decision_report: dict,
    resultados_taguear: list[dict],
    expected_tag_value: str,
    dry_run: bool,
    execucao_inicial: bool = False,
) -> dict:
    """Relatório final da Etapa 2b/2c com todos os recursos do relatório de decisão.

    Inclui o resultado dos que tagueiam, os `pular_iac`/`ja_ok` da Etapa 2a e os
    erros de classificação. Decisões do MVP1 (`conflito`, `revisar_tag_similar`)
    viram `erro_classificacao`: a regra mudou e nada é escrito com base nelas."""
    recursos_finais: list[dict] = []

    motivo_por_arn = {r.get("arn"): r.get("motivo") for r in decision_report.get("recursos") or []}
    iac_por_arn = {r.get("arn"): r.get("iac") for r in decision_report.get("recursos") or []}
    for r in resultados_taguear:
        categoria, origem = _categoria_final_do_resultado(r["resultado"])
        estado_anterior = r.get("estado_anterior")
        recursos_finais.append(
            {
                "arn": r["arn"],
                "servico": r["servico"],
                "regiao": r["regiao"],
                "tipo_recurso": r["tipo_recurso"],
                "categoria_final": categoria,
                "origem": origem,
                "estrategia_api": r["estrategia_api"],
                "resultado": r["resultado"],
                "detalhe_erro": r["detalhe_erro"],
                "motivo": motivo_por_arn.get(r["arn"]),
                "estado_anterior": estado_anterior,
                "acao": _acao(categoria, estado_anterior),
                "iac": r.get("iac") or iac_por_arn.get(r["arn"]),
            }
        )

    for r in decision_report.get("recursos") or []:
        decisao = r.get("decisao")
        if decisao in DECISOES_LEGADAS:
            recursos_finais.append(
                _entrada_erro_classificacao(
                    r.get("arn"),
                    r.get("servico"),
                    f"decisão {decisao!r} é de um relatório de versão anterior — rode 'decide' "
                    "de novo (a regra de negócio mudou no MVP2)",
                )
            )
            continue
        categoria = _CATEGORIA_POR_DECISAO_2A.get(decisao)
        if categoria is None:
            continue  # decisão que tagueia (já coberta acima) ou valor inesperado
        recursos_finais.append(
            {
                "arn": r["arn"],
                "servico": r["servico"],
                "regiao": r["regiao"],
                "tipo_recurso": r.get("tipo_recurso"),
                "categoria_final": categoria,
                "origem": "decisao",
                "estrategia_api": None,
                "resultado": None,
                "detalhe_erro": None,
                "motivo": r.get("motivo"),
                "estado_anterior": r.get("estado_anterior"),
                "acao": None,
                "iac": r.get("iac"),
            }
        )

    # Recursos que a Etapa 2a não conseguiu classificar (registro malformado).
    for r in decision_report.get("erros") or []:
        recursos_finais.append(_entrada_erro_classificacao(r.get("arn"), r.get("servico"), r.get("erro")))

    categoria_counts = Counter(r["categoria_final"] for r in recursos_finais)
    servico_counts = Counter(r["servico"] for r in recursos_finais)
    resultado_counts = Counter(r["resultado"] for r in recursos_finais if r["resultado"] is not None)
    estrategia_counts = Counter(r["estrategia_api"] for r in recursos_finais if r["estrategia_api"] is not None)

    return {
        "conta_id": decision_report.get("conta_id"),
        "executado_em": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "valor_tag_esperado": expected_tag_value,
        "modo": "dry_run" if dry_run else "live",
        "execucao_inicial": execucao_inicial,
        "resumo": {
            "total_recursos": len(recursos_finais),
            "por_categoria_final": {c: categoria_counts.get(c, 0) for c in _TODAS_AS_CATEGORIAS_FINAIS},
            "por_resultado": dict(sorted(resultado_counts.items())),
            "por_estrategia_api": dict(sorted(estrategia_counts.items())),
            "por_servico": dict(sorted(servico_counts.items())),
            "por_acao": dict(sorted(Counter(r["acao"] for r in recursos_finais if r.get("acao")).items())),
        },
        "recursos": recursos_finais,
    }


def run_tagging_execution(
    decision_report: dict,
    session: boto3.Session,
    expected_tag_value: str,
    dry_run: bool = True,
    revalidate: bool = True,
    executor: Executor | None = None,
    execucao_inicial: bool = False,
    max_decision_age_hours: float | None = None,
) -> dict:
    """Ponto de entrada das Etapas 2b (`dry_run=True`) e 2c (`dry_run=False`).

    `max_decision_age_hours`: recusa agir se a descoberta for mais velha que isso.
    `execucao_inicial`: só marca o relatório (primeira execução da conta)."""
    if not dry_run and not revalidate:
        raise RevalidacaoObrigatoriaError(
            "revalidate=False não é permitido com dry_run=False — executar de "
            "verdade sem revalidar arrisca taguear um recurso que passou a ser de "
            "IaC depois da Etapa 2a e perder o registro do estado anterior."
        )
    _checar_idade_do_relatorio(decision_report, max_decision_age_hours)

    if executor is None:
        executor = DryRunExecutor() if dry_run else LiveExecutor()

    taggable = select_taggable(decision_report)

    genericos = [r for r in taggable if route_strategy(r) is ApiStrategy.GENERICO]
    dedicados_por_estrategia: dict[ApiStrategy, list[TaggableResource]] = {}
    for r in taggable:
        estrategia = route_strategy(r)
        if estrategia is not ApiStrategy.GENERICO:
            dedicados_por_estrategia.setdefault(estrategia, []).append(r)

    resultados_taguear = _processar_genericos(
        session, executor, genericos, TAG_KEY, expected_tag_value, revalidate
    )
    resultados_taguear += _processar_dedicados(
        session, executor, dedicados_por_estrategia, TAG_KEY, expected_tag_value, revalidate
    )

    return build_execution_report(
        decision_report, resultados_taguear, expected_tag_value, dry_run, execucao_inicial
    )
