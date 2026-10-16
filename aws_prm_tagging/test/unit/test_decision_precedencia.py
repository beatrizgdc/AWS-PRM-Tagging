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
    AWS conhecido) continua com o motivo genérico de sempre — "taguear via
    IaC" faz sentido quando existe mesmo um IaC do cliente para agir."""
    recurso = recurso_factory(
        status_tag="sem_tag", iac_tipo="cloudformation", stack_name="minha-stack-de-producao"
    )
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_PULAR_IAC
    assert "taguear via IaC" in resultado["motivo"]
    assert resultado["iac"]["gerenciado_por_ferramenta_aws"] is None


def test_tag_presente_valor_esperado_ja_ok(recurso_factory):
    recurso = recurso_factory(status_tag="ok", valor_tag_encontrado=EXPECTED, iac_tipo="desconhecido")
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_JA_OK
    assert resultado["valor_tag_atual"] == EXPECTED


def test_tag_presente_valor_diferente_conflito(recurso_factory):
    recurso = recurso_factory(
        status_tag="conflito", valor_tag_encontrado="pc:valor-errado", iac_tipo="desconhecido"
    )
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_CONFLITO
    assert resultado["valor_tag_atual"] == "pc:valor-errado"


def test_tag_presente_valor_diferente_com_iac_ainda_conflito(recurso_factory):
    """IaC nunca muda um conflito para outra decisão — um valor de tag
    divergente nunca é sobrescrito automaticamente, gerenciado por IaC ou
    não. O IaC segue aparecendo no relatório só como metadado informativo."""
    recurso = recurso_factory(
        status_tag="conflito",
        valor_tag_encontrado="pc:valor-errado",
        iac_tipo="cloudformation",
        stack_name="stack-x",
    )
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_CONFLITO
    assert resultado["iac"]["detectado"] is True
    assert resultado["iac"]["stack_name"] == "stack-x"


def test_chave_case_diferente_vira_revisar_tag_similar(recurso_factory):
    """Uma tag com grafia parecida mas case diferente (ex.: `AWS-APN-ID`) não
    conta como a tag `aws-apn-id` (comparação de key é case-sensitive) — cai
    na regra de "tag ausente", mas NUNCA vira `taguear` diretamente: taguear
    via API por cima criaria uma chave quase-duplicada em vez de corrigir o
    provável erro de digitação. Vira `revisar_tag_similar`, carregando o
    flag `tag_similar_encontrada`/`tag_similar_chaves` (herdados do
    relatório da Etapa 1, via `tag_status.find_similar_tag_keys`) para
    revisão humana."""
    recurso = recurso_factory(
        status_tag="sem_tag",
        iac_tipo="desconhecido",
        tag_similar_encontrada=True,
        tag_similar_chaves=["AWS-APN-ID"],
    )
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_REVISAR_TAG_SIMILAR
    assert resultado["tag_similar_encontrada"] is True
    assert resultado["tag_similar_chaves"] == ["AWS-APN-ID"]


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
