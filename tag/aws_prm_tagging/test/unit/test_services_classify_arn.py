"""`services.classify_arn` — desambiguação de código de produto compartilhado.

Cobre especificamente a correção do bug em que "vpc-lattice" era rotulado
como "AWS Transit Gateway" no relatório (código "AmazonVPC" compartilhado
por duas linhas do CSV, `_service_by_code` sempre pegava a primeira) —
não afeta a tag aplicada, só o campo "servico" do relatório, mas confundia
quem lê o relatório.
"""
from __future__ import annotations

from aws_prm_tagging import services


def _service_list():
    return services.load_services()


def test_vpc_lattice_classificado_com_nome_proprio_nao_transit_gateway():
    svc = services.classify_arn(
        "arn:aws:vpc-lattice:us-east-1:000000000000:service/svc-0123456789abcdef0", _service_list()
    )
    assert svc is not None
    assert svc.name == "Amazon VPC Lattice"


def test_ec2_instance_ainda_classificado_como_amazon_ec2():
    """Regressão: a correção do vpc-lattice não pode quebrar a
    desambiguação ec2 compute vs. rede que já existia."""
    svc = services.classify_arn("arn:aws:ec2:us-east-1:000000000000:instance/i-0123456789abcdef0", _service_list())
    assert svc is not None
    assert svc.name == "Amazon EC2"


def test_ec2_vpc_ainda_classificado_como_transit_gateway():
    """Recurso de rede sob o namespace "ec2" continua caindo na linha "AWS
    Transit Gateway" do CSV — essa parte não é bug (ver docstring de
    `services.py`), só o "vpc-lattice" era."""
    svc = services.classify_arn("arn:aws:ec2:us-east-1:000000000000:vpc/vpc-0123456789abcdef0", _service_list())
    assert svc is not None
    assert svc.name == "AWS Transit Gateway"


def test_namespace_desconhecido_devolve_none():
    assert services.classify_arn("arn:aws:namespace-inexistente:us-east-1:000000000000:algo/x", _service_list()) is None


def test_ssm_opsitem_classificado_como_systems_manager():
    """CSV: "OpsCenter only" — o único tipo de recurso SSM em escopo."""
    svc = services.classify_arn(
        "arn:aws:ssm:us-east-1:000000000000:opsitem/oi-0123456789abcdef0", _service_list()
    )
    assert svc is not None
    assert svc.name == "AWS Systems Manager"


def test_ssm_parameter_fora_de_escopo_devolve_none():
    """Parameter Store não é "OpsCenter" — não deve ser tagueado."""
    assert services.classify_arn(
        "arn:aws:ssm:us-east-1:000000000000:parameter/meu-parametro", _service_list()
    ) is None


def test_ssm_maintenance_window_fora_de_escopo_devolve_none():
    assert services.classify_arn(
        "arn:aws:ssm:us-east-1:000000000000:maintenancewindow/mw-0123456789abcdef0", _service_list()
    ) is None
