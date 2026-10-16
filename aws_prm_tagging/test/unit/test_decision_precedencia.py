"""Regra de precedência de `decision.classify_resource` (Etapa 2a).

Cobre a ordem de decisão descrita no docstring de `decision.py`: tag ausente
(decide entre `taguear`/`pular_iac` conforme IaC) vs. tag presente (decide
entre `ja_ok`/`conflito` comparando com `expected_tag_value`, sem o IaC
mudar esse resultado), e o caso de uma chave de tag com grafia parecida mas
case diferente (tratada como ausência da chave exata).
"""
from __future__ import annotations

from aws_prm_tagging import decision

EXPECTED = "pc:5ugbbrmu7ud3u5hsipfzug61p"


def test_tag_ausente_sem_iac_taguear(recurso_factory):
    """Caso mais simples: nada de sinal (nem tag, nem IaC) -> taguear.

    `iac_tipo="desconhecido"` é o "sem IaC" deste teste: `decision.py` trata
    a ausência de qualquer sinal de IaC como "não detectado" (suposição
    documentada no módulo), não como uma terceira categoria própria."""
    recurso = recurso_factory(status_tag="sem_tag", iac_tipo="desconhecido")
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_TAGUEAR


def test_tag_ausente_com_iac_cloudformation_pular_iac(recurso_factory):
    """IaC detectado tem precedência sobre a ausência de tag: nunca taguear
    via API um recurso gerenciado por CloudFormation/CDK."""
    recurso = recurso_factory(
        status_tag="sem_tag", iac_tipo="cloudformation", stack_name="minha-stack"
    )
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_PULAR_IAC
    assert resultado["iac"]["detectado"] is True
    assert resultado["iac"]["stack_name"] == "minha-stack"


def test_tag_ausente_com_iac_terraform_heuristico_pular_iac(recurso_factory):
    """Mesma regra do teste anterior, para a heurística de Terraform (que
    não tem sinal nativo confiável como o CloudFormation, mas ainda assim
    tem precedência sobre "taguear")."""
    recurso = recurso_factory(status_tag="sem_tag", iac_tipo="terraform_heuristico")
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_PULAR_IAC


def test_pular_iac_com_ferramenta_aws_conhecida_tem_motivo_especifico(recurso_factory):
    """Quando a stack de CloudFormation é gerada internamente por uma
    automação da AWS (Elastic Beanstalk/Control Tower/Service
    Catalog/eksctl — ver `iac_detection.py`), não existe IaC do cliente
    para editar: o `motivo` deve apontar a ferramenta certa em vez do
    texto genérico "taguear via IaC", que não tem nenhuma ação real por
    trás nesses casos. A DECISÃO continua `pular_iac` — nunca tagueado via
    API, exatamente igual ao caso sem ferramenta identificada."""
    recurso = recurso_factory(
        status_tag="sem_tag",
        iac_tipo="cloudformation",
        stack_name="awseb-e-abc123-stack",
        ferramenta_aws="AWS Elastic Beanstalk",
    )
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_PULAR_IAC
    assert "AWS Elastic Beanstalk" in resultado["motivo"]
    assert resultado["iac"]["gerenciado_por_ferramenta_aws"] == "AWS Elastic Beanstalk"


def test_pular_iac_sem_ferramenta_aws_conhecida_mantem_motivo_generico(recurso_factory):
    """Stack presumivelmente do próprio cliente (sem prefixo de ferramenta
    AWS conhecido): o motivo orienta o cliente a pôr a tag no código do IaC."""
    recurso = recurso_factory(
        status_tag="sem_tag", iac_tipo="cloudformation", stack_name="minha-stack-de-producao"
    )
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_PULAR_IAC
    assert "no código do IaC" in resultado["motivo"]
    assert resultado["iac"]["gerenciado_por_ferramenta_aws"] is None


def test_tag_presente_valor_esperado_ja_ok(recurso_factory):
    recurso = recurso_factory(status_tag="ok", valor_tag_encontrado=EXPECTED, iac_tipo="desconhecido")
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_JA_OK
    assert resultado["valor_tag_atual"] == EXPECTED


def test_tag_presente_valor_diferente_sobrescreve(recurso_factory):
    """valor de outro parceiro é sobrescrito, e o valor anterior fica
    registrado em `estado_anterior`."""
    recurso = recurso_factory(
        status_tag="conflito", valor_tag_encontrado="pc:valor-errado", iac_tipo="desconhecido"
    )
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_SOBRESCREVER
    assert resultado["valor_tag_atual"] == "pc:valor-errado"
    assert resultado["estado_anterior"] == {"aws-apn-id": "pc:valor-errado", "chaves_similares": {}}


def test_tag_presente_valor_diferente_com_iac_pula_iac(recurso_factory):
    """TODO IaC é pulado, mesmo com outro valor presente — o apply
    seguinte do IaC desfaria a sobrescrita. O valor anterior vai para a
    lista do cliente."""
    recurso = recurso_factory(
        status_tag="conflito",
        valor_tag_encontrado="pc:valor-errado",
        iac_tipo="cloudformation",
        stack_name="stack-x",
    )
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_PULAR_IAC
    assert "outro valor" in resultado["motivo"]
    assert resultado["iac"]["detectado"] is True
    assert resultado["iac"]["stack_name"] == "stack-x"
    assert resultado["estado_anterior"]["aws-apn-id"] == "pc:valor-errado"


def test_chave_case_diferente_tagueia_e_registra_estado_anterior(recurso_factory):
    """com `AWS-APN-ID` (grafia parecida), a nossa `aws-apn-id` é
    adicionada por cima; a chave parecida e seu valor ficam registrados
    em `estado_anterior` para o relatório do cliente."""
    recurso = recurso_factory(
        status_tag="sem_tag",
        iac_tipo="desconhecido",
        tag_similar_encontrada=True,
        tag_similar_chaves=["AWS-APN-ID"],
    )
    recurso["tag_similar_valores"] = {"AWS-APN-ID": "pc:digitado"}
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_TAGUEAR_COM_TAG_SIMILAR
    assert resultado["tag_similar_encontrada"] is True
    assert resultado["tag_similar_chaves"] == ["AWS-APN-ID"]
    assert resultado["estado_anterior"] == {"aws-apn-id": None, "chaves_similares": {"AWS-APN-ID": "pc:digitado"}}


def test_sem_nada_anterior_estado_anterior_nulo(recurso_factory):
    recurso = recurso_factory(status_tag="sem_tag", iac_tipo="desconhecido")
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_TAGUEAR
    assert resultado["estado_anterior"] is None


def test_ja_ok_com_iac_continua_ja_ok(recurso_factory):
    recurso = recurso_factory(status_tag="ok", valor_tag_encontrado=EXPECTED, iac_tipo="terraform_heuristico")
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_JA_OK


def test_pulumi_e_pulado(recurso_factory):
    recurso = recurso_factory(status_tag="sem_tag", iac_tipo="pulumi_heuristico")
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_PULAR_IAC


def test_tag_similar_com_iac_detectado_ainda_pula_iac(recurso_factory):
    """IaC detectado tem precedência sobre tag similar (mesmo espírito da
    precedência de conflito): se o recurso já não seria tagueado via API de
    qualquer forma, o risco de criar uma chave quase-duplicada não existe —
    o flag de tag similar continua visível no relatório, só não muda a
    decisão para `revisar_tag_similar`."""
    recurso = recurso_factory(
        status_tag="sem_tag",
        iac_tipo="cloudformation",
        tag_similar_encontrada=True,
        tag_similar_chaves=["Aws-Apn-Id"],
    )
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_PULAR_IAC
    assert resultado["tag_similar_encontrada"] is True
