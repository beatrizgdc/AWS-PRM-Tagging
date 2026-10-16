"""Lambda que encaminha ao Zabbix as mensagens do tópico PRM.

Assinada no tópico SNS (central da organização ou de uma conta) pela stack
`infra/template-zabbix.yaml`. Ver `zabbix.py` para o mapeamento de itens.

Variáveis de ambiente:
  ZABBIX_MODO         api | trapper
  ZABBIX_URL          (api) https://zabbix.exemplo.com  — sem /api_jsonrpc.php
  ZABBIX_TOKEN_PARAM  (api) nome do parâmetro SSM SecureString com o token de API
  ZABBIX_SERVIDOR     (trapper) host do Zabbix server/proxy
  ZABBIX_PORTA        (trapper) default 10051
  ZABBIX_HOST         nome do host no Zabbix que recebe os itens (template importado nele)

Falha de envio levanta exceção: a invocação assíncrona do SNS tenta de novo
e, esgotadas as tentativas, a mensagem vai para a DLQ da stack (14 dias) —
nada se perde se o Zabbix estiver fora do ar.
"""
from __future__ import annotations

import json
import logging
import os

import boto3

from . import zabbix

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_TOKEN_CACHE: dict[str, str] = {}


def _token(ssm_client=None) -> str:
    nome = os.environ["ZABBIX_TOKEN_PARAM"]
    if nome not in _TOKEN_CACHE:
        ssm = ssm_client or boto3.client("ssm")
        _TOKEN_CACHE[nome] = ssm.get_parameter(Name=nome, WithDecryption=True)["Parameter"]["Value"]
    return _TOKEN_CACHE[nome]


def _mensagens(event: dict) -> list[dict]:
    msgs = []
    for rec in event.get("Records") or []:
        bruto = (rec.get("Sns") or {}).get("Message", "")
        try:
            msg = json.loads(bruto)
            if not isinstance(msg, dict):
                msg = {"texto": bruto}
        except ValueError:
            msg = {"texto": bruto}
        msg.setdefault("_assunto", (rec.get("Sns") or {}).get("Subject"))
        msgs.append(msg)
    return msgs


def handler(event: dict, context=None, *, ssm_client=None, enviar=None) -> dict:
    host = os.environ["ZABBIX_HOST"]
    modo = os.environ.get("ZABBIX_MODO", "api")
    valores: list[dict] = []
    contas: set[str] = set()
    for msg in _mensagens(event):
        v = zabbix.valores_da_mensagem(msg, host)
        valores += v
        contas |= {x["key"].split("[", 1)[1].rstrip("]") for x in v if "[" in x["key"]}
    if contas:
        valores.insert(0, zabbix.lld_contas(sorted(contas), host))
    if not valores:
        return {"enviados": 0}

    if enviar is None:
        if modo == "api":
            token = _token(ssm_client)
            enviar = lambda vs: zabbix.enviar_api(os.environ["ZABBIX_URL"], token, vs)  # noqa: E731
        elif modo == "trapper":
            enviar = lambda vs: zabbix.enviar_trapper(  # noqa: E731
                os.environ["ZABBIX_SERVIDOR"], int(os.environ.get("ZABBIX_PORTA", "10051")), vs
            )
        else:
            raise ValueError(f"ZABBIX_MODO inválido: {modo!r} (api | trapper)")
    enviar(valores)
    logger.info("Zabbix: %d valor(es) enviados (%d conta(s))", len(valores), len(contas))
    return {"enviados": len(valores), "contas": sorted(contas)}
