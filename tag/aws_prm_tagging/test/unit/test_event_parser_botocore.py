"""Verificação estrutural dos extractors dedicados de `event_parser.py`
contra os shapes REAIS das operações de API no `botocore` instalado
localmente.

## O que este teste prova, e o que ele NÃO prova

Prova: que cada `path` hardcoded num extractor (`_DirectPath`/
`_ConstructedPath`, ver `event_parser.py`) corresponde a um campo que
**realmente existe** na definição da operação de API correspondente — pega
erro de digitação, nome de campo trocado, ou operação renomeada numa
atualização futura do botocore, sem precisar de nenhuma credencial AWS
nem de rodar nada contra uma conta real. É a forma de teste "segura"
descrita em docs/melhorias-futuras.md (ver "Extractors da Etapa 3 ainda não
validados em sandbox") — cobre um MOTIVO real de erro (campo inexistente),
mas não o outro (a capitalização exata que o CloudTrail grava, que o shape
do botocore não garante para serviços de protocolo query/ec2/rest-xml — ver
docstring do módulo `event_parser.py`). Para essa segunda lacuna, o plano é
um runbook de captura manual em sandbox (fora do escopo automatizável) —
ver `docs/melhorias-futuras.md`.

Os extractors com lógica própria (EC2 x4, S3, SQS, Route 53, ELB, WorkSpaces
— funções nomeadas em vez de `_DirectPath`/`_ConstructedPath`) não são
verificados aqui: já são casos especiais documentados no próprio código
(estrutura de CloudTrail conhecida e diferente do shape do botocore, no
caso do EC2; construção deliberada a partir de `requestParameters` para
evitar o problema de capitalização, nos outros).
"""
from __future__ import annotations

import pytest

botocore_session = pytest.importorskip("botocore.session")

from aws_prm_tagging import event_parser  # noqa: E402

# (eventSource sem ".amazonaws.com") -> id do pacote botocore, só para os
# casos em que os dois nomes divergem (ver docstring de event_mapping.py —
# mesma armadilha que já pegou CloudHSM/Keyspaces/Kinesis Analytics/
# Timestream por engano numa versão anterior deste projeto).
_EVENT_SOURCE_PARA_BOTOCORE_ID = {
    "cassandra": "keyspaces",
    "cloudhsm": "cloudhsmv2",
    "elasticloadbalancing": "elbv2",
    "elasticfilesystem": "efs",
    "elasticmapreduce": "emr",
    "kinesisanalytics": "kinesisanalyticsv2",
    "states": "stepfunctions",
    "timestream": "timestream-write",
}

# Dois eventNames que compartilham o mesmo eventSource real mas vêm de
# pacotes SDK DIFERENTES (ver event_mapping.py — "es"/"opensearch" cobrem o
# mesmo eventSource "es.amazonaws.com" para o legado/o atual; "apigateway"/
# "apigatewayv2" cobrem o mesmo eventSource "apigateway.amazonaws.com" para
# REST API (v1) e HTTP/WebSocket API (v2)) — sobrescreve o id derivado só
# para esses dois casos específicos, por (eventSource, eventName).
_EVENT_OVERRIDE_PARA_BOTOCORE_ID = {
    ("es.amazonaws.com", "CreateDomain"): "opensearch",
    ("apigateway.amazonaws.com", "CreateApi"): "apigatewayv2",
}

# Casos confirmados em sandbox (test/manual-live-etapa3/README.md,
# 2026-09-23) onde o CloudTrail real ACHATA a resposta documentada pelo
# botocore — o wrapper (`DBInstance`/`DBCluster`/`CacheCluster`) que a
# operação oficialmente devolve não aparece no evento real; o `path` do
# extractor já reflete isso (sem o wrapper, ver event_parser.py), então a
# verificação abaixo desce pelo wrapper antes de validar o resto do path,
# pra continuar checando contra o shape real da operação em vez de deixar
# de verificar esses casos.
_WRAPPER_ACHATADO_PELO_CLOUDTRAIL = {
    ("rds.amazonaws.com", "CreateDBInstance"): ("DBInstance",),
    ("rds.amazonaws.com", "CreateDBCluster"): ("DBCluster",),
    ("elasticache.amazonaws.com", "CreateCacheCluster"): ("CacheCluster",),
}


def _service_model(session, event_source: str, event_name: str):
    override = _EVENT_OVERRIDE_PARA_BOTOCORE_ID.get((event_source, event_name))
    if override:
        return session.get_service_model(override)
    service_id = event_source.split(".")[0]
    service_id = _EVENT_SOURCE_PARA_BOTOCORE_ID.get(service_id, service_id)
    return session.get_service_model(service_id)


def _descend_shape(shape, path: tuple[str, ...], tolerant_casing: bool):
    for key in path:
        if shape is None or shape.type_name != "structure":
            return None
        member = shape.members.get(key)
        if member is None and tolerant_casing:
            alt = (key[0].lower() + key[1:]) if key else key
            member = shape.members.get(alt)
        if member is None:
            return None
        shape = member
    return shape


def _shape_has_path(shape, path: tuple[str, ...], tolerant_casing: bool) -> bool:
    return _descend_shape(shape, path, tolerant_casing) is not None


def _dedicated_path_specs():
    """(chave, spec) para todo extractor dedicado que seja `_DirectPath`/
    `_ConstructedPath` — os únicos com `path` inspecionável."""
    for key, spec in event_parser._REGISTRY.items():
        builder = spec.arn_builder
        if isinstance(builder, (event_parser._DirectPath, event_parser._ConstructedPath)):
            yield key, spec, builder


@pytest.mark.parametrize(
    "key,spec,builder",
    list(_dedicated_path_specs()),
    ids=[f"{k[0]}/{k[1]}" for k, _, _ in _dedicated_path_specs()],
)
def test_caminho_do_extractor_existe_no_shape_real_da_operacao(key, spec, builder):
    event_source, event_name = key
    session = botocore_session.get_session()
    model = _service_model(session, event_source, event_name)
    assert event_name in model.operation_names, (
        f"'{event_name}' não existe mais em '{event_source}' segundo o botocore instalado — "
        "operação renomeada/removida? Ver event_mapping.py."
    )
    op = model.operation_model(event_name)

    if isinstance(builder, event_parser._DirectPath):
        target_shape = op.output_shape
        origem = "output"
    else:
        target_shape = op.output_shape if builder.source == "responseElements" else op.input_shape
        origem = builder.source

    wrapper = _WRAPPER_ACHATADO_PELO_CLOUDTRAIL.get(key)
    if wrapper is not None:
        target_shape = _descend_shape(target_shape, wrapper, tolerant_casing=True)
        assert target_shape is not None, (
            f"wrapper conhecido {wrapper} não existe mais no shape de {origem} de "
            f"{event_source}/{event_name} — a exceção de achatamento em "
            "_WRAPPER_ACHATADO_PELO_CLOUDTRAIL pode estar desatualizada"
        )

    assert _shape_has_path(target_shape, builder.path, builder.tolerant_casing), (
        f"caminho {builder.path} (tolerant_casing={builder.tolerant_casing}) não encontrado no "
        f"shape de {origem} de {event_source}/{event_name} — ver event_parser.py"
    )
