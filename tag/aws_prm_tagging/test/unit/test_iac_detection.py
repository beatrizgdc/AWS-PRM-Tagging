"""`iac_detection.detect_iac` — detecção de IaC e da ferramenta AWS por trás
da stack de CloudFormation, quando aplicável.

Sem teste antes desta suíte. Cobre as regras já existentes (CloudFormation
tem precedência sobre Terraform, heurística de Terraform em 2 níveis,
ausência de sinal vira "desconhecido") e a identificação de
`gerenciado_por_ferramenta_aws` — usada por `decision.py` para compor um
`motivo` útil quando a stack detectada não é do cliente (Elastic
Beanstalk/Control Tower/Service Catalog/eksctl), sem mudar a decisão
`pular_iac` em nenhum caso.
"""
from __future__ import annotations

from aws_prm_tagging import iac_detection


def test_sem_nenhum_sinal_e_desconhecido():
    resultado = iac_detection.detect_iac({"Nome": "recurso-qualquer"})
    assert resultado == {"tipo": iac_detection.IAC_DESCONHECIDO, "stack_name": None, "gerenciado_por_ferramenta_aws": None}


def test_cloudformation_detectado_sem_ferramenta_aws_conhecida():
    """Stack de nome comum, sem nenhum dos prefixos de ferramenta AWS
    conhecidos — presumido como stack do próprio cliente."""
    resultado = iac_detection.detect_iac({"aws:cloudformation:stack-name": "minha-stack-de-producao"})
    assert resultado["tipo"] == iac_detection.IAC_CLOUDFORMATION
    assert resultado["stack_name"] == "minha-stack-de-producao"
    assert resultado["gerenciado_por_ferramenta_aws"] is None


def test_elastic_beanstalk_identificado_pelo_prefixo_awseb():
    resultado = iac_detection.detect_iac({"aws:cloudformation:stack-name": "awseb-e-abc123-stack"})
    assert resultado["tipo"] == iac_detection.IAC_CLOUDFORMATION
    assert resultado["gerenciado_por_ferramenta_aws"] == "AWS Elastic Beanstalk"


def test_control_tower_identificado_pelo_prefixo_stackset():
    resultado = iac_detection.detect_iac(
        {"aws:cloudformation:stack-name": "StackSet-AWSControlTowerBP-BASELINE-CONFIG-abc"}
    )
    assert resultado["gerenciado_por_ferramenta_aws"] == "AWS Control Tower"


def test_service_catalog_identificado_pelo_prefixo_sc():
    resultado = iac_detection.detect_iac({"aws:cloudformation:stack-name": "SC-123456789012-pp-abc123"})
    assert resultado["gerenciado_por_ferramenta_aws"] == "AWS Service Catalog"


def test_eksctl_identificado_pelo_prefixo_eksctl():
    resultado = iac_detection.detect_iac({"aws:cloudformation:stack-name": "eksctl-meu-cluster-cluster"})
    assert resultado["gerenciado_por_ferramenta_aws"] == "eksctl"


def test_cloudformation_tem_precedencia_sobre_terraform():
    resultado = iac_detection.detect_iac(
        {"aws:cloudformation:stack-name": "minha-stack", "terraform": "true"}
    )
    assert resultado["tipo"] == iac_detection.IAC_CLOUDFORMATION


def test_terraform_por_chave_propria():
    resultado = iac_detection.detect_iac({"terraform": "true"})
    assert resultado == {"tipo": iac_detection.IAC_TERRAFORM_HEURISTICO, "stack_name": None, "gerenciado_por_ferramenta_aws": None}


def test_terraform_por_managed_by():
    resultado = iac_detection.detect_iac({"managed-by": "terraform"})
    assert resultado["tipo"] == iac_detection.IAC_TERRAFORM_HEURISTICO


def test_terraform_por_valor_catch_all_com_chave_arbitraria():
    resultado = iac_detection.detect_iac({"Provisioner": "Terraform"})
    assert resultado["tipo"] == iac_detection.IAC_TERRAFORM_HEURISTICO
