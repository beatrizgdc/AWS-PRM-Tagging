"""Validação estrutural do Step Functions da Etapa 3
(`infra/statemachine/etapa3_delay_e_execucao.asl.json`) — sem AWS, sem
Step Functions Local: só parsing + verificação da forma da Amazon States
Language (ASL).

O arquivo no repositório NÃO é JSON válido por si só — tem placeholders
`${DebounceSeconds}`/`${LambdaArn}` que só viram JSON válido depois da
substituição que o CloudFormation faz via `DefinitionSubstitutions` (ver
`infra/template.yaml`). Este teste faz essa mesma substituição com valores
de exemplo antes de validar, para não precisar depender do CloudFormation
nem de nenhuma chamada de API."""
from __future__ import annotations

import json
from pathlib import Path

_ASL_PATH = (
    Path(__file__).parent.parent.parent / "infra" / "statemachine" / "etapa3_delay_e_execucao.asl.json"
)

_SUBSTITUICOES_DE_EXEMPLO = {
    "DebounceSeconds": "1800",
    "LambdaArn": "arn:aws:lambda:us-east-1:000000000000:function:exemplo",
}


def _carregar_definicao() -> dict:
    texto = _ASL_PATH.read_text(encoding="utf-8")
    for chave, valor in _SUBSTITUICOES_DE_EXEMPLO.items():
        texto = texto.replace("${" + chave + "}", valor)
    return json.loads(texto)


def test_definicao_e_json_valido_apos_substituicao():
    definicao = _carregar_definicao()
    assert isinstance(definicao, dict)


def test_start_at_existe_entre_os_estados():
    definicao = _carregar_definicao()
    assert definicao["StartAt"] in definicao["States"]


def test_todo_estado_tem_type_valido():
    definicao = _carregar_definicao()
    tipos_validos = {"Pass", "Task", "Choice", "Wait", "Succeed", "Fail", "Parallel", "Map"}
    for nome, estado in definicao["States"].items():
        assert estado.get("Type") in tipos_validos, f"estado '{nome}' com Type inválido/ausente: {estado.get('Type')}"


def test_todo_next_aponta_para_estado_existente():
    """`Next` (e `Default`, para `Choice`) precisa apontar para uma chave
    que realmente existe em `States` — um typo aqui só seria pego pelo
    Step Functions no momento do deploy/execução sem este teste."""
    definicao = _carregar_definicao()
    nomes_validos = set(definicao["States"])
    for nome, estado in definicao["States"].items():
        if "Next" in estado:
            assert estado["Next"] in nomes_validos, f"'{nome}'.Next aponta para estado inexistente: {estado['Next']}"
        if "Default" in estado:
            assert estado["Default"] in nomes_validos, f"'{nome}'.Default aponta para estado inexistente: {estado['Default']}"


def test_todo_estado_terminal_ou_tem_next_ou_e_terminal():
    """Todo estado precisa ser terminal (`End: true`, `Succeed`, `Fail`) ou
    ter `Next` — um estado sem nenhum dos dois trava a execução."""
    definicao = _carregar_definicao()
    for nome, estado in definicao["States"].items():
        tipo = estado.get("Type")
        e_terminal_por_tipo = tipo in ("Succeed", "Fail")
        tem_next = "Next" in estado
        tem_end_true = estado.get("End") is True
        assert e_terminal_por_tipo or tem_next or tem_end_true, (
            f"estado '{nome}' não tem Next nem End:true nem é Succeed/Fail — execução travaria aqui"
        )


def test_wait_state_usa_seconds_numerico_apos_substituicao():
    """O estado de debounce precisa virar um número de verdade depois da
    substituição do CloudFormation — se `Seconds` continuasse como string
    (`"${DebounceSeconds}"` entre aspas, por exemplo), o Step Functions
    rejeitaria a definição no deploy."""
    definicao = _carregar_definicao()
    estado_wait = next(e for e in definicao["States"].values() if e.get("Type") == "Wait")
    assert isinstance(estado_wait["Seconds"], int)


def test_lambda_invoke_usa_function_name_da_substituicao():
    definicao = _carregar_definicao()
    estado_task = next(e for e in definicao["States"].values() if e.get("Type") == "Task")
    assert estado_task["Parameters"]["FunctionName"] == _SUBSTITUICOES_DE_EXEMPLO["LambdaArn"]


def test_retry_do_lambda_invoke_tem_campos_obrigatorios():
    definicao = _carregar_definicao()
    estado_task = next(e for e in definicao["States"].values() if e.get("Type") == "Task")
    for retry in estado_task.get("Retry", []):
        assert retry["ErrorEquals"]
        assert isinstance(retry["IntervalSeconds"], int)
        assert isinstance(retry["MaxAttempts"], int)
        assert isinstance(retry["BackoffRate"], (int, float))
