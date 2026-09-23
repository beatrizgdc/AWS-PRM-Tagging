"""Testes de `event_mapping.py` (Etapa 3) — a tabela nunca pode divergir do
CSV oficial, e o event pattern gerado precisa ter a forma que o EventBridge
espera."""
from __future__ import annotations

import glob
import json
from pathlib import Path

from aws_prm_tagging import event_mapping
from aws_prm_tagging.services import load_services

_INFRA_DIR = Path(event_mapping.__file__).parent / "infra"


def test_toda_entrada_do_csv_tem_alguma_entrada_na_tabela():
    """`validate_against_csv` é a rede de segurança contra divergência
    silenciosa: se a AWS adicionar um serviço novo ao CSV do PRM sem que a
    tabela seja atualizada (nem para `None`), este teste quebra."""
    faltando = event_mapping.validate_against_csv(load_services())
    assert faltando == [], (
        f"product_service_code(s) do CSV sem entrada em event_mapping._EVENT_MAPPING: {faltando} "
        "— adicione uma entrada mapeada ou `None` explícito."
    )


def test_todo_product_service_code_da_tabela_existe_no_csv():
    """Sentido contrário: a tabela não pode ter uma chave que não existe (mais)
    no CSV — evitaria manter uma entrada morta depois de o CSV mudar."""
    codigos_csv = {s.product_service_code for s in load_services()}
    for codigo in event_mapping.load_event_mapping():
        assert codigo in codigos_csv, f"'{codigo}' está em event_mapping.py mas não existe no CSV oficial"


def test_servicos_mapeados_e_nao_mapeados_sao_complementares():
    mapeados = set(event_mapping.mapped_services())
    nao_mapeados = set(event_mapping.unmapped_service_codes())
    todos = set(event_mapping.load_event_mapping())
    assert mapeados | nao_mapeados == todos
    assert mapeados & nao_mapeados == set()


def test_toda_regra_mapeada_tem_event_source_e_event_name_nao_vazios():
    for codigo, regras in event_mapping.mapped_services().items():
        assert regras, f"'{codigo}' está em mapped_services() mas com tupla vazia"
        for regra in regras:
            assert regra.event_source, f"event_source vazio em '{codigo}'"
            assert regra.event_name, f"event_name vazio em '{codigo}'"


def test_build_event_pattern_agrupa_por_event_source_via_or():
    pattern = event_mapping.build_event_pattern()

    assert pattern["detail-type"] == ["AWS API Call via CloudTrail"]
    assert "$or" in pattern
    assert len(pattern["$or"]) > 0

    # Cada alternativa do $or tem exatamente uma fonte e uma lista de
    # eventName não vazia — nunca dois eventSource misturados numa mesma
    # alternativa (isso quebraria a correlação source<->eventName que o
    # $or existe para garantir).
    fontes_vistas = set()
    for alternativa in pattern["$or"]:
        assert len(alternativa["source"]) == 1
        fonte = alternativa["source"][0]
        assert fonte not in fontes_vistas, f"fonte '{fonte}' duplicada em duas alternativas do $or"
        fontes_vistas.add(fonte)
        assert alternativa["detail"]["eventName"], f"eventName vazio para '{fonte}'"


def test_build_event_pattern_cobre_ec2_run_instances():
    pattern = event_mapping.build_event_pattern()
    alternativa_ec2 = next(alt for alt in pattern["$or"] if alt["source"] == ["aws.ec2"])
    assert "RunInstances" in alternativa_ec2["detail"]["eventName"]


def test_build_event_patterns_respeita_o_limite_de_tamanho():
    """Cada pattern individual devolvido por `build_event_patterns` precisa
    caber dentro da quota padrão do EventBridge (2.048 caracteres) — aqui
    testado com uma margem menor (a mesma usada por padrão na função), para
    nunca depender de um aumento de quota em produção."""
    patterns = event_mapping.build_event_patterns()
    assert len(patterns) > 1, "o conjunto atual de serviços mapeados deveria exigir mais de uma regra"
    for pattern in patterns:
        tamanho = len(json.dumps(pattern, separators=(",", ":")))
        assert tamanho <= event_mapping._MAX_EVENT_PATTERN_CHARS


def test_build_event_patterns_cobre_todas_as_alternativas_sem_duplicar():
    pattern_unico = event_mapping.build_event_pattern()
    patterns_divididos = event_mapping.build_event_patterns()

    fontes_unico = sorted(alt["source"][0] for alt in pattern_unico["$or"])
    fontes_divididas = sorted(alt["source"][0] for alternativas in patterns_divididos for alt in alternativas["$or"])
    assert fontes_divididas == fontes_unico  # mesma cobertura, sem perder nem duplicar nenhuma fonte


def test_arquivos_gerados_em_infra_nao_estao_desatualizados():
    """Guarda de deriva: os arquivos `infra/event_pattern.*.generated.json`
    commitados precisam bater com o que `build_event_patterns()` produz
    AGORA — se este teste falhar, rode
    `python3 -m aws_prm_tagging.event_mapping` (do diretório pai) e
    commite os arquivos atualizados (e ajuste o número de regras em
    `infra/template.yaml` se a contagem mudou — ver `infra/README.md`)."""
    esperados = event_mapping.build_event_patterns()

    arquivos = sorted(glob.glob(str(_INFRA_DIR / "event_pattern.*.generated.json")))
    assert len(arquivos) == len(esperados), (
        f"infra/ tem {len(arquivos)} arquivo(s) de pattern gerado(s), mas build_event_patterns() "
        f"produz {len(esperados)} agora — rode `python3 -m aws_prm_tagging.event_mapping` para regenerar."
    )
    for arquivo, esperado in zip(arquivos, esperados):
        with open(arquivo, encoding="utf-8") as f:
            atual = json.load(f)
        assert atual == esperado, f"{arquivo} está desatualizado em relação a build_event_patterns()"
