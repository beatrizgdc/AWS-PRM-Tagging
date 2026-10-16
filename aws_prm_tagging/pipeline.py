"""Pipeline Etapa 1 -> 2a -> 2b/2c como funções reutilizáveis.

Usado pelo CLI (`main.py map`) e pela Lambda do tagueamento inicial (item 8,
`handler_initial_tagging.py`) — uma única implementação da descoberta, para
os dois caminhos nunca divergirem.
"""
from __future__ import annotations

import logging
from collections import Counter

import boto3

from . import cloudtrail_iac, decision, native_discovery, relatorio_cliente, report, resource_discovery, tag_execution
from .services import Service

# Itens de IaC / estado anterior por região no resumo SNS (limite de 256 KB).
_AMOSTRA_CLIENTE = 25

logger = logging.getLogger(__name__)


ETAPA_REGIAO_NEGADA_SCP = "regiao_negada_por_scp"


def _negado_por_scp(falha: dict) -> bool:
    erro = (falha.get("erro") or "").lower()
    return "accessdenied" in erro and "service control policy" in erro


def descobrir_regioes(
    session: boto3.Session,
    account_id: str,
    regioes: list[str],
    service_list: list[Service],
    expected_tag_value: str,
    descoberta_nativa: bool = True,
    historico_cloudtrail: bool = True,
    orcamento_cloudtrail_s: float | None = None,
) -> tuple[list[dict], list[dict]]:
    """Etapa 1 para as regiões dadas. Devolve `(recursos, falhas_descoberta)`
    — recursos já com a descoberta nativa mesclada (só o que o passo
    genérico não viu), ainda SEM deduplicação final (feita pelo chamador
    com `report.dedupe_by_arn`)."""
    todos: list[dict] = []
    nativos: list[dict] = []
    falhas: list[dict] = []

    etapas = (
        ("generico", lambda r: resource_discovery.discover_generic_resources(session, r, service_list, expected_tag_value)),
        ("bedrock", lambda r: resource_discovery.discover_bedrock_resources(session, r, service_list, expected_tag_value)),
        ("eks", lambda r: resource_discovery.discover_eks_resources(session, r, account_id, expected_tag_value)),
    )
    regioes_negadas_scp: set[str] = set()
    for idx, regiao in enumerate(regioes, start=1):
        logger.info("Processando região %s (%d/%d)", regiao, idx, len(regioes))
        for nome, fn in etapas:
            try:
                recursos, f = fn(regiao)
            except Exception as exc:  # noqa: BLE001 — backstop por etapa (ClientError já vem em `f`)
                logger.exception("Falha inesperada na descoberta %s em %s — continuando", nome, regiao)
                recursos, f = [], [{"regiao": regiao, "etapa": nome, "erro": str(exc)}]
            if nome == "generico" and any(_negado_por_scp(x) for x in f):
                # Região inteira bloqueada por SCP (ex.: restrição de
                # regiões da organização): UMA entrada, e nada mais é
                # tentado nela — as outras etapas falhariam pela mesma
                # causa (visto na sandbox: 4 falhas idênticas por região).
                regioes_negadas_scp.add(regiao)
                falhas.append({"regiao": regiao, "etapa": ETAPA_REGIAO_NEGADA_SCP, "erro": f[0]["erro"]})
                break
            todos.extend(recursos)
            falhas.extend(f)
        if regiao in regioes_negadas_scp:
            logger.warning("Região %s bloqueada por SCP — pulada", regiao)
            continue

        # Item 1: pulado quando o GetResources da região foi negado
        # (tipicamente SCP de restrição de região — mesma causa derrubaria
        # os ~50 enumeradores).
        negado = any(
            f["regiao"] == regiao and f["etapa"] == "generico" and "AccessDenied" in f["erro"] for f in falhas
        )
        if descoberta_nativa and not negado:
            try:
                recursos, f = native_discovery.discover_native_resources(
                    session, regiao, account_id, service_list, expected_tag_value, incluir_s3=False
                )
                nativos.extend(recursos)
                falhas.extend(f)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Falha inesperada na descoberta nativa em %s — continuando", regiao)
                falhas.append({"regiao": regiao, "etapa": "nativo", "erro": str(exc)})
        logger.info("Região %s concluída. Total acumulado: %d", regiao, len(todos))

    # S3: sempre pela região REAL de cada bucket, só para as regiões do
    # escopo que não foram bloqueadas por SCP — cada região trata os
    # próprios buckets (sem duplicar entre execuções por região).
    regioes_s3 = set(regioes) - regioes_negadas_scp
    if descoberta_nativa and regioes_s3:
        recursos, f = native_discovery.discover_s3_buckets_por_regiao(
            session, regioes_s3, account_id, service_list, expected_tag_value
        )
        nativos.extend(recursos)
        falhas.extend(f)

    # Só depois de TODAS as regiões pedidas: bucket S3/serviço global pode
    # ter aparecido no passo genérico de outra região.
    novos = native_discovery.merge_native_resources(todos, nativos)
    if novos:
        logger.info("Descoberta nativa: %d recurso(s) sem nenhuma tag acrescentados", len(novos))
    todos.extend(novos)

    # MVP2: Terraform/OpenTofu/Pulumi quase nunca deixam tag — o histórico
    # do CloudTrail (userAgent das chamadas de escrita) pega os recursos
    # que a heurística de tags perde. Só para candidatos a tagueamento.
    if historico_cloudtrail and todos:
        stats = cloudtrail_iac.enriquecer_iac(session, todos, orcamento_cloudtrail_s)
        falhas.extend(stats["falhas"])
    return todos, falhas


def executar_regiao(
    session: boto3.Session,
    account_id: str,
    regiao: str,
    service_list: list[Service],
    expected_tag_value: str,
    live: bool,
    execucao_inicial: bool = False,
    orcamento_cloudtrail_s: float | None = 300,
) -> dict:
    """Etapa 1 -> 2a -> 2b (live=False) ou 2c (live=True) para UMA região.
    Devolve um resumo pequeno (cabe numa mensagem SNS), não o relatório
    completo.

    S3: cada região trata só os buckets cuja região real é ela
    (`discover_s3_buckets_por_regiao`). CloudFront/Route 53 (globais) são
    tratados na execução de us-east-1."""
    # Orçamento de 300 s no histórico do CloudTrail: a Lambda tem 900 s.
    recursos, falhas = descobrir_regioes(
        session, account_id, [regiao], service_list, expected_tag_value, orcamento_cloudtrail_s=orcamento_cloudtrail_s
    )
    etapa1 = report.build_report(
        account_id=account_id,
        expected_tag_value=expected_tag_value,
        resources=report.dedupe_by_arn(recursos),
        ou_tree=None,
        falhas_descoberta=falhas,
    )
    decisao = decision.build_decision_report(etapa1, expected_tag_value)
    execucao = tag_execution.run_tagging_execution(
        decisao,
        session=session,
        expected_tag_value=expected_tag_value,
        dry_run=not live,
        revalidate=True,
        execucao_inicial=execucao_inicial,
    )
    categorias = Counter(r["categoria_final"] for r in execucao.get("recursos", []))
    falhas_escrita = [
        {"arn": r["arn"], "detalhe_erro": r.get("detalhe_erro")}
        for r in execucao.get("recursos", [])
        if r["categoria_final"] == "falhou"
    ]
    rel_cliente = relatorio_cliente.build_relatorio_cliente(execucao)
    return {
        "conta_id": account_id,
        "regiao": regiao,
        "modo": "live" if live else "dry-run",
        "total_recursos": len(execucao.get("recursos", [])),
        "por_categoria": dict(sorted(categorias.items())),
        "total_descobertos_so_via_api_nativa": etapa1["resumo"].get("total_descobertos_so_via_api_nativa", 0),
        "total_falhas_descoberta": len(falhas),
        # Amostra limitada: o resumo vai para o SNS (limite de 256 KB).
        "falhas_descoberta_amostra": falhas[:20],
        "falhas_escrita_amostra": falhas_escrita[:20],
        "total_falhas_escrita": len(falhas_escrita),
        # Região inteira negada por SCP: estado conhecido da organização, não
        # novidade (a varredura recorrente não publica por isso).
        "regiao_bloqueada_por_scp": any(f.get("etapa") == ETAPA_REGIAO_NEGADA_SCP for f in falhas),
        # MVP2: relatório do cliente resumido (lista completa: CLI 'apply' /
        # 'iniciar', que grava o relatório inteiro em arquivo).
        "por_acao": execucao.get("resumo", {}).get("por_acao", {}),
        "cliente_resumo": rel_cliente["resumo"],
        "iac_amostra": [
            {k: i[k] for k in ("arn", "ferramenta", "stack_cloudformation", "evidencia", "valor_atual_aws_apn_id")}
            for i in rel_cliente["recursos_iac"][:_AMOSTRA_CLIENTE]
        ],
        "estado_anterior_amostra": [
            {k: a[k] for k in ("arn", "acao", "valor_anterior_aws_apn_id", "chaves_similares_anteriores")}
            for a in rel_cliente["estado_anterior"][:_AMOSTRA_CLIENTE]
        ],
    }
