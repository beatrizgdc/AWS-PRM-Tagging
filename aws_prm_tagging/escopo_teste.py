"""Filtro de escopo para conta COMPARTILHADA (sandbox) — Etapas 3 e 4.

A Etapa 3 não tem modo dry-run: enquanto a stack regional está implantada,
ela tagueia qualquer recurso criado na região, por qualquer pessoa. Numa
sandbox compartilhada isso alcançaria recursos de outras pessoas. O
parâmetro `FiltroEscopoTeste` (variável `FILTRO_ESCOPO`) limita a reação a:

- eventos de CloudTrail feitos por um principal cujo ARN contém um dos
  termos (ex.: o seu usuário/role do IAM), OU
- recursos cujo ARN contém um dos termos (ex.: o prefixo `prm-val-` dos
  recursos de teste — necessário para eventos nativos, como task ECS e
  instância EC2, que não trazem principal).

Termos separados por vírgula. **Vazio = sem filtro** (produção).
"""
from __future__ import annotations

import os


def termos() -> list[str]:
    return [t.strip() for t in os.environ.get("FILTRO_ESCOPO", "").split(",") if t.strip()]


def dentro_do_escopo(event: dict, arn: str) -> bool:
    lista = termos()
    if not lista:
        return True
    principal = ((event.get("detail") or {}).get("userIdentity") or {}).get("arn") or ""
    return any(t in principal or t in arn for t in lista)
