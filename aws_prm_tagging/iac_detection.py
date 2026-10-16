"""Detecção de IaC pelas tags do recurso (Etapas 1/2) e pelo evento do
CloudTrail que o criou (Etapa 3).

Todo IaC (Terraform/OpenTofu, CloudFormation, CDK, Pulumi) nunca é tagueado
pela automação: vai para o relatório do cliente (`relatorio_cliente.py`) com
a evidência (`evidencia`) e a ferramenta provável (`ferramenta`). Sem sinal,
o resultado é "desconhecido", nunca "não gerenciado".

À parte, identifica stacks CloudFormation criadas por automações da AWS ou
de terceiros (Elastic Beanstalk, Control Tower, Service Catalog, eksctl):
não há template do cliente para editar, então `decision.py` usa
`gerenciado_por_ferramenta_aws` para indicar a ação certa. A decisão
continua `pular_iac`.
"""
from __future__ import annotations

IAC_CLOUDFORMATION = "cloudformation"
IAC_TERRAFORM_HEURISTICO = "terraform_heuristico"
IAC_PULUMI_HEURISTICO = "pulumi_heuristico"
IAC_DESCONHECIDO = "desconhecido"

# Todo recurso gerenciado por IaC (Terraform/OpenTofu, CloudFormation, CDK,
# Pulumi) nunca é tagueado; é listado para o cliente pôr a tag no código.
IAC_DETECTADO = frozenset({IAC_CLOUDFORMATION, IAC_TERRAFORM_HEURISTICO, IAC_PULUMI_HEURISTICO})
TODOS_OS_TIPOS_IAC = (IAC_CLOUDFORMATION, IAC_TERRAFORM_HEURISTICO, IAC_PULUMI_HEURISTICO, IAC_DESCONHECIDO)

_CFN_STACK_NAME_TAG = "aws:cloudformation:stack-name"

# Terraform/Pulumi não deixam sinal nativo nas tags. Estas convenções são
# indício, não prova; para recurso novo a Etapa 3 usa o `userAgent`.
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

    # Chaves conhecidas primeiro (evidência mais clara); depois, qualquer tag
    # cujo valor seja o nome de uma ferramenta de IaC. Erra de propósito para o
    # lado de detectar demais: taguear via API um recurso de IaC é pior.
    chaves_ordenadas = [k for k in tags if k in _MANAGED_BY_KEYS] + [k for k in tags if k not in _MANAGED_BY_KEYS]
    for key in chaves_ordenadas:
        value = tags[key]
        if isinstance(value, str):
            achado = _VALORES_IAC.get(value.strip().lower())
            if achado:
                return _resultado(achado[0], achado[1], f"tag {key}={value}")

    return _resultado(IAC_DESCONHECIDO, None, None)


# --- Etapa 3: sinal do CloudTrail (recurso novo) ---

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
    """IaC pelo evento do CloudTrail que criou o recurso (Etapa 3).

        Mesmo formato de `detect_iac`, ou `None` sem sinal. Olha `userAgent`,
        `sourceIPAddress` e `userIdentity.invokedBy`. Para recurso novo é mais
        confiável que as tags: o Terraform não deixa tag por padrão, mas sempre
        se identifica no `userAgent`."""
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
