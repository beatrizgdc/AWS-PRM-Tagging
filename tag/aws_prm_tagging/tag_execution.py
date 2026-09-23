"""Execução do tagueamento (Etapa 2b: dry-run; Etapa 2c: escrita real) a
partir do relatório de decisão da Etapa 2a (`decision.build_decision_report`).

Um único ponto de entrada, `run_tagging_execution`, atende as duas etapas —
`dry_run=True` (default) é a Etapa 2b, `dry_run=False` é a Etapa 2c. A
única coisa que muda entre os dois modos é qual `Executor` está por baixo
(`DryRunExecutor`, que só loga, vs. `LiveExecutor`, que chama boto3 de
verdade) — todo o resto (roteamento de API, agrupamento em lotes,
revalidação, classificação de erro, montagem do relatório final) é
exatamente o mesmo código, nunca duplicado entre as duas etapas. Em
`dry_run=True`, nenhuma chamada de ESCRITA é feita na conta — só leitura
(revalidação, salvo com `revalidate=False`).

## Garantia estrutural de que só `decisao == "taguear"` gera uma chamada

`select_taggable()` é o único ponto de entrada aceito pelo resto do módulo.
Ela converte cada recurso do relatório de decisão em um `TaggableResource`
— um tipo que **não tem campo `decisao`**. Todas as funções abaixo
(`route_strategy`, `_processar_genericos`/`_processar_dedicados`, os
métodos de `Executor`) recebem `TaggableResource`, nunca o dict bruto do
relatório 2a. Não existe caminho de código que aceite
`pular_iac`/`revisar_tag_similar`/`ja_ok`/`conflito` como parâmetro — é um erro de tipo, não uma
condição que alguém possa esquecer de checar.

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

Toda execução (dry-run ou, futuramente, live) revalida o estado atual do
recurso imediatamente antes de agir sobre ele — controlável via
`revalidate=False` em `run_tagging_execution`, default `True`. Dois motivos:

1. Condição de corrida: o recurso pode ter sido deletado, tido a tag
   alterada, ou passado a ser gerenciado por IaC, entre a execução da
   Etapa 1/2a e a execução desta etapa.
2. Idempotência entre execuções: se uma execução anterior já tiver
   tagueado um recurso (ou outra automação já tiver aplicado a tag), rodar
   de novo sobre o mesmo relatório de decisão não deve gerar uma nova
   chamada de escrita para esse recurso. Isso é o que torna reexecuções
   idempotentes por construção: o estado atual do recurso na AWS *é* a
   fonte da verdade de "já foi feito ou não", sem precisar de nenhum
   arquivo de progresso separado.

**A revalidação reaplica a MESMA regra de precedência de `decision.py`**
(`_classificar_estado_revalidado`, usada tanto pelo caminho genérico quanto
pelos dedicados) — não só confere se a tag já está com o valor esperado.
Quatro desfechos possíveis, nesta ordem (idêntica à de `decision._decidir`):

1. Valor atual == valor esperado → `"ja_tagueado"`. Nenhuma chamada de
   escrita.
2. Valor atual presente e DIFERENTE do esperado → `"conflito_na_revalidacao"`.
   Nenhuma chamada de escrita — um valor conflitante nunca é sobrescrito
   automaticamente, apareça ele já na Etapa 2a ou só no momento da escrita.
   IaC é irrelevante aqui (mesma regra de `decision.py`: um conflito nunca
   vira "pular IaC" só porque o recurso também é gerenciado por IaC).
3. Tag ausente E IaC detectado (só verificado quando a tag está mesmo
   ausente — ver item 2) → `"iac_detectado_na_revalidacao"`. Nenhuma
   chamada de escrita — cobre o recurso que passou a ser gerenciado por
   IaC depois da Etapa 1/2a e antes desta execução.
4. Tag ausente, sem IaC, mas uma tag de grafia parecida (ex.: `AWS-APN-ID`)
   apareceu entre a Etapa 2a e esta execução →
   `"tag_similar_encontrada_na_revalidacao"`. Nenhuma chamada de escrita —
   mesma razão de `decision.py`: aplicar `aws-apn-id` por cima criaria uma
   segunda chave quase-duplicada em vez de corrigir o erro original.
5. Nenhum dos quatro → segue para a tentativa de escrita normal.

(Uma versão anterior deste módulo só verificava o item 1, tratando "sem
tag" e "tag com valor diferente" da mesma forma — o que faria uma execução
real tentar sobrescrever um valor conflitante encontrado só no momento da
escrita. Corrigido: a checagem agora é sempre de 4 vias, nunca só 2. O item
4 acima foi adicionado depois — uma versão anterior revalidava IaC mas não
tag similar, então um `AWS-APN-ID` criado depois da Etapa 2a passava
despercebido e o `--live` aplicava `aws-apn-id` por cima, gerando duas
chaves quase-duplicadas no recurso.)

A leitura de revalidação busca o conjunto COMPLETO de tags de cada
recurso (não só a `aws-apn-id`) — precisa disso para o item 3 acima —, por
estratégia:

- Genérico: uma varredura por região via `tag:GetResources` sem
  `TagFilters` (a versão anterior filtrava por `TagFilters=[{"Key":
  "aws-apn-id"}]`, mas isso escondia qualquer outra tag do resultado,
  inclusive as de IaC), parando assim que todos os ARNs pedidos já foram
  encontrados. Um ARN que não aparece na varredura significa "sem tag
  nenhuma hoje" — o que inclui tanto "nunca teve tag" quanto (raro)
  "recurso foi deletado"; qualquer um dos dois casos é seguro seguir para a
  tentativa de tagueamento (se deletado, a chamada real falha de forma
  limpa e é reportada, sem dano).
- EKS (cluster/node group): `eks:ListTagsForResource`, 1 ARN por chamada
  (mesma limitação da escrita) — já devolve o conjunto completo de tags,
  sem custo extra para checar IaC.
- Bedrock: `bedrock:ListTagsForResource`, 1 ARN por chamada — já usado do
  mesmo jeito em `resource_discovery.discover_bedrock_resources`, também
  já completo.
- ELB: `elasticloadbalancing:DescribeTags`, até 20 ARNs por chamada (só a
  leitura aceita lote; a escrita não — ver acima). Mesmo client já usado em
  `resource_discovery.discover_eks_resources`, também já completo.

**Se a leitura de revalidação falhar** (ex.: `AccessDenied` na permissão de
leitura, throttling esgotado), o recurso NUNCA prossegue para a tentativa de
escrita — nem em dry-run, nem em live. (Uma versão anterior deste módulo
deixava a tentativa prosseguir quando a revalidação falhava — o que, em modo
live, significava tentar `tag:TagResources`/etc. sobre um recurso cujo
estado atual era desconhecido, arriscando sobrescrever um conflito que a
leitura de revalidação falhou em enxergar. Corrigido: falha de leitura vira
o mesmo tipo de "não escrever" que um conflito genuíno.) Por isso
`dry_run=False` (live) nunca aceita `revalidate=False` — ver
`RevalidacaoObrigatoriaError` — sem a revalidação ligada, a escrita real
perderia essa proteção inteira.

Dentro desse caso, o resultado se divide em dois, pelo código do erro
(`_classificar_estado_revalidado`): se for um dos códigos de "recurso não
encontrado" (`_CODIGOS_RECURSO_NAO_ENCONTRADO` — ex.: um load balancer
apagado entre a Etapa 1 e esta execução) →
`"recurso_nao_encontrado_na_revalidacao"`; qualquer outro erro (permissão,
throttling esgotado, etc.) → `"revalidacao_falhou"`, genérico. Mesma
distinção operacional que já existe na classificação de erro de ESCRITA
(`_classificar_erro_aws`): "recurso sumiu" é o relatório da Etapa 1 estar
desatualizado, esperado ocasionalmente — não deveria disparar o mesmo alerta
que uma falha de permissão ou throttling esgotado, que geralmente exige
ação.

## Classificação de erro na escrita real (`LiveExecutor`)

Toda chamada de escrita é decorada com `retry.with_backoff()` — throttling
é absorvido silenciosamente e só vira erro no relatório se esgotar todas as
tentativas. Erros definitivos são classificados em 3 categorias
(`_classificar_erro_aws`), pelo `Code` devolvido pela AWS:

- `RESULTADO_ERRO_PERMISSAO` — `AccessDenied`/variantes. Categoria própria
  porque é operacionalmente diferente de um erro pontual: precisa de ajuste
  de política IAM, não é transitório.
- `RESULTADO_RECURSO_NAO_ENCONTRADO` — o recurso sumiu entre a Etapa 1 e
  esta execução (`ResourceNotFoundException` e variantes por serviço).
  Também categoria própria: não é falha operacional, é o relatório da
  Etapa 1 estar desatualizado — esperado ocasionalmente, não deveria
  disparar o mesmo alerta que `AccessDenied`.
- `RESULTADO_ERRO` — qualquer outro código (`ValidationException`, tipo não
  suportado, etc.) — o código/mensagem originais ficam em `detalhe_erro`
  mesmo assim.

Uma falha (de qualquer categoria) nunca aborta o resto da execução: no
caminho genérico, se a chamada de lote inteira levantar exceção, só os
ARNs DAQUELE lote viram falha — o próximo lote/região segue normalmente.
No caminho dedicado, cada recurso é uma chamada isolada por natureza (sem
batch). `tag:TagResources` também pode devolver sucesso HTTP com falha
parcial (`FailedResourcesMap`) — cada ARN listado ali vira falha
individual, os demais do mesmo lote viram sucesso.

## Idade máxima do relatório de decisão

`max_decision_age_hours` em `run_tagging_execution` (opcional, `None` =
sem checagem) recusa agir — levanta `DecisionReportDesatualizadoError` —
quando `decision_report["descoberta_executada_em"]` (propagado por
`decision.build_decision_report` a partir do `executado_em` da Etapa 1) é
mais velho que o limite. Isso é ortogonal à revalidação por recurso: a
revalidação cobre "esse recurso específico mudou de estado"; a idade do
relatório cobre "esse universo de recursos pode estar amplamente
desatualizado" (recursos novos não apareceriam de jeito nenhum,
independente de revalidação).

## Relatório final (`build_execution_report`)

O relatório interno de `_processar_genericos`/`_processar_dedicados` só
cobre recursos `decisao == "taguear"` (a única categoria que passa por
`select_taggable`). O relatório final de saída da Etapa 2b/2c cobre todas
as categorias que fazem sentido para um humano/dashboard —
`tagueado_sucesso` (só live) / `simulado_sucesso` (só dry-run, NUNCA
misturado com sucesso real na mesma categoria) / `falhou` /
`pulado_iac` / `revisar_tag_similar` / `conflito` / `ja_ok` /
`erro_classificacao` — então `build_execution_report` mescla três grupos:

- Os `taguear` (resultado desta execução, mapeado por
  `_categoria_final_do_resultado` — inclui os pulados na revalidação:
  `ja_tagueado` vira `ja_ok`, `conflito_na_revalidacao` vira `conflito`,
  `iac_detectado_na_revalidacao` vira `pulado_iac`,
  `tag_similar_encontrada_na_revalidacao` vira `revisar_tag_similar`,
  `revalidacao_falhou`/`recurso_nao_encontrado_na_revalidacao` viram `falhou`).
- Os `pular_iac`/`revisar_tag_similar`/`ja_ok`/`conflito` que a própria
  Etapa 2a já decidiu — nunca passam por este módulo, nunca geram chamada
  nenhuma, entram carregados direto no relatório final com a mesma
  categoria.
- Os `erros` da Etapa 2a (recursos malformados que nem chegaram a ser
  classificados) — antes ficavam presos em `decision_report["erros"]` e
  nunca apareciam no relatório desta etapa; agora entram como
  `erro_classificacao`, com a mensagem original em `detalhe_erro`.

Campo `origem` (`"decisao"` / `"revalidacao"` / `"execucao"`) preserva em
qual momento a classificação foi de fato feita — útil para auditoria e para
a Etapa 4 distinguir, por exemplo, um conflito visto já na descoberta
original de um conflito que só apareceu no momento da escrita (possível
tag de outro parceiro AWS aplicada nesse meio-tempo).
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

from .decision import (
    DECISAO_CONFLITO,
    DECISAO_JA_OK,
    DECISAO_PULAR_IAC,
    DECISAO_REVISAR_TAG_SIMILAR,
    DECISAO_TAGUEAR,
)
from .iac_detection import IAC_CLOUDFORMATION, IAC_DESCONHECIDO, IAC_TERRAFORM_HEURISTICO, detect_iac
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

# Mesma regra de precedência de `decision.py`: só entra aqui quando a TAG
# está ausente (verificado antes, na chamada) — IaC nunca tem precedência
# sobre um valor de tag já presente (conflito), só sobre a ausência dela.
_IAC_DETECTADO = frozenset({IAC_CLOUDFORMATION, IAC_TERRAFORM_HEURISTICO})

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
RESULTADO_TAGUEADO_SUCESSO = "tagueado_sucesso"
RESULTADO_JA_TAGUEADO = "ja_tagueado"
RESULTADO_CONFLITO_NA_REVALIDACAO = "conflito_na_revalidacao"
RESULTADO_IAC_DETECTADO_NA_REVALIDACAO = "iac_detectado_na_revalidacao"
RESULTADO_TAG_SIMILAR_NA_REVALIDACAO = "tag_similar_encontrada_na_revalidacao"
RESULTADO_RECURSO_NAO_ENCONTRADO_NA_REVALIDACAO = "recurso_nao_encontrado_na_revalidacao"
RESULTADO_REVALIDACAO_FALHOU = "revalidacao_falhou"
RESULTADO_ERRO_PERMISSAO = "erro_permissao"
RESULTADO_RECURSO_NAO_ENCONTRADO = "recurso_nao_encontrado"
RESULTADO_ERRO = "erro"

# Categoria final de um recurso no relatório de execução (`categoria_final`
# em `build_execution_report`) — mais grossa que `resultado` acima, pensada
# para dashboard/leitura humana. Ver `_categoria_final_do_resultado`.
#
# `CATEGORIA_SIMULADO_SUCESSO` é separada de `CATEGORIA_TAGUEADO_SUCESSO` de
# propósito: um dry-run nunca deveria contar como "tagueado" num dashboard
# que agregue por `categoria_final` sem também filtrar por `modo` — cada
# relatório já carrega `modo` no topo, mas um resultado simulado e um real
# não deveriam cair no mesmo balde de contagem por padrão.
CATEGORIA_TAGUEADO_SUCESSO = "tagueado_sucesso"
CATEGORIA_SIMULADO_SUCESSO = "simulado_sucesso"
CATEGORIA_FALHOU = "falhou"
CATEGORIA_PULADO_IAC = "pulado_iac"
CATEGORIA_REVISAR_TAG_SIMILAR = "revisar_tag_similar"
CATEGORIA_CONFLITO = "conflito"
CATEGORIA_JA_OK = "ja_ok"
CATEGORIA_ERRO_CLASSIFICACAO = "erro_classificacao"
_TODAS_AS_CATEGORIAS_FINAIS = (
    CATEGORIA_TAGUEADO_SUCESSO,
    CATEGORIA_SIMULADO_SUCESSO,
    CATEGORIA_FALHOU,
    CATEGORIA_PULADO_IAC,
    CATEGORIA_REVISAR_TAG_SIMILAR,
    CATEGORIA_CONFLITO,
    CATEGORIA_JA_OK,
    CATEGORIA_ERRO_CLASSIFICACAO,
)

# Códigos de erro AWS conhecidos, mapeados para uma categoria de resultado
# própria — o resto (throttling esgotado, validação, tipo não suportado
# etc.) cai em RESULTADO_ERRO genérico, com o código original preservado em
# `detalhe_erro`. Não é uma lista exaustiva — só os casos operacionalmente
# distintos o suficiente para merecer contagem própria no relatório (ver
# seção 4 do plano da Etapa 2c: permissão precisa de ajuste de política,
# "não encontrado" é relatório desatualizado, não falha operacional).
_CODIGOS_ERRO_PERMISSAO = frozenset({"AccessDenied", "AccessDeniedException", "UnauthorizedException"})
_CODIGOS_RECURSO_NAO_ENCONTRADO = frozenset(
    {
        "ResourceNotFoundException",
        "NoSuchEntity",
        "ClusterNotFoundException",
        "NodegroupNotFoundException",
        # Códigos do ELBv2 — API modelada (shape name == Code), não estilo
        # legado sem sufixo. Alta confiança, mas não confirmado em sandbox
        # (ver melhorias-futuras.md).
        "LoadBalancerNotFoundException",
        "TargetGroupNotFoundException",
        "ListenerNotFoundException",
        "RuleNotFoundException",
        "TrustStoreNotFoundException",
    }
)


@dataclass(frozen=True)
class ResourceOutcome:
    """Resultado de uma tentativa de tagueamento (real ou simulada) sobre um
    único recurso — devolvido por `Executor.tag_single` e por cada valor de
    `Executor.tag_generic_batch`. `detalhe_erro` só é preenchido quando
    `resultado` não é um sucesso."""

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
    """Estado atual de um recurso, apurado com SUCESSO imediatamente antes
    de agir sobre ele — a MESMA checagem de precedência de `decision.py`
    (valor presente sempre decide antes de olhar IaC/tag similar; IaC tem
    precedência sobre tag similar; os dois só importam quando a tag está
    ausente), reaplicada no momento da escrita, não só no momento da
    decisão original da Etapa 2a. Ver `_classificar_estado_revalidado`."""

    valor_atual: str | None
    iac_tipo: str
    tag_similar_encontrada: bool


@dataclass(frozen=True)
class _RevalidationFailure:
    """A leitura de revalidação foi TENTADA mas FALHOU (erro de permissão,
    throttling esgotado, etc.) — distinto de "nunca tentada"
    (`revalidate=False`, onde o dict de estado por região/estratégia fica
    vazio e o recurso é tratado como pendente normalmente, sem nenhuma
    garantia). Um recurso marcado com isso NUNCA prossegue para a
    tentativa de escrita, em nenhum dos dois modos (dry-run ou live) — sem
    saber o estado atual do recurso, a resposta segura é não arriscar
    sobrescrever um conflito que a leitura falhou em enxergar."""

    detalhe_erro: dict | None = None


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
def _get_resources_page(client, pagination_token: str | None) -> dict:
    kwargs: dict = {"ResourcesPerPage": 100}
    if pagination_token:
        kwargs["PaginationToken"] = pagination_token
    return client.get_resources(**kwargs)


def _revalidate_generic(
    session: boto3.Session, regiao: str, arns: set[str], tag_key: str
) -> dict[str, _RevalidatedState | _RevalidationFailure]:
    """Varredura via `tag:GetResources` cobre todos os ARNs genéricos
    revalidados nesta região de uma vez. Deliberadamente SEM `TagFilters`
    (ao contrário de uma versão anterior deste módulo): revalidar exige o
    conjunto COMPLETO de tags de cada recurso, não só a `aws-apn-id` — é o
    que permite reconfirmar o status de IaC no momento da escrita, não só
    o valor da tag-alvo (ver docstring do módulo). Para de paginar assim
    que todos os ARNs pedidos já foram encontrados, para não pagar o custo
    de uma varredura completa da região quando o alvo aparece cedo.

    Se a leitura falhar no meio da paginação, os ARNs já encontrados em
    páginas anteriores mantêm seu estado normal (são conhecidos de
    verdade) — só os ARNs ainda `pendentes` no momento da falha viram
    `_RevalidationFailure`, nunca `_RevalidatedState`."""
    client = session.client("resourcegroupstaggingapi", region_name=regiao)
    encontrados: dict[str, _RevalidatedState] = {}
    pendentes = set(arns)
    token = None
    erro_leitura: dict | None = None
    try:
        while pendentes:
            resp = _get_resources_page(client, token)
            for mapping in resp.get("ResourceTagMappingList", []):
                arn = mapping["ResourceARN"]
                if arn not in pendentes:
                    continue
                tags = tags_list_to_dict(mapping.get("Tags", []))
                encontrados[arn] = _RevalidatedState(
                    valor_atual=tags.get(tag_key),
                    iac_tipo=detect_iac(tags)["tipo"],
                    tag_similar_encontrada=bool(find_similar_tag_keys(tags)),
                )
                pendentes.discard(arn)
            token = resp.get("PaginationToken")
            if not token:
                break
    except ClientError as exc:
        erro = exc.response.get("Error", {})
        erro_leitura = {"codigo": erro.get("Code", ""), "mensagem": erro.get("Message", "")}
        logger.exception(
            "Falha ao revalidar tags genéricas em %s — os %d ARN(s) ainda não "
            "encontrados até aqui ficam marcados como revalidação falha (não "
            "prosseguem para tentativa de escrita)",
            regiao,
            len(pendentes),
        )

    resultado: dict[str, _RevalidatedState | _RevalidationFailure] = dict(encontrados)
    for arn in arns:
        if arn in resultado:
            continue
        if erro_leitura is not None:
            resultado[arn] = _RevalidationFailure(detalhe_erro=erro_leitura)
        else:
            resultado[arn] = _RevalidatedState(
                valor_atual=None, iac_tipo=IAC_DESCONHECIDO, tag_similar_encontrada=False
            )
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
        return _RevalidatedState(
            valor_atual=tags.get(tag_key),
            iac_tipo=detect_iac(tags)["tipo"],
            tag_similar_encontrada=bool(find_similar_tag_keys(tags)),
        )
    except ClientError as exc:
        erro = exc.response.get("Error", {})
        logger.exception(
            "Falha ao revalidar tags de %s (%s) — recurso marcado como revalidação "
            "falha, não prossegue para tentativa de escrita",
            resource.arn,
            service_name,
        )
        return _RevalidationFailure(detalhe_erro={"codigo": erro.get("Code", ""), "mensagem": erro.get("Message", "")})


def _revalidated_state_from_tags(tags: dict[str, str], tag_key: str) -> _RevalidatedState:
    return _RevalidatedState(
        valor_atual=tags.get(tag_key),
        iac_tipo=detect_iac(tags)["tipo"],
        tag_similar_encontrada=bool(find_similar_tag_keys(tags)),
    )


def _revalidate_elb(
    session: boto3.Session, regiao: str, resources: list[TaggableResource], tag_key: str
) -> dict[str, _RevalidatedState | _RevalidationFailure]:
    client = session.client("elbv2", region_name=regiao)
    resultado: dict[str, _RevalidatedState | _RevalidationFailure] = {
        r.arn: _RevalidatedState(valor_atual=None, iac_tipo=IAC_DESCONHECIDO, tag_similar_encontrada=False)
        for r in resources
    }
    for lote in _chunk([r.arn for r in resources], 20):  # describe_tags aceita até 20 ARNs
        try:
            resp = elb_describe_tags(client, lote)
        except ClientError:
            # A chamada do lote inteiro falhou — comportamento conhecido do
            # DescribeTags de load balancers quando 1 ARN do lote não existe
            # mais (ex.: apagado entre a Etapa 1 e esta execução):
            # `LoadBalancerNotFound` derruba a chamada toda, não só o ARN
            # ruim. Sem o retry abaixo, os outros ARNs do MESMO lote (que
            # podem existir e estar OK) virariam `revalidacao_falhou` por
            # causa de só 1 ruim, bloqueando a tentativa de escrita deles
            # também. Repete 1 ARN por vez só quando o lote falha, para
            # isolar qual ARN é o problema sem pagar esse custo no caminho
            # feliz (lote inteiro válido).
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
# Executor — único ponto de decisão entre "logar o que seria feito" (Etapa
# 2b, `DryRunExecutor`) e "fazer de verdade" (Etapa 2c, `LiveExecutor`).
# Todo o roteamento/agrupamento/revalidação acima é comum às duas.
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
    """Etapa 2b: nunca chama boto3 de escrita — só loga, no mesmo formato
    estruturado que vira o relatório de saída (preview fiel do que a Etapa
    2c executaria com os mesmos dados). Sempre devolve sucesso simulado —
    não há como o dry-run saber se uma chamada real falharia."""

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
    """Etapa 2c: chama de verdade a API nativa de cada estratégia (ver
    docstring do módulo). Implementa o mesmo `Protocol` que
    `DryRunExecutor` — nenhuma mudança no roteamento/agrupamento/
    revalidação do resto do módulo para existir."""

    def tag_generic_batch(
        self, session: boto3.Session, regiao: str, arns: list[str], tag_key: str, tag_value: str
    ) -> dict[str, ResourceOutcome]:
        client = session.client("resourcegroupstaggingapi", region_name=regiao)
        try:
            resp = _tag_resources_call(client, arns, {tag_key: tag_value})
        except ClientError as exc:
            # A chamada inteira falhou (ex.: throttling esgotou todas as
            # tentativas do retry.py) — todo o lote vira falha com o mesmo
            # erro; isso não impede o PRÓXIMO lote/região de continuar (ver
            # `_processar_genericos`, que segue lote a lote).
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
    """Aplica ao estado revalidado a MESMA regra de precedência de
    `decision._decidir` (ver docstring de `decision.py`): valor de tag
    presente sempre decide antes de olhar IaC/tag similar; IaC tem
    precedência sobre tag similar; os dois só importam quando a tag está
    ausente. Devolve o resultado já decidido (nenhum dos casos abaixo nunca
    gera tentativa de escrita), ou `None` se o recurso segue pendente de
    uma tentativa de escrita real.

    Uma leitura de revalidação que FALHOU (`_RevalidationFailure`) tem
    precedência sobre tudo — nunca prossegue para escrita, em nenhum dos
    dois modos: sem saber o estado atual, a resposta segura é não
    arriscar sobrescrever um conflito que a leitura falhou em enxergar.

    Dentro desse caso, o código do erro ainda é olhado uma vez: se for um
    dos `_CODIGOS_RECURSO_NAO_ENCONTRADO` (ex.: o recurso foi apagado entre
    a Etapa 1 e esta execução), o resultado é
    `RESULTADO_RECURSO_NAO_ENCONTRADO_NA_REVALIDACAO` em vez do genérico
    `RESULTADO_REVALIDACAO_FALHOU` — mesma distinção que já existe na
    classificação de erro de ESCRITA (`_classificar_erro_aws`): "recurso
    sumiu" é o relatório da Etapa 1 estar desatualizado (esperado
    ocasionalmente), não uma falha operacional que precise do mesmo alerta
    de um `AccessDenied`/throttling esgotado. Não reusa
    `_classificar_erro_aws` diretamente porque essa função devolve
    resultados sem entrada em `_CATEGORIA_POR_RESULTADO` (assumem
    `origem="execucao"` por padrão) — usar um resultado próprio aqui
    preserva `origem="revalidacao"`, que é a informação real de quando essa
    classificação aconteceu."""
    if isinstance(estado, _RevalidationFailure):
        codigo = (estado.detalhe_erro or {}).get("codigo", "")
        if codigo in _CODIGOS_RECURSO_NAO_ENCONTRADO:
            return ResourceOutcome(
                resultado=RESULTADO_RECURSO_NAO_ENCONTRADO_NA_REVALIDACAO, detalhe_erro=estado.detalhe_erro
            )
        return ResourceOutcome(resultado=RESULTADO_REVALIDACAO_FALHOU, detalhe_erro=estado.detalhe_erro)
    if estado.valor_atual == tag_value:
        return ResourceOutcome(resultado=RESULTADO_JA_TAGUEADO)
    if estado.valor_atual is not None:
        return ResourceOutcome(resultado=RESULTADO_CONFLITO_NA_REVALIDACAO)
    if estado.iac_tipo in _IAC_DETECTADO:
        return ResourceOutcome(resultado=RESULTADO_IAC_DETECTADO_NA_REVALIDACAO)
    if estado.tag_similar_encontrada:
        return ResourceOutcome(resultado=RESULTADO_TAG_SIMILAR_NA_REVALIDACAO)
    return None


def _resultado_entry(
    resource: TaggableResource, estrategia: ApiStrategy, resultado: str, detalhe_erro: dict | None = None
) -> dict:
    return {
        "arn": resource.arn,
        "servico": resource.servico,
        "regiao": resource.regiao,
        "tipo_recurso": resource.tipo_recurso,
        "estrategia_api": estrategia.value,
        "resultado": resultado,
        "detalhe_erro": detalhe_erro,
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
                resultados.append(_resultado_entry(r, ApiStrategy.GENERICO, outcome.resultado, outcome.detalhe_erro))
            else:
                pendentes.append(r)

        for lote in _chunk(pendentes, _TAMANHO_MAX_LOTE_GENERICO):
            arns = [r.arn for r in lote]
            resultado_por_arn = executor.tag_generic_batch(session, regiao, arns, tag_key, tag_value)
            for r in lote:
                outcome = resultado_por_arn.get(r.arn) or ResourceOutcome(resultado=RESULTADO_ERRO)
                resultados.append(_resultado_entry(r, ApiStrategy.GENERICO, outcome.resultado, outcome.detalhe_erro))
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
    # ELB_LOAD_BALANCER — agrupa a LEITURA por região em lotes de 20 (a
    # escrita continua 1-a-1; só describe_tags aceita lote).
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
                    _resultado_entry(r, estrategia, outcome_revalidacao.resultado, outcome_revalidacao.detalhe_erro)
                )
                continue
            outcome = executor.tag_single(session, estrategia, r, tag_key, tag_value)
            resultados.append(_resultado_entry(r, estrategia, outcome.resultado, outcome.detalhe_erro))
    return resultados


# ---------------------------------------------------------------------------
# Idade máxima do relatório de decisão — salvaguarda antes de agir sobre uma
# descoberta desatualizada (ver docstring do módulo)
# ---------------------------------------------------------------------------


class DecisionReportDesatualizadoError(Exception):
    """A descoberta (Etapa 1) que embasa este relatório de decisão é mais
    velha do que o limite pedido, ou o relatório não carrega essa
    informação — ação recusada até uma Etapa 1/2a novas rodarem. Nunca
    levantada quando `max_decision_age_hours` não é passado."""


class RevalidacaoObrigatoriaError(Exception):
    """`revalidate=False` não é permitido junto de `dry_run=False`: executar
    de verdade sem nenhuma revalidação do estado atual de cada recurso
    arrisca sobrescrever um conflito que tenha aparecido depois da Etapa 2a
    — a mesma coisa que a checagem de 3 vias da revalidação existe para
    evitar. Em dry-run, `revalidate=False` continua permitido (nenhuma
    escrita real está em jogo)."""


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
# Relatório final — mescla os recursos "taguear" (resultado desta execução)
# com os recursos "pular_iac"/"revisar_tag_similar"/"ja_ok"/"conflito"
# (decididos direto na Etapa 2a, nunca passam por este módulo) e os "erros"
# de classificação da Etapa 2a, num único relatório.
# ---------------------------------------------------------------------------

_CATEGORIA_POR_DECISAO_2A = {
    DECISAO_PULAR_IAC: CATEGORIA_PULADO_IAC,
    DECISAO_REVISAR_TAG_SIMILAR: CATEGORIA_REVISAR_TAG_SIMILAR,
    DECISAO_JA_OK: CATEGORIA_JA_OK,
    DECISAO_CONFLITO: CATEGORIA_CONFLITO,
}

# resultado (deste módulo) -> (categoria_final, origem) — ver docstring do
# módulo. RESULTADO_SIMULADO_OK (dry-run) e RESULTADO_TAGUEADO_SUCESSO
# (live) apontam para categorias DIFERENTES de propósito — nunca misturar
# um resultado simulado com um sucesso real na mesma contagem.
_CATEGORIA_POR_RESULTADO = {
    RESULTADO_SIMULADO_OK: (CATEGORIA_SIMULADO_SUCESSO, "execucao"),
    RESULTADO_TAGUEADO_SUCESSO: (CATEGORIA_TAGUEADO_SUCESSO, "execucao"),
    RESULTADO_JA_TAGUEADO: (CATEGORIA_JA_OK, "revalidacao"),
    RESULTADO_CONFLITO_NA_REVALIDACAO: (CATEGORIA_CONFLITO, "revalidacao"),
    RESULTADO_IAC_DETECTADO_NA_REVALIDACAO: (CATEGORIA_PULADO_IAC, "revalidacao"),
    RESULTADO_TAG_SIMILAR_NA_REVALIDACAO: (CATEGORIA_REVISAR_TAG_SIMILAR, "revalidacao"),
    RESULTADO_RECURSO_NAO_ENCONTRADO_NA_REVALIDACAO: (CATEGORIA_FALHOU, "revalidacao"),
    RESULTADO_REVALIDACAO_FALHOU: (CATEGORIA_FALHOU, "revalidacao"),
}


def _categoria_final_do_resultado(resultado: str) -> tuple[str, str]:
    """`(categoria_final, origem)` para um resultado produzido por
    `_processar_genericos`/`_processar_dedicados`. Qualquer resultado não
    mapeado explicitamente (`RESULTADO_ERRO_PERMISSAO`,
    `RESULTADO_RECURSO_NAO_ENCONTRADO`, `RESULTADO_ERRO`) é uma falha."""
    return _CATEGORIA_POR_RESULTADO.get(resultado, (CATEGORIA_FALHOU, "execucao"))


def build_execution_report(
    decision_report: dict,
    resultados_taguear: list[dict],
    expected_tag_value: str,
    dry_run: bool,
    execucao_inicial: bool = False,
) -> dict:
    """Monta o relatório final de execução (Etapa 2b em modo preview, Etapa
    2c em modo real — só `modo`/os `resultado`s individuais mudam) no mesmo
    estilo de `report.build_report`/`decision.build_decision_report`.

    Ao contrário do relatório interno de `resultados_taguear` (só recursos
    `decisao == "taguear"`), este relatório cobre TODOS os recursos do
    relatório de decisão, inclusive os que a Etapa 2a nem chegou a
    classificar: os `pular_iac`/`revisar_tag_similar`/`ja_ok`/`conflito`
    entram carregados direto (nunca passaram por este módulo, nunca
    geraram chamada nenhuma), os `erros` de classificação da Etapa 2a
    entram como `erro_classificacao`, lado a lado com o resultado real da
    tentativa de tagueamento dos `taguear`. `categoria_final` é a mesma
    taxonomia para todas as origens; `origem` (`"decisao"` vs.
    `"revalidacao"` vs. `"execucao"`) preserva de onde veio a
    classificação, para quem quiser auditar."""
    recursos_finais: list[dict] = []

    for r in resultados_taguear:
        categoria, origem = _categoria_final_do_resultado(r["resultado"])
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
                "motivo": None,
            }
        )

    for r in decision_report.get("recursos") or []:
        categoria = _CATEGORIA_POR_DECISAO_2A.get(r.get("decisao"))
        if categoria is None:
            continue  # decisao == "taguear" (já coberto acima) ou valor inesperado
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
            }
        )

    # Recursos que a Etapa 2a nem conseguiu classificar (malformados no
    # relatório da Etapa 1) — antes ficavam só em decision_report["erros"]
    # e nunca chegavam até aqui, some do relatório final sem deixar rastro.
    # Entram como categoria própria, não como "falhou" (essa é reservada
    # para falha de EXECUÇÃO, não de classificação).
    for r in decision_report.get("erros") or []:
        recursos_finais.append(
            {
                "arn": r.get("arn"),
                "servico": r.get("servico"),
                "regiao": None,
                "tipo_recurso": None,
                "categoria_final": CATEGORIA_ERRO_CLASSIFICACAO,
                "origem": "decisao",
                "estrategia_api": None,
                "resultado": None,
                "detalhe_erro": {"codigo": None, "mensagem": r.get("erro")},
                "motivo": None,
            }
        )

    categoria_counts = Counter(r["categoria_final"] for r in recursos_finais)
    servico_counts = Counter(r["servico"] for r in recursos_finais)
    # Granularidade fina (resultado/estrategia_api) só existe para os
    # recursos que passaram por este módulo — os de origem "decisao" têm
    # `resultado`/`estrategia_api` nulos e não entram nessas duas contagens.
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
    """Ponto de entrada único da Etapa 2b (`dry_run=True`, default) e da
    Etapa 2c (`dry_run=False`) — a mesma função, só trocando o `Executor`
    usado por baixo (`executor` explícito tem prioridade; senão,
    `DryRunExecutor`/`LiveExecutor` conforme `dry_run`). Nenhuma lógica de
    negócio (quais recursos recebem chamada de escrita, roteamento de API,
    batching, revalidação) muda entre os dois modos.

    `max_decision_age_hours`: quando informado, recusa agir (levanta
    `DecisionReportDesatualizadoError`) se a descoberta subjacente ao
    relatório de decisão for mais velha que isso — ver
    `decision.build_decision_report` e a docstring deste módulo.

    `execucao_inicial`: sinal de observabilidade repassado ao relatório de
    saída (`execucao_inicial` no JSON), nunca usado para bloquear a
    execução — pensado para o Lambda marcar a primeira execução de uma
    conta (disparada pelo Custom Resource no `Create` da stack) para
    facilitar revisão humana posterior, sem exigir aprovação prévia.

    Levanta `RevalidacaoObrigatoriaError` se `dry_run=False` e
    `revalidate=False` forem passados juntos — ver a docstring dessa
    exceção."""
    if not dry_run and not revalidate:
        raise RevalidacaoObrigatoriaError(
            "revalidate=False não é permitido com dry_run=False — executar de "
            "verdade sem revalidar arrisca sobrescrever um conflito que tenha "
            "aparecido depois da Etapa 2a."
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
