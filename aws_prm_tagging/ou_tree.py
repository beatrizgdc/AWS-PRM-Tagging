"""Descoberta da árvore de OUs a partir da conta de gerenciamento da Organization.

Se a conta atual não for a conta de gerenciamento (ou a conta não fizer parte
de uma Organization), a descoberta é pulada com um aviso — sem quebrar a
execução do restante do script.
"""
from __future__ import annotations

import logging

import boto3
from botocore.exceptions import ClientError

from .retry import with_backoff

logger = logging.getLogger(__name__)


@with_backoff()
def _describe_organization(client) -> dict:
    return client.describe_organization()["Organization"]


@with_backoff()
def _list_roots(client) -> list[dict]:
    roots = []
    token = None
    while True:
        kwargs = {"NextToken": token} if token else {}
        resp = client.list_roots(**kwargs)
        roots.extend(resp["Roots"])
        token = resp.get("NextToken")
        if not token:
            break
    return roots


def _list_organizational_units_for_parent(client, parent_id: str) -> list[dict]:
    ous = []
    token = None
    while True:
        kwargs = {"ParentId": parent_id}
        if token:
            kwargs["NextToken"] = token
        resp = _call_with_backoff(client.list_organizational_units_for_parent, **kwargs)
        ous.extend(resp["OrganizationalUnits"])
        token = resp.get("NextToken")
        if not token:
            break
    return ous


def _list_accounts_for_parent(client, parent_id: str) -> list[dict]:
    accounts = []
    token = None
    while True:
        kwargs = {"ParentId": parent_id}
        if token:
            kwargs["NextToken"] = token
        resp = _call_with_backoff(client.list_accounts_for_parent, **kwargs)
        accounts.extend(resp["Accounts"])
        token = resp.get("NextToken")
        if not token:
            break
    return accounts


@with_backoff()
def _call_with_backoff(func, **kwargs):
    return func(**kwargs)


def _build_node(client, ou_id: str, name: str) -> dict:
    accounts = _list_accounts_for_parent(client, ou_id)
    child_ous = _list_organizational_units_for_parent(client, ou_id)
    return {
        "id": ou_id,
        "nome": name,
        "contas": [{"id": a["Id"], "nome": a["Name"]} for a in accounts],
        "ous_filhas": [_build_node(client, ou["Id"], ou["Name"]) for ou in child_ous],
    }


def discover_ou_tree(session: boto3.Session, account_id: str) -> dict | None:
    client = session.client("organizations", region_name="us-east-1")

    try:
        org = _describe_organization(client)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code == "AWSOrganizationsNotInUseException":
            logger.warning(
                "Conta %s não pertence a uma AWS Organization — pulando descoberta de OUs",
                account_id,
            )
        else:
            logger.warning(
                "Não foi possível descrever a Organization (%s) — pulando descoberta de OUs",
                error_code,
            )
        return None

    management_account_id = org["MasterAccountId"]
    if management_account_id != account_id:
        logger.warning(
            "Conta atual (%s) não é a conta de gerenciamento da Organization (%s) — "
            "pulando descoberta de árvore de OUs",
            account_id,
            management_account_id,
        )
        return None

    try:
        roots = _list_roots(client)
    except ClientError:
        logger.exception("Falha ao listar roots da Organization — pulando árvore de OUs")
        return None

    tree = {
        "roots": [_build_node(client, root["Id"], root["Name"]) for root in roots]
    }
    logger.info("Árvore de OUs descoberta com %d root(s)", len(tree["roots"]))
    return tree
