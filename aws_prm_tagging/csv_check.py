"""Etapa 4 — alerta quando a lista oficial de serviços muda (ideia do gestor,
registrada em docs/melhorias-futuras.md).

Compara a tabela "Resource Tagging Included Services" publicada na página
oficial do guia com o CSV empacotado (`data/...csv`) e com o mapeamento de
eventos (`event_mapping.py`). Motivação concreta: em 2026-09-30 o CSV do
projeto estava 8 serviços atrás da página, e o link de download do zip
oficial devolvia 404 — por isso a fonte é a TABELA da página, não o zip.

Roda centralmente (CLI/CI na Darede), não em cada conta de cliente: o CSV é
o mesmo para todos, e rodar em ~80 contas geraria o mesmo alerta 80 vezes.
Exige acesso de saída à internet (docs.aws.amazon.com).

Código de saída do subcomando: 0 = sem diferença, 3 = diferença encontrada.
"""
from __future__ import annotations

import csv
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from . import event_mapping

URL_PADRAO = "https://docs.aws.amazon.com/PRM/latest/aws-prm-onboarding-guide/resource-tagging-included-services.html"
_CSV = Path(__file__).parent / "data" / "resource-tagging-included-services.csv"


class _Tabelas(HTMLParser):
    """Extrai todas as tabelas HTML como listas de linhas de texto."""

    def __init__(self):
        super().__init__()
        self.tabelas: list[list[list[str]]] = []
        self._linha: list[str] | None = None
        self._celula: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tabelas.append([])
        elif tag == "tr" and self.tabelas:
            self._linha = []
        elif tag in ("td", "th") and self._linha is not None:
            self._celula = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._celula is not None and self._linha is not None:
            self._linha.append(" ".join("".join(self._celula).split()))
            self._celula = None
        elif tag == "tr" and self._linha is not None:
            if self._linha:
                self.tabelas[-1].append(self._linha)
            self._linha = None

    def handle_data(self, data):
        if self._celula is not None:
            self._celula.append(data)


def _nota(n: str) -> str:
    n = (n or "").strip()
    return "" if n.lower() == "none" else n


def extrair_servicos_da_pagina(html: str) -> list[tuple[str, str, str]]:
    p = _Tabelas()
    p.feed(html)
    for tabela in p.tabelas:
        if not tabela:
            continue
        cabecalho = [c.lower() for c in tabela[0]]
        if "product service code" in cabecalho and "service name" in cabecalho:
            i_nome, i_cod = cabecalho.index("service name"), cabecalho.index("product service code")
            i_nota = cabecalho.index("notes") if "notes" in cabecalho else None
            return [
                (lin[i_nome], lin[i_cod], _nota(lin[i_nota]) if i_nota is not None and i_nota < len(lin) else "")
                for lin in tabela[1:]
                if len(lin) > max(i_nome, i_cod)
            ]
    raise ValueError("Tabela 'Resource Tagging Included Services' não encontrada na página — o layout mudou?")


def _csv_local() -> list[tuple[str, str, str]]:
    with open(_CSV, encoding="utf-8") as f:
        return [(r["Service Name"], r["Product Service Code"], _nota(r["Notes"])) for r in csv.DictReader(f)]


def comparar(pagina: list[tuple[str, str, str]], local: list[tuple[str, str, str]] | None = None) -> dict:
    local = local if local is not None else _csv_local()
    p = {n: (c, nt) for n, c, nt in pagina}
    l_ = {n: (c, nt) for n, c, nt in local}
    codigos_mapeados = set(event_mapping.mapped_services())
    novos = [{"servico": n, "codigo": p[n][0], "nota": p[n][1]} for n in p if n not in l_]
    diferencas = {
        "novos_na_pagina": novos,
        "removidos_da_pagina": [{"servico": n, "codigo": l_[n][0]} for n in l_ if n not in p],
        "codigo_alterado": [{"servico": n, "antes": l_[n][0], "agora": p[n][0]} for n in p if n in l_ and p[n][0] != l_[n][0]],
        "nota_alterada": [{"servico": n, "antes": l_[n][1], "agora": p[n][1]} for n in p if n in l_ and p[n][1] != l_[n][1]],
        # Código novo que a Etapa 3 ainda não observa (precisa de entrada em event_mapping.py)
        "codigos_novos_sem_mapeamento": sorted({s["codigo"] for s in novos} - codigos_mapeados - set(event_mapping.unmapped_service_codes())),
    }
    diferencas["tem_diferenca"] = any(diferencas[k] for k in ("novos_na_pagina", "removidos_da_pagina", "codigo_alterado", "nota_alterada"))
    diferencas["total_pagina"], diferencas["total_csv_local"] = len(pagina), len(local)
    return diferencas


def baixar(url: str = URL_PADRAO, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "aws-prm-tagging-csv-check"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — URL fixa da documentação AWS
        return r.read().decode("utf-8", errors="replace")
