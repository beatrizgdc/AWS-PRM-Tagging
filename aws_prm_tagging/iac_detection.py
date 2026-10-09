"""Detecção heurística e best-effort de IaC a partir das tags de um recurso.

Regra de ouro: na ausência de qualquer sinal, o resultado é sempre
"desconhecido" — nunca assumimos "não gerenciado por IaC".

**Isso nunca muda a decisão de tagueamento** — a regra de que recurso
gerenciado por IaC nunca é tagueado via API/CLI/Console é fixa (ver
`decision.py`). O que este módulo também identifica, à parte, é QUEM
provavelmente é dono do CloudFormation por trás da tag
`aws:cloudformation:stack-name`: várias automações da própria AWS (Elastic
Beanstalk, Control Tower, Service Catalog) e ferramentas de terceiros
(`eksctl`) usam CloudFormation por baixo dos panos, sem o cliente nunca ter
escrito ou visto um template — ao contrário de uma stack que o cliente
mesmo autora em Terraform/CloudFormation/CDK. Para esses casos, "vá editar
seu Terraform" (o motivo padrão de `pular_iac`) é um conselho sem nenhuma
ação real por trás — não existe template do cliente para editar. A
identificação (`gerenciado_por_ferramenta_aws`) serve só para
`decision.py` compor um `motivo` que aponte a ação certa (ex.: configurar
a tag nas opções do ambiente do Elastic Beanstalk) — a decisão continua
`pular_iac`, nunca tagueada via API.
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

# Convenções de nome de stack CloudFormation conhecidas, geradas por
# automações da própria AWS (ou ferramentas de terceiro-mas-não-do-cliente)
# — não pelo cliente diretamente. `startswith` na ordem declarada; sem
# sobreposição conhecida entre os prefixos.
_PREFIXOS_FERRAMENTA_AWS: dict[str, str] = {
    "awseb-": "AWS Elastic Beanstalk",
    "StackSet-AWSControlTower": "AWS Control Tower",
    "SC-": "AWS Service Catalog",
    "eksctl-": "eksctl",
}


def _ferramenta_aws_gerenciando_stack(stack_name: str) -> str | None:
    for prefixo, ferramenta in _PREFIXOS_FERRAMENTA_AWS.items():
        if stack_name.startswith(prefixo):
            return ferramenta
    return None


def detect_iac(tags: dict[str, str]) -> dict[str, str | None]:
    stack_name = tags.get(_CFN_STACK_NAME_TAG)
    if stack_name is not None:
        return {
            "tipo": IAC_CLOUDFORMATION,
            "stack_name": stack_name,
            "gerenciado_por_ferramenta_aws": _ferramenta_aws_gerenciando_stack(stack_name),
        }

    for key in _TERRAFORM_SELF_KEYS:
        if key in tags and str(tags[key]).strip().lower() == "true":
            return {"tipo": IAC_TERRAFORM_HEURISTICO, "stack_name": None, "gerenciado_por_ferramenta_aws": None}
    for key in _MANAGED_BY_KEYS:
        if key in tags and str(tags[key]).strip().lower() == "terraform":
            return {"tipo": IAC_TERRAFORM_HEURISTICO, "stack_name": None, "gerenciado_por_ferramenta_aws": None}

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
            return {"tipo": IAC_TERRAFORM_HEURISTICO, "stack_name": None, "gerenciado_por_ferramenta_aws": None}

    return {"tipo": IAC_DESCONHECIDO, "stack_name": None, "gerenciado_por_ferramenta_aws": None}
