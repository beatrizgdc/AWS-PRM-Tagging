"""Monta o relatório final (JSON) a partir dos recursos e da árvore de OUs coletados."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone


def build_report(
    account_id: str,
    expected_tag_value: str,
    resources: list[dict],
    ou_tree: dict | None,
) -> dict:
    status_tag_counts = Counter(r["status_tag"] for r in resources)
    status_iac_counts = Counter(r["iac"]["tipo"] for r in resources)
    service_counts = Counter(r["servico"] for r in resources)

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
        },
        "recursos": resources,
        "arvore_ou": ou_tree,
    }
