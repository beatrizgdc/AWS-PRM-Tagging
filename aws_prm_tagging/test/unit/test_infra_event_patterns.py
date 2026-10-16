"""Guarda de deriva entre `infra/template-regional.yaml` e os arquivos
`infra/event_pattern.*.generated.json` (gerados por `event_mapping.py`).

Não há como o CloudFormation incluir um JSON externo dentro de
`AWS::Events::Rule.EventPattern` (ao contrário de `AWS::Serverless::
StateMachine`, que tem `DefinitionUri`) — os blocos `EventPattern` em
`template-regional.yaml` são colados manualmente a partir dos arquivos gerados. Este
teste é o que garante que essa cópia manual não diverge silenciosamente."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_INFRA_DIR = Path(__file__).parent.parent.parent / "infra"


class _PermissiveLoader(yaml.SafeLoader):
    """Trata qualquer tag CloudFormation (`!Ref`, `!Sub`, `!GetAtt`, ...)
    como um valor opaco em vez de falhar — este teste só precisa inspecionar
    `EventPattern`, que não usa nenhuma dessas tags."""


def _construct_any(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


_PermissiveLoader.add_multi_constructor("!", _construct_any)


def _load_template() -> dict:
    with open(_INFRA_DIR / "template-regional.yaml", encoding="utf-8") as f:
        return yaml.load(f, Loader=_PermissiveLoader)


def test_toda_regra_de_evento_do_template_bate_com_o_arquivo_gerado():
    template = _load_template()
    arquivos_gerados = sorted(_INFRA_DIR.glob("event_pattern.*.generated.json"))
    assert arquivos_gerados, "nenhum infra/event_pattern.*.generated.json encontrado"

    for i, arquivo in enumerate(arquivos_gerados):
        with open(arquivo, encoding="utf-8") as f:
            esperado = json.load(f)
        nome_recurso = f"Etapa3EventRule{i}"
        recurso = template["Resources"].get(nome_recurso)
        assert recurso is not None, (
            f"{arquivo.name} existe, mas não há recurso '{nome_recurso}' em template-regional.yaml — "
            "o número de regras mudou e o template não foi atualizado (ver infra/README.md)."
        )
        pattern_no_template = recurso["Properties"]["EventPattern"]
        assert pattern_no_template == esperado, f"EventPattern de '{nome_recurso}' diverge de {arquivo.name}"

    # E o inverso: nenhuma regra "a mais" sem arquivo gerado correspondente.
    indices_de_regra = [
        nome for nome in template["Resources"] if nome.startswith("Etapa3EventRule")
    ]
    assert len(indices_de_regra) == len(arquivos_gerados), (
        f"template-regional.yaml tem {len(indices_de_regra)} regra(s) Etapa3EventRule*, mas há "
        f"{len(arquivos_gerados)} arquivo(s) gerado(s) — remova a(s) regra(s) extra(s) ou regenere."
    )


def test_regra_nativa_ec2_bate_com_event_mapping():
    """A regra de evento nativo do EC2 no template precisa ser idêntica a
    `event_mapping.NATIVE_EVENT_PATTERNS["ec2_instance_running"]`."""
    from aws_prm_tagging import event_mapping

    template = _load_template()
    recurso = template["Resources"].get("Etapa3NativeEc2InstanceRunningRule")
    assert recurso is not None, "regra nativa de EC2 ausente em template-regional.yaml"
    assert recurso["Properties"]["EventPattern"] == event_mapping.NATIVE_EVENT_PATTERNS["ec2_instance_running"]


def test_toda_regra_tem_dlq_e_a_politica_da_fila_autoriza_exatamente_as_regras():
    """Item 9: todo alvo de regra precisa ter DeadLetterConfig apontando para
    a DLQ da região, e a política da fila precisa autorizar TODAS as regras
    (uma regra nova sem entrada na política perderia os eventos com
    AccessDenied silencioso na hora de mandar para a DLQ)."""
    template = _load_template()
    recursos = template["Resources"]
    regras = sorted(n for n, r in recursos.items() if r["Type"] == "AWS::Events::Rule")
    assert regras
    for nome in regras:
        for alvo in recursos[nome]["Properties"]["Targets"]:
            assert "DeadLetterConfig" in alvo, f"{nome} sem DeadLetterConfig"
    fontes = recursos["Etapa3EventsDlqPolicy"]["Properties"]["PolicyDocument"]["Statement"][0]["Condition"][
        "ArnEquals"
    ]["aws:SourceArn"]
    autorizadas = sorted(str(f).split(".")[0] for f in fontes)
    assert autorizadas == regras


def test_regional_nao_tem_topico_sns_e_hub_tem():
    """Item 7: o tópico é peça única por conta — nunca duplicado por região."""
    regional = _load_template()
    assert not any(r["Type"].startswith("AWS::SNS::") for r in regional["Resources"].values())
    with open(_INFRA_DIR / "template-hub.yaml", encoding="utf-8") as f:
        hub = yaml.load(f, Loader=_PermissiveLoader)
    tipos = [r["Type"] for r in hub["Resources"].values()]
    # Desde o item 8 o hub também tem o tagueamento inicial; o que importa
    # aqui é que o tópico existe UMA vez (no hub) e nunca na regional.
    assert tipos.count("AWS::SNS::Topic") == 1


def test_regra_nativa_ecs_bate_com_event_mapping():
    from aws_prm_tagging import event_mapping

    recurso = _load_template()["Resources"].get("Etapa3NativeEcsTaskRunningRule")
    assert recurso is not None, "regra nativa de ECS ausente"
    assert recurso["Properties"]["EventPattern"] == event_mapping.NATIVE_EVENT_PATTERNS["ecs_task_running"]


def test_regra_de_drift_bate_com_event_mapping():
    from aws_prm_tagging import event_mapping

    recurso = _load_template()["Resources"].get("Etapa4DriftRule")
    assert recurso is not None, "regra de drift (Etapa 4) ausente"
    assert recurso["Properties"]["EventPattern"] == event_mapping.DRIFT_EVENT_PATTERN
