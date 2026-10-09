"""Descoberta de regiões comerciais ativas na conta."""
from __future__ import annotations

import logging

import boto3

from .retry import with_backoff

logger = logging.getLogger(__name__)

# PRM hoje só é suportado em regiões comerciais (não European Sovereign Cloud
# nem GovCloud/China) — conforme FAQ do guia oficial. describe_regions já só
# devolve regiões da partition da sessão, então isso é reforço, não filtro
# adicional de partition.
_ACTIVE_OPT_IN_STATUSES = {"opt-in-not-required", "opted-in"}


@with_backoff()
def _describe_regions(session: boto3.Session, base_region: str) -> list[dict]:
    ec2 = session.client("ec2", region_name=base_region)
    return ec2.describe_regions(AllRegions=True)["Regions"]


def get_active_regions(session: boto3.Session, base_region: str = "us-east-1") -> list[str]:
    """Lista as regiões comerciais habilitadas (opted-in ou opt-in-not-required)."""
    regions = _describe_regions(session, base_region)
    active = sorted(
        r["RegionName"] for r in regions if r.get("OptInStatus") in _ACTIVE_OPT_IN_STATUSES
    )
    logger.info("Regiões ativas encontradas: %d (%s)", len(active), ", ".join(active))
    return active
