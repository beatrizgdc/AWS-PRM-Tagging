"""Detecção heurística e best-effort de IaC a partir das tags de um recurso.

Regra de ouro: na ausência de qualquer sinal, o resultado é sempre
"desconhecido" — nunca assumimos "não gerenciado por IaC".
"""
from __future__ import annotations

IAC_CLOUDFORMATION = "cloudformation"
IAC_TERRAFORM_HEURISTICO = "terraform_heuristico"
IAC_DESCONHECIDO = "desconhecido"

_CFN_STACK_NAME_TAG = "aws:cloudformation:stack-name"

# Terraform não deixa nenhum sinal nativo confiável (ao contrário do
# CloudFormation/CDK). Estas são apenas convenções de tag comuns adotadas por
# times que usam Terraform — a presença delas é um indício, não uma prova.
# Padrão 1: chave própria "terraform"/"Terraform" com valor "true".
_TERRAFORM_SELF_KEYS = {"terraform", "Terraform", "TERRAFORM"}
# Padrão 2: chave genérica de governança (ex.: "managed-by") com valor "terraform".
_MANAGED_BY_KEYS = {"managed-by", "ManagedBy", "managed_by", "Managed-By"}


def detect_iac(tags: dict[str, str]) -> dict[str, str | None]:
    stack_name = tags.get(_CFN_STACK_NAME_TAG)
    if stack_name is not None:
        return {"tipo": IAC_CLOUDFORMATION, "stack_name": stack_name}

    for key in _TERRAFORM_SELF_KEYS:
        if key in tags and str(tags[key]).strip().lower() == "true":
            return {"tipo": IAC_TERRAFORM_HEURISTICO, "stack_name": None}
    for key in _MANAGED_BY_KEYS:
        if key in tags and str(tags[key]).strip().lower() == "terraform":
            return {"tipo": IAC_TERRAFORM_HEURISTICO, "stack_name": None}
            
# Catch-all: além dos nomes de chave conhecidos acima, qualquer tag cujo
# VALOR seja literalmente "terraform" também conta como indício — orgs
# usam nomes de chave arbitrários (IaC, Provisioner, CreatedBy, source
# etc.) para essa mesma convenção. Risco de falso positivo é
# desprezível; o custo de um falso negativo aqui é maior (uma etapa
# seguinte poderia taguear via API um recurso na verdade gerenciado por
# Terraform), então a heurística erra deliberadamente para o lado de
# detectar demais, não de menos.

for value in tags.values():
    if isinstance(value, str) and value.strip().lower() == "terraform":
        return {"tipo": IAC_TERRAFORM_HEURISTICO, "stack_name": None}
        
return {"tipo": IAC_DESCONHECIDO, "stack_name": None}
