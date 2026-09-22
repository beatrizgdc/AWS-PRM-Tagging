"""Monta o relatório final (JSON) a partir dos recursos e da árvore de OUs coletados."""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def dedupe_by_arn(resources: list[dict]) -> list[dict]:
    """Remove entradas duplicadas pelo mesmo ARN, mantendo a última ocorrência.

    Um mesmo recurso físico pode ser descoberto por mais de um caminho — ex.:
    uma instância EC2 que é node de um cluster EKS é retornada tanto pelo
    passo genérico (Resource Groups Tagging API, `servico="Amazon EC2"`)
    quanto pela descoberta dedicada de EKS (`servico="Amazon EKS"`,
    `tipo_recurso="node"`), já que o namespace "ec2" não está na lista de
    exclusão do passo genérico. Mantemos a última ocorrência porque
    `main.py` sempre roda a descoberta dedicada (EKS/Bedrock) depois da
    genérica — a entrada dedicada é a mais específica e correta.
    """
    deduped: dict[str, dict] = {}
    for resource in resources:
        deduped[resource["arn"]] = resource
    removed = len(resources) - len(deduped)
    if removed:
        logger.info(
            "%d entrada(s) duplicada(s) por ARN removida(s) do relatório "
            "(mesmo recurso descoberto por mais de um caminho)",
            removed,
        )
    return list(deduped.values())


def build_report(
    account_id: str,
    expected_tag_value: str,
    resources: list[dict],
    ou_tree: dict | None,
) -> dict:
    status_tag_counts = Counter(r["status_tag"] for r in resources)
    status_iac_counts = Counter(r["iac"]["tipo"] for r in resources)
    service_counts = Counter(r["servico"] for r in resources)
    total_tag_similar = sum(1 for r in resources if r.get("tag_similar_encontrada"))

    return {
        "conta_id": account_id,
        "executado_em": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "valor_tag_esperado": expected_tag_value,
        "resumo": {
            "total_recursos": len(resources),
            "por_status_tag": {
                "sem_tag": status_tag_counts.get("sem_tag", 0),
                "ok": status_tag_counts.get("ok", 0),
                "conflito": status_tag_counts.get("conflito", 0),
            },
            "por_status_iac": {
                "cloudformation": status_iac_counts.get("cloudformation", 0),
                "terraform_heuristico": status_iac_counts.get("terraform_heuristico", 0),
                "desconhecido": status_iac_counts.get("desconhecido", 0),
            },
            "por_servico": dict(sorted(service_counts.items())),
            "total_tag_similar_encontrada": total_tag_similar,
        },
        "recursos": resources,
        "arvore_ou": ou_tree,
    }
