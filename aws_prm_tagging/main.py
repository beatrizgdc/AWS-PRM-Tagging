"""CLI do AWS Partner Revenue Measurement (PRM) — Resource Tagging.

Três subcomandos, um por estágio implementado até agora:

- `map`    — Etapa 1: mapeamento 100% somente-leitura da conta.
- `decide` — Etapa 2a: classifica o relatório da Etapa 1 em
  `taguear`/`pular_iac`/`ja_ok`/`conflito`. Também somente-leitura (função
  pura, sem chamada de API nenhuma).
- `apply`  — Etapas 2b (default, dry-run) e 2c (`--live`, escrita real) do
  tagueamento dos recursos `taguear` do relatório da Etapa 2a. Em dry-run,
  só faz chamadas de LEITURA na conta (revalidação do estado atual de cada
  recurso, salvo com `--no-revalidate`) — nunca escreve. Ver
  `docs/arquitetura.md#tag_executionpy`.

Cada subcomando lê a saída em disco do estágio anterior e escreve a sua
própria saída em disco — o encadeamento entre estágios é responsabilidade
de quem roda o CLI (ou de um orquestrador futuro), não deste módulo.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from . import csv_check, decision, org_audit, ou_tree, pipeline, preflight, recomendacoes, regions, report, services, tag_execution
from .retry import with_backoff

logger = logging.getLogger("aws_prm_tagging")

# Formato já confirmado e fechado para este projeto: "pc:<product-code>",
# só letras/números depois do prefixo. Não é um formato genérico de tag da
# AWS — é a decisão de escopo específica deste projeto (nunca "ra-...").
_EXPECTED_TAG_VALUE_PATTERN = re.compile(r"^pc:[A-Za-z0-9]+$")


@with_backoff()
def _get_account_id(session: boto3.Session) -> str:
    sts = session.client("sts")
    return sts.get_caller_identity()["Account"]


def _validate_expected_tag_value(value: str) -> bool:
    """Erro de verdade (não só aviso) — o formato já está fechado para este
    projeto, então um valor fora do padrão quase certamente é engano de
    quem digitou o comando, não uma variação válida a aceitar."""
    if not _EXPECTED_TAG_VALUE_PATTERN.match(value):
        logger.error(
            "--expected-tag-value '%s' inválido — o formato deste projeto é "
            "'pc:<product-code>' (ex.: pc:5ugbbrmu7ud3u5hsipfzug61p), só "
            "letras/números depois de 'pc:'.",
            value,
        )
        return False
    return True


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# map — Etapa 1
# ---------------------------------------------------------------------------


def _run_map(args: argparse.Namespace) -> int:
    if not _validate_expected_tag_value(args.expected_tag_value):
        return 1

    session = boto3.Session(profile_name=args.profile)

    try:
        account_id = _get_account_id(session)
    except (ClientError, BotoCoreError):
        logger.exception(
            "Não foi possível obter a identidade da conta AWS (sts:GetCallerIdentity). "
            "Verifique o perfil/credenciais informados."
        )
        return 1

    logger.info("Conta AWS alvo: %s", account_id)

    try:
        active_regions = regions.get_active_regions(session)
    except (ClientError, BotoCoreError):
        logger.exception("Não foi possível listar as regiões ativas da conta. Abortando.")
        return 1
    if args.regioes:
        pedidas = [r.strip() for r in args.regioes.split(",") if r.strip()]
        inativas = sorted(set(pedidas) - set(active_regions))
        if inativas:
            logger.error("--regioes inclui região(ões) não ativa(s) nesta conta: %s", ", ".join(inativas))
            return 1
        active_regions = [r for r in active_regions if r in pedidas]
        logger.info("Varredura restrita por --regioes: %s", ", ".join(active_regions))

    service_list = services.load_services()
    logger.info("%d serviços elegíveis carregados do CSV oficial", len(service_list))

    # Loop por região extraído para pipeline.py (Lote 3) — a Lambda do
    # tagueamento inicial (item 8) usa exatamente a mesma função.
    all_resources, falhas_descoberta = pipeline.descobrir_regioes(
        session,
        account_id,
        active_regions,
        service_list,
        args.expected_tag_value,
        descoberta_nativa=args.descoberta_nativa,
    )

    try:
        tree = ou_tree.discover_ou_tree(session, account_id)
    except Exception:
        logger.exception(
            "Falha inesperada na descoberta da árvore de OUs — prosseguindo sem ela"
        )
        tree = None

    deduped_resources = report.dedupe_by_arn(all_resources)

    final_report = report.build_report(
        account_id=account_id,
        expected_tag_value=args.expected_tag_value,
        resources=deduped_resources,
        ou_tree=tree,
        falhas_descoberta=falhas_descoberta,
    )

    _write_json(args.output, final_report)

    if falhas_descoberta:
        logger.warning(
            "%d falha(s) de descoberta registrada(s) no relatório (falhas_descoberta) — "
            "o total de recursos abaixo pode estar incompleto para as regiões/etapas afetadas.",
            len(falhas_descoberta),
        )

    logger.info(
        "Concluído. %d recursos mapeados em %d regiões. Relatório salvo em %s",
        len(deduped_resources),
        len(active_regions),
        args.output,
    )
    return 0


# ---------------------------------------------------------------------------
# decide — Etapa 2a
# ---------------------------------------------------------------------------


def _run_decide(args: argparse.Namespace) -> int:
    if not _validate_expected_tag_value(args.expected_tag_value):
        return 1

    try:
        etapa1_report = _load_json(args.input)
    except (OSError, json.JSONDecodeError):
        logger.exception("Não foi possível ler o relatório da Etapa 1 em %s", args.input)
        return 1

    decision_report = decision.build_decision_report(etapa1_report, args.expected_tag_value)
    _write_json(args.output, decision_report)

    resumo = decision_report["resumo"]
    logger.info(
        "Concluído. %d recurso(s) avaliado(s) (%d erro(s), %d fora de escopo). "
        "Por decisão: %s. Relatório salvo em %s",
        resumo["total_recursos_avaliados"],
        resumo["total_erros"],
        resumo["total_excluidos_fora_de_escopo"],
        resumo["por_decisao"],
        args.output,
    )
    return 0


# ---------------------------------------------------------------------------
# apply — Etapa 2b (dry-run, default) e Etapa 2c (--live)
# ---------------------------------------------------------------------------


def _run_apply(args: argparse.Namespace) -> int:
    if args.live and args.no_revalidate:
        logger.error(
            "--live não pode ser combinado com --no-revalidate: executar de verdade "
            "sem revalidar o estado atual de cada recurso arrisca sobrescrever um "
            "conflito que tenha aparecido depois da Etapa 2a. Rode sem --no-revalidate."
        )
        return 1

    try:
        decision_report = _load_json(args.input)
    except (OSError, json.JSONDecodeError):
        logger.exception("Não foi possível ler o relatório de decisão da Etapa 2a em %s", args.input)
        return 1

    if args.somente_arns:
        try:
            with open(args.somente_arns, encoding="utf-8") as f:
                permitidos = {linha.split("\t", 1)[0].strip() for linha in f if linha.strip() and not linha.startswith("#")}
        except OSError:
            logger.exception("Não foi possível ler --somente-arns %s", args.somente_arns)
            return 1
        antes = len(decision_report.get("recursos") or [])
        decision_report = {
            **decision_report,
            "recursos": [r for r in decision_report.get("recursos") or [] if r.get("arn") in permitidos],
            "erros": [r for r in decision_report.get("erros") or [] if r.get("arn") in permitidos],
        }
        logger.warning(
            "--somente-arns: agindo sobre %d de %d recurso(s) do relatório de decisão; os demais são ignorados "
            "(nenhuma leitura ou escrita neles).",
            len(decision_report["recursos"]),
            antes,
        )

    expected_tag_value = decision_report.get("valor_tag_esperado")
    if not expected_tag_value:
        logger.error(
            "O relatório de decisão em %s não tem 'valor_tag_esperado' — não é um "
            "relatório válido da Etapa 2a (decision.build_decision_report).",
            args.input,
        )
        return 1

    session = boto3.Session(profile_name=args.profile)

    try:
        execution_report = tag_execution.run_tagging_execution(
            decision_report,
            session=session,
            expected_tag_value=expected_tag_value,
            dry_run=not args.live,
            revalidate=not args.no_revalidate,
            max_decision_age_hours=args.max_decision_age_hours,
        )
    except (tag_execution.DecisionReportDesatualizadoError, tag_execution.RevalidacaoObrigatoriaError) as exc:
        logger.error("%s", exc)
        return 1

    _write_json(args.output, execution_report)

    resumo = execution_report["resumo"]
    logger.info(
        "Concluído (%s). %d recurso(s) no relatório. Por categoria: %s. Relatório salvo em %s",
        execution_report["modo"],
        resumo["total_recursos"],
        resumo["por_categoria_final"],
        args.output,
    )
    return 0


# ---------------------------------------------------------------------------
# preflight — checagem antes do deploy da Etapa 3 (somente leitura)
# ---------------------------------------------------------------------------


def _run_preflight(args: argparse.Namespace) -> int:
    session = boto3.Session(profile_name=args.profile)
    try:
        resultado = preflight.run_preflight(session)
    except (ClientError, BotoCoreError):
        logger.exception("Falha no preflight (permissões de leitura de EC2/CloudTrail?)")
        return 1
    _write_json(args.output, resultado)
    logger.info("Regiões para o StackSet regional: %s", ",".join(resultado["regioes_stackset"]))
    for rec in resultado["recomendacoes"]:
        logger.warning("%s", rec)
    # Código 2 = não bloqueante, mas exige atenção (sem trail em alguma região).
    return 0 if resultado["pronto_para_deploy"] else 2


# ---------------------------------------------------------------------------
# recomendar — tag na origem (itens 12 e 14), somente leitura
# ---------------------------------------------------------------------------


def _run_recomendar(args: argparse.Namespace) -> int:
    session = boto3.Session(profile_name=args.profile)
    try:
        conta_id = session.client("sts").get_caller_identity()["Account"]
        ativas = regions.get_active_regions(session)
    except (ClientError, BotoCoreError):
        logger.exception("Falha ao identificar conta/regiões")
        return 1
    alvo = [r.strip() for r in args.regioes.split(",")] if args.regioes else ativas
    recs, falhas = recomendacoes.gerar_recomendacoes(session, alvo, args.expected_tag_value)
    _write_json(
        args.output,
        {"conta_id": conta_id, "valor_tag_esperado": args.expected_tag_value, "total": len(recs), "recomendacoes": recs, "falhas": falhas},
    )
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write(recomendacoes.para_markdown(recs, conta_id, args.expected_tag_value))
    logger.info("%d recomendação(ões) gerada(s); %d falha(s) de leitura", len(recs), len(falhas))
    return 0


# ---------------------------------------------------------------------------
# Etapa 4 — auditar-org e verificar-csv (somente leitura)
# ---------------------------------------------------------------------------


def _run_auditar_org(args: argparse.Namespace) -> int:
    session = boto3.Session(profile_name=args.profile)
    resultado = org_audit.auditar(session, args.expected_tag_value)
    _write_json(args.output, resultado)
    r = resultado["resumo"]
    logger.info(
        "SCPs com achados: %d | tag policies sobre aws-apn-id: %d | bloqueios prováveis: %d",
        r["scps_com_achados"], r["tag_policies_sobre_aws_apn_id"], r["bloqueios_provaveis"],
    )
    for f in resultado["falhas"]:
        logger.warning("Falha ao ler %s: %s", f["tipo"], f["erro"])
    return 2 if r["requer_revisao"] else 0


def _run_verificar_csv(args: argparse.Namespace) -> int:
    try:
        html = csv_check.baixar(args.url)
        pagina = csv_check.extrair_servicos_da_pagina(html)
    except Exception:
        logger.exception("Não foi possível ler a tabela oficial em %s", args.url)
        return 1
    dif = csv_check.comparar(pagina)
    _write_json(args.output, dif)
    if dif["tem_diferenca"]:
        logger.warning(
            "Lista oficial mudou: %d novo(s), %d removido(s), %d código(s) e %d nota(s) alterados. "
            "Atualizar data/ e reference/ (ver reference/FONTE-CSV.md).",
            len(dif["novos_na_pagina"]), len(dif["removidos_da_pagina"]),
            len(dif["codigo_alterado"]), len(dif["nota_alterada"]),
        )
        return 3
    logger.info("CSV local igual à página oficial (%d serviços).", dif["total_pagina"])
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aws_prm_tagging",
        description="AWS Partner Revenue Measurement (PRM) — Resource Tagging.",
    )
    subparsers = parser.add_subparsers(dest="comando", required=True)

    map_parser = subparsers.add_parser(
        "map", help="Etapa 1: mapeamento somente-leitura da conta."
    )
    map_parser.add_argument(
        "--expected-tag-value",
        required=True,
        help="Valor esperado da tag aws-apn-id, ex.: pc:5ugbbrmu7ud3u5hsipfzug61p",
    )
    map_parser.add_argument("--profile", default=None, help="Perfil de credenciais AWS local")
    map_parser.add_argument("--output", default="prm_mapping_report.json", help="Arquivo JSON de saída")
    map_parser.add_argument(
        "--regioes",
        default=None,
        help="Lista separada por vírgula (ex.: us-east-1,sa-east-1) para restringir a varredura. "
        "Default: todas as regiões ativas.",
    )
    map_parser.add_argument(
        "--sem-descoberta-nativa",
        dest="descoberta_nativa",
        action="store_false",
        help="Desliga a descoberta pelas APIs nativas de cada serviço (recursos que nunca "
        "tiveram tag). Ligada por padrão — ver native_discovery.py.",
    )
    map_parser.set_defaults(func=_run_map)

    decide_parser = subparsers.add_parser(
        "decide", help="Etapa 2a: classifica o relatório da Etapa 1 (sem chamada de API)."
    )
    decide_parser.add_argument("--input", required=True, help="Relatório JSON da Etapa 1 (saída de 'map')")
    decide_parser.add_argument(
        "--expected-tag-value",
        required=True,
        help="Valor esperado da tag aws-apn-id para esta OU/contrato",
    )
    decide_parser.add_argument("--output", default="prm_decision_report.json", help="Arquivo JSON de saída")
    decide_parser.set_defaults(func=_run_decide)

    apply_parser = subparsers.add_parser(
        "apply",
        help="Etapa 2b (dry-run, default) ou 2c (--live) do tagueamento do relatório da Etapa 2a.",
    )
    apply_parser.add_argument("--input", required=True, help="Relatório JSON da Etapa 2a (saída de 'decide')")
    apply_parser.add_argument("--profile", default=None, help="Perfil de credenciais AWS local")
    apply_parser.add_argument("--output", default="prm_apply_report.json", help="Arquivo JSON de saída")
    apply_parser.add_argument(
        "--no-revalidate",
        action="store_true",
        help="Desliga a revalidação do estado atual de cada recurso antes de agir sobre ele "
        "(por padrão, revalida — ver docs/arquitetura.md#tag_executionpy)",
    )
    apply_parser.add_argument(
        "--live",
        action="store_true",
        help="Execução real (Etapa 2c) — escreve a tag de verdade na conta. Por padrão roda em "
        "dry-run (Etapa 2b), sem escrever nada.",
    )
    apply_parser.add_argument(
        "--somente-arns",
        default=None,
        help="Arquivo com um ARN por linha (primeira coluna, separada por TAB; linhas com # são "
        "ignoradas). Só esses recursos são processados — essencial em conta compartilhada "
        "(sandbox) e útil para rollout gradual.",
    )
    apply_parser.add_argument(
        "--max-decision-age-hours",
        type=float,
        default=None,
        help="Recusa agir se a descoberta (Etapa 1) que embasa o relatório de decisão for mais "
        "velha que este limite, em horas (default: sem checagem)",
    )
    apply_parser.set_defaults(func=_run_apply)

    rec_parser = subparsers.add_parser(
        "recomendar",
        help="Somente leitura: gera recomendações de tag na origem (EKS, ECS, DRS, IoT) para o cliente aplicar no IaC.",
    )
    rec_parser.add_argument("--profile", default=None)
    rec_parser.add_argument("--expected-tag-value", required=True, help="Valor esperado da tag aws-apn-id")
    rec_parser.add_argument("--regioes", default=None, help="Default: todas as regiões ativas.")
    rec_parser.add_argument("--output", default="prm_recomendacoes.json")
    rec_parser.add_argument("--markdown", default=None, help="Também grava um .md legível para enviar ao cliente.")
    rec_parser.set_defaults(func=_run_recomendar)

    org_parser = subparsers.add_parser(
        "auditar-org",
        help="Etapa 4, somente leitura, com credencial da CONTA DE GERENCIAMENTO: SCPs e tag policies que "
        "afetam aws-apn-id. Saída 2 = requer revisão humana.",
    )
    org_parser.add_argument("--profile", default=None)
    org_parser.add_argument("--expected-tag-value", required=True, help="Valor esperado da tag aws-apn-id")
    org_parser.add_argument("--output", default="prm_auditoria_org.json")
    org_parser.set_defaults(func=_run_auditar_org)

    csv_parser = subparsers.add_parser(
        "verificar-csv",
        help="Etapa 4: compara o CSV empacotado com a tabela oficial da página do guia. Saída 3 = diferença.",
    )
    csv_parser.add_argument("--url", default=csv_check.URL_PADRAO)
    csv_parser.add_argument("--output", default="prm_verificacao_csv.json")
    csv_parser.set_defaults(func=_run_verificar_csv)

    preflight_parser = subparsers.add_parser(
        "preflight",
        help="Antes do deploy da Etapa 3: verifica trail do CloudTrail por região e lista as regiões do StackSet.",
    )
    preflight_parser.add_argument("--profile", default=None, help="Perfil de credenciais AWS local")
    preflight_parser.add_argument("--output", default="prm_preflight.json", help="Arquivo JSON de saída")
    preflight_parser.set_defaults(func=_run_preflight)

    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    return args.func(args)


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
