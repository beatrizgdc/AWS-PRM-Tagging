"""CLI: mapeamento (somente-leitura) de recursos AWS para o AWS Partner Revenue
Measurement (PRM) — Resource Tagging.

Não cria, altera ou remove nenhum recurso ou tag. Gera um relatório JSON com
o status da tag `aws-apn-id` por recurso, heurística de IaC, e (se executado
na conta de gerenciamento de uma Organization) a árvore de OUs.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from . import ou_tree, regions, report, resource_discovery, services
from .retry import with_backoff

logger = logging.getLogger("aws_prm_tagging")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mapeamento somente-leitura de recursos AWS para o AWS PRM (Resource Tagging)."
    )
    parser.add_argument(
        "--expected-tag-value",
        required=True,
        help="Valor esperado da tag aws-apn-id, ex.: pc:xxxxxxxxxxxxxxxxxxxxxxxxx",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Perfil de credenciais AWS configurado localmente (default: default/env)",
    )
    parser.add_argument(
        "--output",
        default="prm_mapping_report.json",
        help="Caminho do arquivo JSON de saída (default: prm_mapping_report.json)",
    )
    return parser.parse_args(argv)


@with_backoff()
def _get_account_id(session: boto3.Session) -> str:
    sts = session.client("sts")
    return sts.get_caller_identity()["Account"]


def run(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    args = _parse_args(argv)

    if not args.expected_tag_value.startswith("pc:"):
        logger.warning(
            "--expected-tag-value '%s' não começa com 'pc:' (formato padrão para "
            "product code). Se for um Revenue Attribution ID, o formato esperado "
            "é 'ra-<13 caracteres>' — confirme com o guia antes de prosseguir.",
            args.expected_tag_value,
        )

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

    final_report = report.build_report(
        account_id=account_id,
        expected_tag_value=args.expected_tag_value,
        resources=all_resources,
        ou_tree=tree,
    )

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(final_report, f, indent=2, ensure_ascii=False)

    logger.info(
        "Concluído. %d recursos mapeados em %d regiões. Relatório salvo em %s",
        len(all_resources),
        total_regions,
        args.output,
    )
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
