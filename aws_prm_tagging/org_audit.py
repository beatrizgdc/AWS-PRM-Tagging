"""Etapa 4 — auditoria de SCP e tag policies do AWS Organizations.

Somente leitura. Precisa rodar com credenciais da CONTA DE GERENCIAMENTO
(ou de um administrador delegado do Organizations) — por isso é um
subcomando de CLI (`auditar-org`), não parte da stack implantada em cada
conta, e fica condicionado à aprovação do cliente
(docs/arquitetura-multicliente.md).

O que procura:

- **SCPs** com `Deny` que alcançam ações de tagueamento (`tag:TagResources`,
  `ec2:CreateTags`, `*:TagResource`...) — elas impedem a automação de
  escrever a tag na(s) conta(s)/OU(s) alvo, e a Etapa 2c/3 só veria
  `AccessDenied`. Statements com `Condition` são marcados como
  "condicional" (podem ou não bloquear — revisão humana). Também sinaliza
  SCP que restringe regiões (`aws:RequestedRegion`), que explica falhas de
  descoberta em regiões negadas.
- **Tag policies** que governam a chave `aws-apn-id`: capitalização
  diferente (tag policy fixa a capitalização da chave), lista de valores
  permitidos que não inclui o valor esperado, e `enforced_for` — que
  BLOQUEIA a operação de tagueamento não conforme nos tipos de recurso
  listados.

É uma análise heurística de texto de política, para triagem: o resultado
diz o que um humano precisa revisar, não substitui o simulador de
políticas do IAM.
"""
from __future__ import annotations

import fnmatch
import json
import logging

import boto3
from botocore.exceptions import ClientError

from .tag_status import TAG_KEY

logger = logging.getLogger(__name__)

IAM_LEITURA = sorted({"organizations:ListPolicies", "organizations:DescribePolicy", "organizations:ListTargetsForPolicy"})

# Ações representativas dos caminhos de escrita que a automação usa.
_ACOES_DE_TAG = (
    "tag:TagResources",
    "ec2:CreateTags",
    "s3:PutBucketTagging",
    "rds:AddTagsToResource",
    "elasticloadbalancing:AddTags",
    "lambda:TagResource",
    "eks:TagResource",
    "ecs:TagResource",
    "dynamodb:TagResource",
)


def _lista(v) -> list:
    return v if isinstance(v, list) else ([v] if v else [])


def _acoes_alcancadas(stmt: dict) -> list[str]:
    if "NotAction" in stmt:
        excluidas = [a.lower() for a in _lista(stmt["NotAction"])]
        return [a for a in _ACOES_DE_TAG if not any(fnmatch.fnmatch(a.lower(), e) for e in excluidas)]
    padroes = [a.lower() for a in _lista(stmt.get("Action"))]
    return [a for a in _ACOES_DE_TAG if any(fnmatch.fnmatch(a.lower(), p) for p in padroes)]


def analisar_scp(conteudo: dict) -> list[dict]:
    achados: list[dict] = []
    for i, stmt in enumerate(_lista(conteudo.get("Statement"))):
        if stmt.get("Effect") != "Deny":
            continue
        sid = stmt.get("Sid") or f"#{i}"
        cond_txt = json.dumps(stmt.get("Condition") or {}).lower()
        alcancadas = _acoes_alcancadas(stmt)
        if alcancadas:
            achados.append(
                {
                    "statement": sid,
                    "achado": "deny_condicional_em_tagueamento" if stmt.get("Condition") else "deny_em_tagueamento",
                    "acoes_alcancadas": alcancadas,
                    "menciona_aws_apn_id": TAG_KEY in json.dumps(stmt).lower(),
                    "condicao": stmt.get("Condition"),
                }
            )
        if "aws:requestedregion" in cond_txt:
            achados.append(
                {"statement": sid, "achado": "restringe_regioes", "condicao": stmt.get("Condition"),
                 "observacao": "explica AccessDenied na descoberta das regiões negadas (não é falha da automação)"}
            )
    return achados


def _valor_permitido(valor: str, permitidos: list[str]) -> bool:
    return any(p == valor or (p.endswith("*") and valor.startswith(p[:-1])) for p in permitidos)


def analisar_tag_policy(conteudo: dict, valor_esperado: str) -> list[dict]:
    achados: list[dict] = []
    for apelido, regra in (conteudo.get("tags") or {}).items():
        chave = ((regra.get("tag_key") or {}).get("@@assign")) or apelido
        if not isinstance(chave, str) or chave.lower() != TAG_KEY:
            continue
        enforced = _lista((regra.get("enforced_for") or {}).get("@@assign"))
        problemas = []
        if chave != TAG_KEY:
            problemas.append(f"capitalização da chave na policy é '{chave}' (a automação escreve '{TAG_KEY}')")
        permitidos = _lista((regra.get("tag_value") or {}).get("@@assign"))
        if permitidos and not _valor_permitido(valor_esperado, permitidos):
            problemas.append(f"valor '{valor_esperado}' fora da lista permitida {permitidos}")
        if problemas:
            achados.append(
                {
                    "regra": apelido,
                    "achado": "tag_policy_bloqueia" if enforced else "tag_policy_nao_conforme",
                    "problemas": problemas,
                    "enforced_for": enforced,
                    "observacao": (
                        "enforced_for faz a AWS RECUSAR a escrita não conforme nesses tipos de recurso"
                        if enforced
                        else "sem enforced_for: a escrita funciona, mas aparece como não conforme no relatório de tag policies"
                    ),
                }
            )
        else:
            achados.append({"regra": apelido, "achado": "tag_policy_compativel", "enforced_for": enforced})
    return achados


def _politicas(org, filtro: str) -> list[dict]:
    return [p for pag in org.get_paginator("list_policies").paginate(Filter=filtro) for p in pag.get("Policies", [])]


def _alvos(org, policy_id: str) -> list[dict]:
    return [
        {"id": t["TargetId"], "nome": t.get("Name"), "tipo": t.get("Type")}
        for pag in org.get_paginator("list_targets_for_policy").paginate(PolicyId=policy_id)
        for t in pag.get("Targets", [])
    ]


def auditar(session: boto3.Session, valor_esperado: str) -> dict:
    org = session.client("organizations", region_name="us-east-1")
    resultado: dict = {"valor_tag_esperado": valor_esperado, "scps": [], "tag_policies": [], "falhas": []}
    for filtro, chave, analisador in (
        ("SERVICE_CONTROL_POLICY", "scps", lambda c: analisar_scp(c)),
        ("TAG_POLICY", "tag_policies", lambda c: analisar_tag_policy(c, valor_esperado)),
    ):
        try:
            for p in _politicas(org, filtro):
                if p.get("AwsManaged") and filtro == "SERVICE_CONTROL_POLICY":
                    continue  # FullAWSAccess e afins (Allow) — nada a analisar
                conteudo = json.loads(org.describe_policy(PolicyId=p["Id"])["Policy"]["Content"])
                achados = analisador(conteudo)
                if achados:
                    resultado[chave].append(
                        {"id": p["Id"], "nome": p.get("Name"), "alvos": _alvos(org, p["Id"]), "achados": achados}
                    )
        except ClientError as exc:
            erro = exc.response.get("Error", {})
            resultado["falhas"].append({"tipo": filtro, "erro": f"{erro.get('Code', '')}: {erro.get('Message', '')}"})

    bloqueios = [
        a for grupo in ("scps", "tag_policies") for p in resultado[grupo] for a in p["achados"]
        if a["achado"] in ("deny_em_tagueamento", "tag_policy_bloqueia")
    ]
    resultado["resumo"] = {
        "scps_com_achados": len(resultado["scps"]),
        "tag_policies_sobre_aws_apn_id": len(resultado["tag_policies"]),
        "bloqueios_provaveis": len(bloqueios),
        "requer_revisao": bool(bloqueios) or any(
            a["achado"] in ("deny_condicional_em_tagueamento", "tag_policy_nao_conforme")
            for g in ("scps", "tag_policies") for p in resultado[g] for a in p["achados"]
        ),
    }
    return resultado
