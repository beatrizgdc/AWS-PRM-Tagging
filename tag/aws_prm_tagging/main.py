"""CLI do AWS Partner Revenue Measurement (PRM) — Resource Tagging.

Três subcomandos, um por estágio implementado até agora:

- `map`    — Etapa 1: mapeamento 100% somente-leitura da conta.
- `decide` — Etapa 2a: classifica o relatório da Etapa 1 em
  `taguear`/`pular_iac`/`ja_ok`/`conflito`. Também somente-leitura (função
  pura, sem chamada de API nenhuma).
- `apply`  — Etapa 2b: simula (dry-run) o tagueamento dos recursos
  `taguear` do relatório da Etapa 2a. Só faz chamadas de LEITURA na conta
  (revalidação do estado atual da tag, salvo com `--no-revalidate`) — nunca
  escreve. A execução real (Etapa 2c) ainda não existe; ver
  `docs/arquitetura.md#tag_executionpy`.

Cada subcomando lê a saída em disco do estágio anterior e escreve a sua
própria saída em disco — o encadeamento entre estágios é responsabilidade
de quem roda o CLI (ou de um orquestrador futuro), não deste módulo.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from . import decision, ou_tree, regions, report, resource_discovery, services, tag_execution
from .retry import with_backoff

logger = logging.getLogger("aws_prm_tagging")


@with_backoff()
def _get_account_id(session: boto3.Session) -> str:
    sts = session.client("sts")
    return sts.get_caller_identity()["Account"]


def _validate_expected_tag_value(value: str) -> None:
    if not value.startswith("pc:"):
        logger.warning(
            "--expected-tag-value '%s' não começa com 'pc:' (formato padrão para "
            "product code). Se for um Revenue Attribution ID, o formato esperado "
            "é 'ra-<13 caracteres>' — confirme com o guia antes de prosseguir.",
            value,
        )


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
    _validate_expected_tag_value(args.expected_tag_value)

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

    service_list = services.load_services()
    logger.info("%d serviços elegíveis carregados do CSV oficial", len(service_list))

    all_resources: list[dict] = []
    total_regions = len(active_regions)
    for idx, region in enumerate(active_regions, start=1):
        logger.info("Processando região %s (%d/%d)", region, idx, total_regions)

        try:
            all_resources.extend(
                resource_discovery.discover_generic_resources(
                    session, region, service_list, args.expected_tag_value
                )
            )
        except Exception:
            logger.exception(
                "Falha inesperada na descoberta genérica em %s — pulando esta etapa "
                "nesta região e continuando",
                region,
            )

        try:
            all_resources.extend(
                resource_discovery.discover_bedrock_resources(
                    session, region, service_list, args.expected_tag_value
                )
            )
        except Exception:
            logger.exception(
                "Falha inesperada na descoberta de Bedrock em %s — pulando esta etapa "
                "nesta região e continuando",
                region,
            )

        try:
            all_resources.extend(
                resource_discovery.discover_eks_resources(
                    session, region, account_id, args.expected_tag_value
                )
            )
        except Exception:
            logger.exception(
                "Falha inesperada na descoberta de EKS em %s — pulando esta etapa "
                "nesta região e continuando",
                region,
            )

        logger.info(
            "Região %s concluída. Total acumulado de recursos: %d", region, len(all_resources)
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
    )

    _write_json(args.output, final_report)

    logger.info(
        "Concluído. %d recursos mapeados em %d regiões. Relatório salvo em %s",
        len(deduped_resources),
        total_regions,
        args.output,
    )
    return 0


# ---------------------------------------------------------------------------
# decide — Etapa 2a
# ---------------------------------------------------------------------------


def _run_decide(args: argparse.Namespace) -> int:
    _validate_expected_tag_value(args.expected_tag_value)

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
# apply — Etapa 2b (dry-run)
# ---------------------------------------------------------------------------


def _run_apply(args: argparse.Namespace) -> int:
    if args.live:
        logger.error(
            "Execução real (Etapa 2c) ainda não foi implementada — esta versão do "
            "CLI só suporta o modo dry-run da Etapa 2b. Remova --live."
        )
        return 1

    try:
        decision_report = _load_json(args.input)
    except (OSError, json.JSONDecodeError):
        logger.exception("Não foi possível ler o relatório de decisão da Etapa 2a em %s", args.input)
        return 1

    expected_tag_value = decision_report.get("valor_tag_esperado")
    if not expected_tag_value:
        logger.error(
            "O relatório de decisão em %s não tem 'valor_tag_esperado' — não é um "
            "relatório válido da Etapa 2a (decision.build_decision_report).",
            args.input,
        )
        return 1

    session = boto3.Session(profile_name=args.profile)

    stage2b_report = tag_execution.run_stage2b(
        decision_report,
        session=session,
        expected_tag_value=expected_tag_value,
        revalidate=not args.no_revalidate,
    )
    _write_json(args.output, stage2b_report)

    resumo = stage2b_report["resumo"]
    logger.info(
        "Concluído (dry-run). %d recurso(s) processado(s). Por resultado: %s. "
        "Relatório salvo em %s",
        resumo["total_recursos_processados"],
        resumo["por_resultado"],
        args.output,
    )
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
        help="Valor esperado da tag aws-apn-id, ex.: pc:xxxxxxxxxxxxxxxxxxxxxxxxx",
    )
    map_parser.add_argument("--profile", default=None, help="Perfil de credenciais AWS local")
    map_parser.add_argument("--output", default="prm_mapping_report.json", help="Arquivo JSON de saída")
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
        help="Etapa 2b: simula (dry-run) o tagueamento do relatório da Etapa 2a. Nunca escreve na conta.",
    )
    apply_parser.add_argument("--input", required=True, help="Relatório JSON da Etapa 2a (saída de 'decide')")
    apply_parser.add_argument("--profile", default=None, help="Perfil de credenciais AWS local")
    apply_parser.add_argument("--output", default="prm_apply_report.json", help="Arquivo JSON de saída")
    apply_parser.add_argument(
        "--no-revalidate",
        action="store_true",
        help="Desliga a revalidação do estado atual da tag antes de simular cada recurso "
        "(por padrão, revalida — ver docs/arquitetura.md#tag_executionpy)",
    )
    apply_parser.add_argument(
        "--live",
        action="store_true",
        help="Execução real em vez de dry-run — Etapa 2c, ainda não implementada.",
    )
    apply_parser.set_defaults(func=_run_apply)

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
