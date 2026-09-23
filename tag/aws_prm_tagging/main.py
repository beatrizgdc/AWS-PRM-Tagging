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

from . import decision, ou_tree, regions, report, resource_discovery, services, tag_execution
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
            "'pc:<product-code>' (ex.: pc:xxxxxxxxxxxxxxxxxxxxxxxxx), só "
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

    service_list = services.load_services()
    logger.info("%d serviços elegíveis carregados do CSV oficial", len(service_list))

    all_resources: list[dict] = []
    falhas_descoberta: list[dict] = []
    total_regions = len(active_regions)
    for idx, region in enumerate(active_regions, start=1):
        logger.info("Processando região %s (%d/%d)", region, idx, total_regions)

        try:
            recursos, falhas = resource_discovery.discover_generic_resources(
                session, region, service_list, args.expected_tag_value
            )
            all_resources.extend(recursos)
            falhas_descoberta.extend(falhas)
        except Exception as exc:
            # Backstop para falha inesperada (bug de programação) — as
            # falhas de API já esperadas (ClientError) são capturadas
            # dentro de resource_discovery.py e voltam na lista `falhas`
            # acima, não chegam a levantar exceção até aqui.
            logger.exception(
                "Falha inesperada na descoberta genérica em %s — pulando esta etapa "
                "nesta região e continuando",
                region,
            )
            falhas_descoberta.append({"regiao": region, "etapa": "generico", "erro": str(exc)})

        try:
            recursos, falhas = resource_discovery.discover_bedrock_resources(
                session, region, service_list, args.expected_tag_value
            )
            all_resources.extend(recursos)
            falhas_descoberta.extend(falhas)
        except Exception as exc:
            logger.exception(
                "Falha inesperada na descoberta de Bedrock em %s — pulando esta etapa "
                "nesta região e continuando",
                region,
            )
            falhas_descoberta.append({"regiao": region, "etapa": "bedrock", "erro": str(exc)})

        try:
            recursos, falhas = resource_discovery.discover_eks_resources(
                session, region, account_id, args.expected_tag_value
            )
            all_resources.extend(recursos)
            falhas_descoberta.extend(falhas)
        except Exception as exc:
            logger.exception(
                "Falha inesperada na descoberta de EKS em %s — pulando esta etapa "
                "nesta região e continuando",
                region,
            )
            falhas_descoberta.append({"regiao": region, "etapa": "eks", "erro": str(exc)})

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
        total_regions,
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
        "--max-decision-age-hours",
        type=float,
        default=None,
        help="Recusa agir se a descoberta (Etapa 1) que embasa o relatório de decisão for mais "
        "velha que este limite, em horas (default: sem checagem)",
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
