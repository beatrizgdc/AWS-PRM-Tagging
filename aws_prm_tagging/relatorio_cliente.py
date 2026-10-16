"""Relatório final para o cliente (MVP2).

Gerado a partir do relatório de execução (`tag_execution.build_execution_report`,
saída de `apply`). Duas listas, pedidas pelo DevOps em 13/10/2026:

1. **Recursos gerenciados por IaC** (Terraform/OpenTofu, CloudFormation, CDK,
   Pulumi): a automação não tagueia esses recursos. O cliente precisa
   adicionar `aws-apn-id` no próprio código. A lista traz a ferramenta
   provável, a evidência da detecção, o valor atual (se houver) e um
   exemplo de como aplicar a tag em cada ferramenta.
2. **Estado anterior**: recursos em que a automação sobrescreveu o valor de
   outro parceiro ou adicionou `aws-apn-id` ao lado de uma chave de grafia
   parecida (ex.: `AWS-APN-ID`). Fica documentado como estava antes.

Módulo puro, sem I/O: devolve dict/strings; quem grava em disco é `main.py`.
Três formatos: JSON (máquina), CSV (planilha) e Markdown (leitura/envio).
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import datetime, timezone

from .tag_status import TAG_KEY

_CATEGORIAS_SUCESSO = ("tagueado_sucesso", "simulado_sucesso")


def _exemplos_iac(valor: str) -> dict[str, str]:
    """Como aplicar a tag em cada ferramenta — o caminho de menor esforço é
    a tag padrão do provider/app, que cobre todos os recursos de uma vez."""
    return {
        "Terraform": (
            'provider "aws" {\n'
            "  default_tags {\n"
            f'    tags = {{ "{TAG_KEY}" = "{valor}" }}\n'
            "  }\n"
            "}"
        ),
        "OpenTofu": (
            'provider "aws" {\n'
            "  default_tags {\n"
            f'    tags = {{ "{TAG_KEY}" = "{valor}" }}\n'
            "  }\n"
            "}"
        ),
        "CloudFormation": (
            "Tags no nível da stack (propagadas aos recursos que suportam tag):\n"
            "aws cloudformation update-stack --stack-name <stack> --use-previous-template \\\n"
            f"  --tags Key={TAG_KEY},Value={valor} [demais tags atuais da stack] \\\n"
            "  [--parameters ParameterKey=<p>,UsePreviousValue=true ...] [--capabilities ...]\n"
            "(update-stack SUBSTITUI a lista de tags da stack: repita as tags existentes.)"
        ),
        "AWS CDK": f"Tags.of(app).add('{TAG_KEY}', '{valor}');  // e cdk deploy",
        "Pulumi": (
            "new aws.Provider('aws', { defaultTags: { tags: { "
            f"'{TAG_KEY}': '{valor}' }} }} }});\n"
            f"# ou: pulumi config set --path 'aws:defaultTags.tags.{TAG_KEY}' '{valor}'"
        ),
    }


def _ferramenta_exibicao(iac: dict | None) -> str:
    iac = iac or {}
    if iac.get("gerenciado_por_ferramenta_aws"):
        return iac["gerenciado_por_ferramenta_aws"]
    ferramenta = iac.get("ferramenta")
    if ferramenta:
        return ferramenta
    return {
        "cloudformation": "CloudFormation/CDK",
        "terraform_heuristico": "Terraform",
        "pulumi_heuristico": "Pulumi",
    }.get(iac.get("tipo") or "", "IaC (não identificado)")


def _chave_exemplo(ferramenta: str) -> str | None:
    if ferramenta in ("CloudFormation/CDK", "CloudFormation"):
        return "CloudFormation"
    if ferramenta in ("Terraform", "OpenTofu", "AWS CDK", "Pulumi"):
        return ferramenta
    return None


def build_relatorio_cliente(execution_report: dict) -> dict:
    valor = execution_report.get("valor_tag_esperado") or "pc:<product-code>"
    recursos = execution_report.get("recursos") or []

    iac: list[dict] = []
    alterados: list[dict] = []
    for r in recursos:
        anterior = r.get("estado_anterior") or {}
        if r.get("categoria_final") == "pulado_iac":
            ferramenta = _ferramenta_exibicao(r.get("iac"))
            iac.append(
                {
                    "arn": r.get("arn"),
                    "servico": r.get("servico"),
                    "regiao": r.get("regiao"),
                    "ferramenta": ferramenta,
                    "stack_cloudformation": (r.get("iac") or {}).get("stack_name"),
                    "evidencia": (r.get("iac") or {}).get("evidencia"),
                    "valor_atual_aws_apn_id": anterior.get(TAG_KEY),
                    "chaves_similares": anterior.get("chaves_similares") or {},
                    "acao_recomendada": (
                        f"configurar a tag {TAG_KEY}={valor} nas opções do {ferramenta}"
                        if (r.get("iac") or {}).get("gerenciado_por_ferramenta_aws")
                        else f"adicionar {TAG_KEY}={valor} no código {ferramenta} e aplicar"
                    ),
                }
            )
        elif r.get("categoria_final") in _CATEGORIAS_SUCESSO and anterior:
            alterados.append(
                {
                    "arn": r.get("arn"),
                    "servico": r.get("servico"),
                    "regiao": r.get("regiao"),
                    "acao": r.get("acao"),
                    "simulado": r.get("categoria_final") == "simulado_sucesso",
                    "valor_anterior_aws_apn_id": anterior.get(TAG_KEY),
                    "chaves_similares_anteriores": anterior.get("chaves_similares") or {},
                    "valor_aplicado": valor,
                }
            )

    por_ferramenta: dict[str, int] = defaultdict(int)
    for item in iac:
        por_ferramenta[item["ferramenta"]] += 1
    exemplos = _exemplos_iac(valor)
    exemplos_usados = {}
    for f in por_ferramenta:
        chave = _chave_exemplo(f)
        if chave and chave in exemplos:
            exemplos_usados[chave] = exemplos[chave]

    return {
        "conta_id": execution_report.get("conta_id"),
        "gerado_em": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "modo_execucao": execution_report.get("modo"),
        "valor_tag": valor,
        "resumo": {
            "total_recursos_iac": len(iac),
            "iac_por_ferramenta": dict(sorted(por_ferramenta.items())),
            "total_sobrescritos": sum(1 for a in alterados if a["valor_anterior_aws_apn_id"] is not None),
            "total_adicionados_com_chave_similar": sum(
                1 for a in alterados if a["valor_anterior_aws_apn_id"] is None
            ),
        },
        "recursos_iac": sorted(iac, key=lambda x: (x["ferramenta"], x["stack_cloudformation"] or "", x["arn"] or "")),
        "estado_anterior": sorted(alterados, key=lambda x: x["arn"] or ""),
        "como_aplicar_no_iac": exemplos_usados,
    }


def _fmt_similares(d: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in d.items())


def to_csv(relatorio: dict) -> str:
    """Uma planilha só, com a coluna `lista` separando as duas listas."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["lista", "arn", "servico", "regiao", "ferramenta_ou_acao", "stack_cloudformation",
         "valor_anterior_aws_apn_id", "chaves_similares", "evidencia_ou_valor_aplicado", "acao_recomendada"]
    )
    for i in relatorio["recursos_iac"]:
        w.writerow(
            ["iac", i["arn"], i["servico"], i["regiao"], i["ferramenta"], i["stack_cloudformation"] or "",
             i["valor_atual_aws_apn_id"] or "", _fmt_similares(i["chaves_similares"]), i["evidencia"] or "",
             i["acao_recomendada"]]
        )
    for a in relatorio["estado_anterior"]:
        w.writerow(
            ["estado_anterior", a["arn"], a["servico"], a["regiao"],
             (a["acao"] or "") + (" (simulado)" if a["simulado"] else ""), "",
             a["valor_anterior_aws_apn_id"] or "", _fmt_similares(a["chaves_similares_anteriores"]),
             a["valor_aplicado"], ""]
        )
    return buf.getvalue()


def _md_cel(v) -> str:
    return str(v if v not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def to_markdown(relatorio: dict) -> str:
    r = relatorio["resumo"]
    linhas = [
        f"# Relatório de tagueamento PRM — conta {relatorio.get('conta_id')}",
        "",
        f"Gerado em {relatorio['gerado_em']} (execução: {relatorio.get('modo_execucao')}). "
        f"Tag: `{TAG_KEY}={relatorio['valor_tag']}`.",
        "",
        "## 1. Recursos gerenciados por IaC — ação do cliente",
        "",
        f"{r['total_recursos_iac']} recurso(s). A automação não tagueia recursos de IaC: o próximo "
        "apply desfaria a tag. Adicione a tag no código (exemplos no fim).",
        "",
    ]
    if r["iac_por_ferramenta"]:
        linhas += [f"- {f}: {n}" for f, n in r["iac_por_ferramenta"].items()] + [""]
        linhas += [
            "| Recurso (ARN) | Serviço | Região | Ferramenta | Stack | Valor atual | Evidência |",
            "|---|---|---|---|---|---|---|",
        ]
        for i in relatorio["recursos_iac"]:
            linhas.append(
                "| " + " | ".join(
                    _md_cel(x) for x in (i["arn"], i["servico"], i["regiao"], i["ferramenta"],
                                         i["stack_cloudformation"], i["valor_atual_aws_apn_id"], i["evidencia"])
                ) + " |"
            )
        linhas.append("")
    linhas += [
        "## 2. Estado anterior dos recursos alterados",
        "",
        f"{r['total_sobrescritos']} sobrescrito(s) (havia outro valor em `{TAG_KEY}`) e "
        f"{r['total_adicionados_com_chave_similar']} com chave de grafia parecida (mantida como estava; "
        f"a AWS só reconhece `{TAG_KEY}` minúsculo).",
        "",
    ]
    if relatorio["estado_anterior"]:
        linhas += [
            "| Recurso (ARN) | Ação | Valor anterior aws-apn-id | Chaves parecidas antes | Valor aplicado |",
            "|---|---|---|---|---|",
        ]
        for a in relatorio["estado_anterior"]:
            acao = (a["acao"] or "") + (" (simulado)" if a["simulado"] else "")
            linhas.append(
                "| " + " | ".join(
                    _md_cel(x) for x in (a["arn"], acao, a["valor_anterior_aws_apn_id"],
                                         _fmt_similares(a["chaves_similares_anteriores"]), a["valor_aplicado"])
                ) + " |"
            )
        linhas.append("")
    if relatorio["como_aplicar_no_iac"]:
        linhas += ["## Como aplicar a tag no IaC", ""]
        for ferramenta, exemplo in relatorio["como_aplicar_no_iac"].items():
            linhas += [f"**{ferramenta}**", "", "```", exemplo, "```", ""]
    return "\n".join(linhas)
