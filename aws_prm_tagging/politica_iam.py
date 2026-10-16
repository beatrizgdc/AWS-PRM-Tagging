"""Política IAM para quem roda o CLI ou o modo interativo numa conta.

Fonte única: as políticas da role do tagueamento inicial em
`infra/template-hub.yaml`, que executa o mesmo pipeline (map, decide e
apply). Assim a política documentada nunca diverge do que a automação usa.
Somam-se as leituras do Organizations, que só o CLI faz (`ou_tree` e
`auditar-org`).

    python3 -m aws_prm_tagging.main politica-iam              # leitura (map, decide, dry-run)
    python3 -m aws_prm_tagging.main politica-iam --com-escrita  # + apply --live
"""
from __future__ import annotations

import json
from pathlib import Path

from .tag_status import TAG_KEY

_TEMPLATE_HUB = Path(__file__).resolve().parent / "infra" / "template-hub.yaml"
_POLITICAS_LEITURA = ("PrmTagueamentoInicialLeitura", "PrmTagueamentoInicialRevalidacao")
_POLITICAS_ESCRITA = ("PrmTagueamentoInicialEscritaGenerica", "PrmTagueamentoInicialEscritaNativa")

LEITURA_ORGANIZATIONS = (
    "organizations:DescribeOrganization",
    "organizations:DescribePolicy",
    "organizations:ListAccountsForParent",
    "organizations:ListOrganizationalUnitsForParent",
    "organizations:ListPolicies",
    "organizations:ListRoots",
    "organizations:ListTargetsForPolicy",
)


def _statements(nomes: tuple[str, ...]) -> list[dict]:
    import yaml  # só para este subcomando; as Lambdas não têm pyyaml

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda loader, sufixo, node: None)
    tpl = yaml.load(_TEMPLATE_HUB.read_text(encoding="utf-8"), Loader=_Loader)
    role = tpl["Resources"]["TagueamentoInicialRole"]["Properties"]
    return [
        st
        for pol in role["Policies"]
        if pol["PolicyName"] in nomes
        for st in pol["PolicyDocument"]["Statement"]
        if isinstance(st, dict) and st.get("Effect") == "Allow"
    ]


def _acoes(statements: list[dict]) -> set[str]:
    acoes: set[str] = set()
    for st in statements:
        acao = st["Action"]
        acoes.update(acao if isinstance(acao, list) else [acao])
    return acoes


def acoes_leitura() -> list[str]:
    return sorted(_acoes(_statements(_POLITICAS_LEITURA)) | set(LEITURA_ORGANIZATIONS))


def politica(com_escrita: bool = False) -> dict:
    statements = [{"Sid": "PrmLeitura", "Effect": "Allow", "Action": acoes_leitura(), "Resource": "*"}]
    if com_escrita:
        for i, st in enumerate(_statements(_POLITICAS_ESCRITA)):
            if not isinstance(st.get("Resource"), str):
                continue  # recursos com intrínseca do CloudFormation (só existem no template)
            acao = st["Action"]
            statements.append(
                {
                    "Sid": f"PrmEscritaTag{i}",
                    "Effect": "Allow",
                    "Action": sorted(acao) if isinstance(acao, list) else acao,
                    "Resource": st["Resource"],
                    "Condition": {"ForAllValues:StringEquals": {"aws:TagKeys": [TAG_KEY]}},
                }
            )
        statements.append(
            {
                "Sid": "PrmEscritaTagApiGateway",
                "Effect": "Allow",
                "Action": "apigateway:PUT",
                "Resource": "arn:aws:apigateway:*::/tags/*",
                "Condition": {"ForAllValues:StringEquals": {"aws:TagKeys": [TAG_KEY]}},
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def como_json(com_escrita: bool = False) -> str:
    return json.dumps(politica(com_escrita), indent=2, ensure_ascii=False)
