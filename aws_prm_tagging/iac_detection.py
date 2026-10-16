"""Detecção heurística e best-effort de IaC a partir das tags de um recurso
(Etapa 1/2) e do evento do CloudTrail que o criou (Etapa 3).

MVP2: a regra "IaC nunca é tagueado pela automação" vale para TODO IaC —
Terraform/OpenTofu, CloudFormation, CDK e Pulumi. Esses recursos são
listados no relatório para o cliente (`relatorio_cliente.py`) com a
evidência que levou à detecção (`evidencia`) e a ferramenta provável
(`ferramenta`), para ele adicionar a tag no próprio código.

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
IAC_PULUMI_HEURISTICO = "pulumi_heuristico"
IAC_DESCONHECIDO = "desconhecido"

# MVP2 (regra da AWS repassada pelo DevOps em 13/10/2026): TODO recurso
# gerenciado por IaC — Terraform/OpenTofu, CloudFormation, CDK (que é
# CloudFormation por baixo) e Pulumi — nunca é tagueado pela automação; é
# listado no relatório para o cliente colocar a tag no próprio código.
IAC_DETECTADO = frozenset({IAC_CLOUDFORMATION, IAC_TERRAFORM_HEURISTICO, IAC_PULUMI_HEURISTICO})
TODOS_OS_TIPOS_IAC = (IAC_CLOUDFORMATION, IAC_TERRAFORM_HEURISTICO, IAC_PULUMI_HEURISTICO, IAC_DESCONHECIDO)

_CFN_STACK_NAME_TAG = "aws:cloudformation:stack-name"

# Terraform/Pulumi não deixam nenhum sinal nativo confiável nas TAGS (ao
# contrário do CloudFormation/CDK). Estas são convenções de tag comuns — a
# presença delas é um indício, não uma prova. Para recursos NOVOS a Etapa 3
# tem um sinal bem mais forte: o `userAgent` do evento do CloudTrail
# (`detect_iac_from_cloudtrail`).
# Padrão 1: chave própria "terraform"/"Terraform" com valor "true".
_TERRAFORM_SELF_KEYS = {"terraform", "Terraform", "TERRAFORM"}
# Padrão 2: chave genérica de governança (ex.: "managed-by") com valor da ferramenta.
_MANAGED_BY_KEYS = {"managed-by", "ManagedBy", "managed_by", "Managed-By", "managedBy", "managed-by-iac"}

# valor (minúsculo) -> (tipo, ferramenta). Vale para QUALQUER chave (ver
# catch-all em `detect_iac`). "cdk"/"cloudformation" como valor cobrem
# times que marcam isso por convenção mesmo quando a tag
# aws:cloudformation:stack-name não chegou ao recurso.
_VALORES_IAC: dict[str, tuple[str, str]] = {
    "terraform": (IAC_TERRAFORM_HEURISTICO, "Terraform"),
    "opentofu": (IAC_TERRAFORM_HEURISTICO, "OpenTofu"),
    "tofu": (IAC_TERRAFORM_HEURISTICO, "OpenTofu"),
    "pulumi": (IAC_PULUMI_HEURISTICO, "Pulumi"),
    "cdk": (IAC_CLOUDFORMATION, "AWS CDK"),
    "aws-cdk": (IAC_CLOUDFORMATION, "AWS CDK"),
    "cloudformation": (IAC_CLOUDFORMATION, "CloudFormation"),
}
# Prefixos de CHAVE de convenção comum (o Pulumi não aplica tag sozinho,
# mas times costumam marcar "pulumi:project"/"pulumi:stack" via
# autotagging). Indício, não prova.
_PREFIXOS_CHAVE_IAC: dict[str, tuple[str, str]] = {
    "pulumi:": (IAC_PULUMI_HEURISTICO, "Pulumi"),
}

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


def _resultado(tipo: str, ferramenta: str | None, evidencia: str | None, stack_name=None, ferramenta_aws=None) -> dict:
    return {
        "tipo": tipo,
        "stack_name": stack_name,
        "gerenciado_por_ferramenta_aws": ferramenta_aws,
        "ferramenta": ferramenta,
        "evidencia": evidencia,
    }


def detect_iac(tags: dict[str, str]) -> dict[str, str | None]:
    stack_name = tags.get(_CFN_STACK_NAME_TAG)
    if stack_name is not None:
        return _resultado(
            IAC_CLOUDFORMATION,
            "CloudFormation/CDK",
            f"tag {_CFN_STACK_NAME_TAG}={stack_name}",
            stack_name=stack_name,
            ferramenta_aws=_ferramenta_aws_gerenciando_stack(stack_name),
        )

    for key in _TERRAFORM_SELF_KEYS:
        if key in tags and str(tags[key]).strip().lower() == "true":
            return _resultado(IAC_TERRAFORM_HEURISTICO, "Terraform", f"tag {key}={tags[key]}")
    for key in tags:
        for prefixo, (tipo, ferramenta) in _PREFIXOS_CHAVE_IAC.items():
            if key.lower().startswith(prefixo):
                return _resultado(tipo, ferramenta, f"tag {key}")

    # Chaves de governança conhecidas primeiro (mensagem de evidência mais
    # clara), depois o catch-all: qualquer tag cujo VALOR seja literalmente
    # o nome de uma ferramenta de IaC também conta como indício — orgs usam
    # nomes de chave arbitrários (IaC, Provisioner, CreatedBy, source etc.)
    # para essa mesma convenção. Risco de falso positivo é desprezível; o
    # custo de um falso negativo aqui é maior (taguear via API um recurso
    # que o IaC do cliente vai "corrigir" de volta no próximo apply), então
    # a heurística erra deliberadamente para o lado de detectar demais.
    chaves_ordenadas = [k for k in tags if k in _MANAGED_BY_KEYS] + [k for k in tags if k not in _MANAGED_BY_KEYS]
    for key in chaves_ordenadas:
        value = tags[key]
        if isinstance(value, str):
            achado = _VALORES_IAC.get(value.strip().lower())
            if achado:
                return _resultado(achado[0], achado[1], f"tag {key}={value}")

    return _resultado(IAC_DESCONHECIDO, None, None)


# ---------------------------------------------------------------------------
# Etapa 3: sinal do CloudTrail (recurso NOVO)
# ---------------------------------------------------------------------------

# Trechos (minúsculos) do `userAgent` gravado pelo CloudTrail. O provider
# AWS do Terraform se identifica como
# "APN/1.0 HashiCorp/1.0 Terraform/1.x (+https://www.terraform.io) terraform-provider-aws/5.x ..."
# e o OpenTofu como "OpenTofu/1.x ...". O Pulumi (pulumi-aws e
# aws-native) inclui "pulumi". A ordem importa: OpenTofu/Pulumi usam o
# terraform-provider-aws por baixo em alguns casos, então são testados antes.
_USER_AGENT_IAC: tuple[tuple[str, str, str], ...] = (
    ("opentofu", IAC_TERRAFORM_HEURISTICO, "OpenTofu"),
    ("pulumi", IAC_PULUMI_HEURISTICO, "Pulumi"),
    ("terraform", IAC_TERRAFORM_HEURISTICO, "Terraform"),
    ("hashicorp", IAC_TERRAFORM_HEURISTICO, "Terraform"),
    ("cloudformation.amazonaws.com", IAC_CLOUDFORMATION, "CloudFormation/CDK"),
    ("aws-cdk", IAC_CLOUDFORMATION, "AWS CDK"),
)


def detect_iac_from_cloudtrail(detail: dict | None) -> dict[str, str | None] | None:
    """Detecta IaC pelo evento do CloudTrail que criou o recurso (Etapa 3).

    Devolve o mesmo formato de `detect_iac` ou `None` se o evento não traz
    sinal. Olha `userAgent`, `sourceIPAddress` e `userIdentity.invokedBy`
    (o CloudFormation aparece como `cloudformation.amazonaws.com` em pelo
    menos um deles). É bem mais confiável que as tags para recurso novo: o
    Terraform não deixa tag nenhuma por padrão, mas sempre se identifica no
    `userAgent` da chamada de criação."""
    if not isinstance(detail, dict):
        return None
    identidade = detail.get("userIdentity") or {}
    campos = (
        ("userAgent", detail.get("userAgent")),
        ("sourceIPAddress", detail.get("sourceIPAddress")),
        ("userIdentity.invokedBy", identidade.get("invokedBy") if isinstance(identidade, dict) else None),
    )
    for nome, valor in campos:
        if not isinstance(valor, str) or not valor:
            continue
        minusculo = valor.lower()
        for trecho, tipo, ferramenta in _USER_AGENT_IAC:
            if trecho in minusculo:
                return _resultado(tipo, ferramenta, f"CloudTrail {nome}: {valor[:200]}")
    return None
