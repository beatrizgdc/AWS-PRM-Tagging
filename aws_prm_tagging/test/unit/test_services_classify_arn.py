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


def test_nat_gateway_e_classificado_como_ec2():
    from aws_prm_tagging.services import classify_arn, load_services

    svc = classify_arn("arn:aws:ec2:us-east-1:000000000000:natgateway/nat-0abc", load_services())
    assert svc is not None and svc.product_service_code == "AmazonEC2"


def test_namespaces_preparados_ficam_inertes_enquanto_o_csv_nao_tiver_a_linha():
    """dax/mobiletargeting/sms-voice/workspaces-instances estão mapeados em
    _NAMESPACE_TO_CODE de antemão (item 15), mas só passam a classificar
    quando o CSV oficial tiver a linha correspondente."""
    from aws_prm_tagging.services import classify_arn, load_services

    services = load_services()
    codigos = {s.product_service_code for s in services}
    for arn, codigo in [
        ("arn:aws:dax:us-east-1:000000000000:cache/c1", "AmazonDAX"),
        ("arn:aws:sms-voice:us-east-1:000000000000:pool/p1", "AmazonPinpoint"),
        ("arn:aws:workspaces-instances:us-east-1:000000000000:workspaceinstance/w1", "AmazonWorkSpacesInstances"),
    ]:
        svc = classify_arn(arn, services)
        if codigo in codigos:
            assert svc is not None and svc.product_service_code == codigo
        else:
            assert svc is None
