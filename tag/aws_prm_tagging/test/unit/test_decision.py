"""Testes unitários da Etapa 2a (decision.py) — 100% offline, sem boto3/AWS."""
from __future__ import annotations

from aws_prm_tagging import decision

EXPECTED = "pc:xxxxxxxxxxxxxxxxxxxxxxxxx"
OUTRO_EXPECTED = "pc:outrocontrato123"


def _recurso(
    arn: str = "arn:aws:ec2:us-east-1:000000000000:instance/i-abc123",
    servico: str = "Amazon EC2",
    regiao: str = "us-east-1",
    tipo_recurso: str | None = None,
    status_tag: str = "sem_tag",
    valor_tag_encontrado: str | None = None,
    iac_tipo: str = "desconhecido",
    stack_name: str | None = None,
    tag_similar_encontrada: bool = False,
    tag_similar_chaves: list[str] | None = None,
) -> dict:
    """Constrói um dict de recurso no formato exato produzido por
    `resource_discovery._build_resource` (Etapa 1), para não depender de
    nenhum acesso AWS real nos testes."""
    return {
        "arn": arn,
        "servico": servico,
        "regiao": regiao,
        "tipo_recurso": tipo_recurso,
        "status_tag": status_tag,
        "valor_tag_encontrado": valor_tag_encontrado,
        "tag_similar_encontrada": tag_similar_encontrada,
        "tag_similar_chaves": tag_similar_chaves or [],
        "iac": {"tipo": iac_tipo, "stack_name": stack_name},
    }


def _relatorio_etapa1(recursos: list[dict], conta_id: str = "000000000000") -> dict:
    return {
        "conta_id": conta_id,
        "executado_em": "2026-01-01T00:00:00Z",
        "valor_tag_esperado": EXPECTED,
        "resumo": {},
        "recursos": recursos,
        "arvore_ou": None,
    }


# ---------------------------------------------------------------------------
# Regras de precedência básicas
# ---------------------------------------------------------------------------


def test_tag_ausente_sem_iac_taguear():
    recurso = _recurso(status_tag="sem_tag", iac_tipo="desconhecido")
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_TAGUEAR


def test_tag_ausente_com_iac_cloudformation_pular_iac():
    recurso = _recurso(status_tag="sem_tag", iac_tipo="cloudformation", stack_name="minha-stack")
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_PULAR_IAC
    assert resultado["iac"]["detectado"] is True
    assert resultado["iac"]["stack_name"] == "minha-stack"


def test_tag_ausente_com_iac_terraform_heuristico_pular_iac():
    recurso = _recurso(status_tag="sem_tag", iac_tipo="terraform_heuristico")
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_PULAR_IAC


def test_tag_presente_valor_esperado_ja_ok():
    recurso = _recurso(status_tag="ok", valor_tag_encontrado=EXPECTED, iac_tipo="desconhecido")
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_JA_OK
    assert resultado["valor_tag_atual"] == EXPECTED


def test_tag_presente_valor_diferente_conflito():
    recurso = _recurso(
        status_tag="conflito", valor_tag_encontrado="pc:valor-errado", iac_tipo="desconhecido"
    )
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_CONFLITO
    assert resultado["valor_tag_atual"] == "pc:valor-errado"


def test_tag_presente_valor_diferente_com_iac_ainda_conflito():
    """IaC nunca muda a decisão de conflito — nunca sobrescrever automaticamente."""
    recurso = _recurso(
        status_tag="conflito",
        valor_tag_encontrado="pc:valor-errado",
        iac_tipo="cloudformation",
        stack_name="stack-x",
    )
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_CONFLITO
    assert resultado["iac"]["detectado"] is True
    assert resultado["iac"]["stack_name"] == "stack-x"


# ---------------------------------------------------------------------------
# Tag com grafia parecida (case diferente)
# ---------------------------------------------------------------------------


def test_chave_case_diferente_tratada_como_ausente_com_flag():
    recurso = _recurso(
        status_tag="sem_tag",
        iac_tipo="desconhecido",
        tag_similar_encontrada=True,
        tag_similar_chaves=["AWS-APN-ID"],
    )
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado["decisao"] == decision.DECISAO_TAGUEAR
    assert resultado["tag_similar_encontrada"] is True
    assert resultado["tag_similar_chaves"] == ["AWS-APN-ID"]


# ---------------------------------------------------------------------------
# EKS — sub-recursos independentes
# ---------------------------------------------------------------------------


def test_eks_cluster_node_group_node_volume_lb_decisoes_independentes():
    tipos = ["cluster", "node_group", "node", "ebs_volume", "load_balancer"]
    recursos = [
        _recurso(
            arn=f"arn:aws:eks:us-east-1:000000000000:{tipo}/x-{tipo}",
            servico="Amazon EKS",
            tipo_recurso=tipo,
            status_tag="sem_tag",
            iac_tipo="desconhecido",
        )
        for tipo in tipos
    ]
    resultados = [decision.classify_resource(r, EXPECTED) for r in recursos]
    assert all(r is not None for r in resultados)
    assert [r["tipo_recurso"] for r in resultados] == tipos
    assert all(r["decisao"] == decision.DECISAO_TAGUEAR for r in resultados)


def test_eks_fargate_fora_de_escopo_excluido():
    # resource_discovery.py nunca emite Fargate (excluído na descoberta) —
    # "fargate_profile" aqui é ilustrativo, simulando qualquer tipo_recurso
    # de EKS fora da lista suportada chegando (ex.: entrada malformada/futura).
    recurso = _recurso(servico="Amazon EKS", tipo_recurso="fargate_profile")
    assert decision.classify_resource(recurso, EXPECTED) is None


# ---------------------------------------------------------------------------
# Bedrock — só application inference profile
# ---------------------------------------------------------------------------


def test_bedrock_nao_application_inference_profile_excluido():
    recurso = _recurso(servico="Amazon Bedrock", tipo_recurso=None)
    assert decision.classify_resource(recurso, EXPECTED) is None


def test_bedrock_application_inference_profile_participa_normalmente():
    recurso = _recurso(
        arn="arn:aws:bedrock:us-east-1:000000000000:application-inference-profile/abc",
        servico="Amazon Bedrock",
        tipo_recurso="application_inference_profile",
        status_tag="ok",
        valor_tag_encontrado=EXPECTED,
    )
    resultado = decision.classify_resource(recurso, EXPECTED)
    assert resultado is not None
    assert resultado["decisao"] == decision.DECISAO_JA_OK


# ---------------------------------------------------------------------------
# build_decision_report — lote completo
# ---------------------------------------------------------------------------


def test_lote_vazio_relatorio_valido_sem_erro():
    relatorio = decision.build_decision_report(_relatorio_etapa1([]), EXPECTED)
    assert relatorio["recursos"] == []
    assert relatorio["erros"] == []
    assert relatorio["resumo"]["total_recursos_avaliados"] == 0
    assert relatorio["resumo"]["total_erros"] == 0
    assert relatorio["resumo"]["total_excluidos_fora_de_escopo"] == 0
    assert relatorio["resumo"]["por_decisao"] == {
        decision.DECISAO_TAGUEAR: 0,
        decision.DECISAO_PULAR_IAC: 0,
        decision.DECISAO_JA_OK: 0,
        decision.DECISAO_CONFLITO: 0,
    }


def test_recurso_malformado_vira_erro_sem_interromper_lote():
    bom = _recurso(status_tag="sem_tag", iac_tipo="desconhecido")
    malformado = {"arn": "arn:aws:s3:::bucket-sem-status"}  # falta status_tag, iac, etc.
    relatorio = decision.build_decision_report(_relatorio_etapa1([bom, malformado]), EXPECTED)
    assert len(relatorio["recursos"]) == 1
    assert relatorio["recursos"][0]["arn"] == bom["arn"]
    assert len(relatorio["erros"]) == 1
    assert relatorio["erros"][0]["arn"] == "arn:aws:s3:::bucket-sem-status"
    assert "erro" in relatorio["erros"][0]
    assert relatorio["resumo"]["total_erros"] == 1


def test_recurso_totalmente_malformado_nao_derruba_lote():
    relatorio = decision.build_decision_report(
        _relatorio_etapa1([None, "isso não é um recurso", 42]), EXPECTED
    )
    assert relatorio["recursos"] == []
    assert len(relatorio["erros"]) == 3
    assert relatorio["resumo"]["total_erros"] == 3


def test_resumo_agregado_por_decisao_servico_e_iac():
    recursos = [
        _recurso(arn="arn:1", status_tag="sem_tag", iac_tipo="desconhecido", servico="Amazon EC2"),
        _recurso(arn="arn:2", status_tag="sem_tag", iac_tipo="cloudformation", servico="Amazon EC2"),
        _recurso(arn="arn:3", status_tag="ok", valor_tag_encontrado=EXPECTED, servico="Amazon S3"),
        _recurso(
            arn="arn:4",
            status_tag="conflito",
            valor_tag_encontrado="pc:outro",
            servico="Amazon S3",
        ),
    ]
    relatorio = decision.build_decision_report(_relatorio_etapa1(recursos), EXPECTED)
    resumo = relatorio["resumo"]
    assert resumo["por_decisao"] == {
        decision.DECISAO_TAGUEAR: 1,
        decision.DECISAO_PULAR_IAC: 1,
        decision.DECISAO_JA_OK: 1,
        decision.DECISAO_CONFLITO: 1,
    }
    assert resumo["por_servico"] == {"Amazon EC2": 2, "Amazon S3": 2}
    assert resumo["por_status_iac"] == {
        "cloudformation": 1,
        "terraform_heuristico": 0,
        "desconhecido": 3,
    }


def test_recursos_fora_de_escopo_nao_aparecem_em_nenhuma_categoria():
    recursos = [
        _recurso(arn="arn:eks-fargate", servico="Amazon EKS", tipo_recurso="fargate_profile"),
        _recurso(arn="arn:bedrock-outro", servico="Amazon Bedrock", tipo_recurso=None),
        _recurso(arn="arn:normal", status_tag="sem_tag", iac_tipo="desconhecido"),
    ]
    relatorio = decision.build_decision_report(_relatorio_etapa1(recursos), EXPECTED)
    arns_no_relatorio = {r["arn"] for r in relatorio["recursos"]} | {
        e.get("arn") for e in relatorio["erros"]
    }
    assert "arn:eks-fargate" not in arns_no_relatorio
    assert "arn:bedrock-outro" not in arns_no_relatorio
    assert "arn:normal" in arns_no_relatorio
    assert relatorio["resumo"]["total_excluidos_fora_de_escopo"] == 2


# ---------------------------------------------------------------------------
# Sem estado global entre execuções (múltiplas sub-OUs/contratos)
# ---------------------------------------------------------------------------


def test_dois_valores_esperados_diferentes_resultados_independentes():
    """Simula duas sub-OUs com contratos diferentes lendo o MESMO relatório
    da Etapa 1 (que roda uma vez por conta, não por sub-OU). O recurso tem
    a tag com o valor de EXPECTED gravada — decidir com EXPECTED deve dar
    ja_ok; decidir com OUTRO_EXPECTED (contrato da outra sub-OU) deve dar
    conflito. Isso garante que a Etapa 2a recalcula a comparação com o
    expected_tag_value de cada chamada, em vez de confiar num status_tag
    congelado, e que não há estado global/cache vazando entre chamadas."""
    recursos = [_recurso(arn="arn:1", status_tag="ok", valor_tag_encontrado=EXPECTED)]
    etapa1 = _relatorio_etapa1(recursos)

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
