"""`decision.build_decision_report` (Etapa 2a) — nível de lote/relatório.

Enquanto `test_decision_precedencia.py` e `test_decision_escopo.py` testam
`classify_resource` recurso a recurso, este arquivo cobre o comportamento de
`build_decision_report` sobre um lote inteiro: robustez a entrada malformada
(sem derrubar o lote), o resumo agregado, e a garantia de que não há estado
global vazando entre chamadas com `expected_tag_value` diferentes — o caso
de sub-OUs de um mesmo cliente com contratos diferentes lendo o mesmo
relatório da Etapa 1.
"""
from __future__ import annotations

from aws_prm_tagging import decision

EXPECTED = "pc:5ugbbrmu7ud3u5hsipfzug61p"
OUTRO_EXPECTED = "pc:outrocontrato123"


def test_lote_vazio_relatorio_valido_sem_erro(relatorio_etapa1_factory):
    """Um relatório da Etapa 1 sem nenhum recurso produz um relatório de
    decisão válido e vazio — não é um caso de erro."""
    relatorio = decision.build_decision_report(relatorio_etapa1_factory([]), EXPECTED)
    assert relatorio["recursos"] == []
    assert relatorio["erros"] == []
    assert relatorio["resumo"]["total_recursos_avaliados"] == 0
    assert relatorio["resumo"]["total_erros"] == 0
    assert relatorio["resumo"]["total_excluidos_fora_de_escopo"] == 0
    assert relatorio["resumo"]["por_decisao"] == {
        decision.DECISAO_TAGUEAR: 0,
        decision.DECISAO_PULAR_IAC: 0,
        decision.DECISAO_REVISAR_TAG_SIMILAR: 0,
        decision.DECISAO_JA_OK: 0,
        decision.DECISAO_CONFLITO: 0,
    }


def test_recurso_malformado_vira_erro_sem_interromper_lote(recurso_factory, relatorio_etapa1_factory):
    """Um recurso incompleto (aqui, sem `status_tag`/`iac`/`regiao`) vira uma
    entrada em `erros` com uma mensagem curta — o resto do lote é processado
    normalmente, sem exceção não tratada derrubando a execução inteira."""
    bom = recurso_factory(status_tag="sem_tag", iac_tipo="desconhecido")
    malformado = {"arn": "arn:aws:s3:::bucket-sem-status"}
    etapa1 = relatorio_etapa1_factory([bom, malformado])

    relatorio = decision.build_decision_report(etapa1, EXPECTED)

    assert len(relatorio["recursos"]) == 1
    assert relatorio["recursos"][0]["arn"] == bom["arn"]
    assert len(relatorio["erros"]) == 1
    assert relatorio["erros"][0]["arn"] == "arn:aws:s3:::bucket-sem-status"
    assert "erro" in relatorio["erros"][0]
    assert relatorio["resumo"]["total_erros"] == 1


def test_recurso_totalmente_malformado_nao_derruba_lote(relatorio_etapa1_factory):
    """Entradas que nem são um dict de recurso (None, string solta, número)
    também viram erro, não exceção — cobre o caso de um relatório da Etapa 1
    corrompido/gerado incorretamente, não só campos faltando."""
    etapa1 = relatorio_etapa1_factory([None, "isso não é um recurso", 42])

    relatorio = decision.build_decision_report(etapa1, EXPECTED)

    assert relatorio["recursos"] == []
    assert len(relatorio["erros"]) == 3
    assert relatorio["resumo"]["total_erros"] == 3


def test_resumo_agregado_por_decisao_servico_e_iac(recurso_factory, relatorio_etapa1_factory):
    """`resumo` conta corretamente nos três eixos pedidos (decisão, serviço,
    status de IaC), no mesmo estilo de agregação de `report.build_report`."""
    recursos = [
        recurso_factory(arn="arn:1", status_tag="sem_tag", iac_tipo="desconhecido", servico="Amazon EC2"),
        recurso_factory(arn="arn:2", status_tag="sem_tag", iac_tipo="cloudformation", servico="Amazon EC2"),
        recurso_factory(arn="arn:3", status_tag="ok", valor_tag_encontrado=EXPECTED, servico="Amazon S3"),
        recurso_factory(
            arn="arn:4", status_tag="conflito", valor_tag_encontrado="pc:outro", servico="Amazon S3"
        ),
    ]
    relatorio = decision.build_decision_report(relatorio_etapa1_factory(recursos), EXPECTED)

    resumo = relatorio["resumo"]
    assert resumo["por_decisao"] == {
        decision.DECISAO_TAGUEAR: 1,
        decision.DECISAO_PULAR_IAC: 1,
        decision.DECISAO_REVISAR_TAG_SIMILAR: 0,
        decision.DECISAO_JA_OK: 1,
        decision.DECISAO_CONFLITO: 1,
    }
    assert resumo["por_servico"] == {"Amazon EC2": 2, "Amazon S3": 2}
    assert resumo["por_status_iac"] == {
        "cloudformation": 1,
        "terraform_heuristico": 0,
        "desconhecido": 3,
    }


def test_recursos_fora_de_escopo_nao_aparecem_em_nenhuma_categoria(recurso_factory, relatorio_etapa1_factory):
    """Recursos fora do escopo (ver `test_decision_escopo.py`) não aparecem
    em `recursos` nem em `erros` — só são contados em
    `resumo.total_excluidos_fora_de_escopo`, à parte das 4 categorias de
    decisão e da lista de erros."""
    recursos = [
        recurso_factory(arn="arn:eks-fargate", servico="Amazon EKS", tipo_recurso="fargate_profile"),
        recurso_factory(arn="arn:bedrock-outro", servico="Amazon Bedrock", tipo_recurso=None),
        recurso_factory(arn="arn:normal", status_tag="sem_tag", iac_tipo="desconhecido"),
    ]
    relatorio = decision.build_decision_report(relatorio_etapa1_factory(recursos), EXPECTED)

    arns_no_relatorio = {r["arn"] for r in relatorio["recursos"]} | {
        e.get("arn") for e in relatorio["erros"]
    }
    assert "arn:eks-fargate" not in arns_no_relatorio
    assert "arn:bedrock-outro" not in arns_no_relatorio
    assert "arn:normal" in arns_no_relatorio
    assert relatorio["resumo"]["total_excluidos_fora_de_escopo"] == 2


def test_dois_valores_esperados_diferentes_resultados_independentes(recurso_factory, relatorio_etapa1_factory):
    """Simula duas sub-OUs com contratos diferentes lendo o MESMO relatório
    da Etapa 1 (que roda uma vez por conta, não por sub-OU). O recurso tem a
    tag gravada com o valor de EXPECTED — decidir com EXPECTED deve dar
    ja_ok; decidir com OUTRO_EXPECTED (contrato da outra sub-OU) deve dar
    conflito. Isso garante que `decision.py` recalcula a comparação com o
    `expected_tag_value` de cada chamada (ver docstring do módulo — nunca
    confia num `status_tag` congelado), e que nenhuma chamada deixa estado
    que vaze para a próxima."""
    recursos = [recurso_factory(arn="arn:1", status_tag="ok", valor_tag_encontrado=EXPECTED)]
    etapa1 = relatorio_etapa1_factory(recursos)

    relatorio_a = decision.build_decision_report(etapa1, EXPECTED)
    relatorio_b = decision.build_decision_report(etapa1, OUTRO_EXPECTED)

    assert relatorio_a["recursos"][0]["decisao"] == decision.DECISAO_JA_OK
    assert relatorio_b["recursos"][0]["decisao"] == decision.DECISAO_CONFLITO
    assert relatorio_b["recursos"][0]["valor_tag_atual"] == EXPECTED
    assert relatorio_a["valor_tag_esperado"] == EXPECTED
    assert relatorio_b["valor_tag_esperado"] == OUTRO_EXPECTED

    # Rodar de novo com EXPECTED depois de ter rodado com OUTRO_EXPECTED
    # devolve o mesmo resultado de antes — nenhuma chamada altera estado
    # que afete a próxima.
    relatorio_a_de_novo = decision.build_decision_report(etapa1, EXPECTED)
    assert relatorio_a_de_novo["recursos"][0]["decisao"] == decision.DECISAO_JA_OK
