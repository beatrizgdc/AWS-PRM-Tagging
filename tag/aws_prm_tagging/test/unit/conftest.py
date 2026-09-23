"""Fixtures compartilhadas pelos testes unitários (offline, sem AWS/boto3).

Centraliza a construção de dicts no formato exato que a Etapa 1 produz
(`resource_discovery._build_resource` / `report.build_report`), para que
testes de qualquer módulo futuro — não só `decision.py` — não precisem
reconstruir esse schema à mão nem duplicar os defaults entre arquivos.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def recurso_factory():
    """Fábrica de dicts de recurso no formato produzido pela Etapa 1.

    Uso: `recurso_factory(status_tag="ok", iac_tipo="cloudformation")` — cada
    parâmetro tem um default razoável para o caso mais comum (recurso sem
    tag, sem IaC, EC2 genérico), então cada teste só precisa sobrescrever o
    que é relevante para o cenário que está exercitando.
    """

    def _build(
        arn: str = "arn:aws:ec2:us-east-1:000000000000:instance/i-abc123",
        servico: str = "Amazon EC2",
        regiao: str = "us-east-1",
        tipo_recurso: str | None = None,
        status_tag: str = "sem_tag",
        valor_tag_encontrado: str | None = None,
        iac_tipo: str = "desconhecido",
        stack_name: str | None = None,
        ferramenta_aws: str | None = None,
        tag_similar_encontrada: bool = False,
        tag_similar_chaves: list[str] | None = None,
    ) -> dict:
        return {
            "arn": arn,
            "servico": servico,
            "regiao": regiao,
            "tipo_recurso": tipo_recurso,
            "status_tag": status_tag,
            "valor_tag_encontrado": valor_tag_encontrado,
            "tag_similar_encontrada": tag_similar_encontrada,
            "tag_similar_chaves": tag_similar_chaves or [],
            "iac": {"tipo": iac_tipo, "stack_name": stack_name, "gerenciado_por_ferramenta_aws": ferramenta_aws},
        }

    return _build


@pytest.fixture
def relatorio_etapa1_factory():
    """Fábrica do relatório-container da Etapa 1 (`report.build_report`),
    já com os campos de topo que `decision.py` não usa preenchidos com
    valores neutros — o teste só precisa informar a lista de `recursos`."""

    def _build(recursos: list[dict], conta_id: str = "000000000000") -> dict:
        return {
            "conta_id": conta_id,
            "executado_em": "2026-01-01T00:00:00Z",
            "valor_tag_esperado": "pc:placeholder",
            "resumo": {},
            "recursos": recursos,
            "arvore_ou": None,
        }

    return _build


@pytest.fixture
def decisao_factory():
    """Fábrica de dicts de recurso no formato produzido pela Etapa 2a
    (`decision.classify_resource`) — usada pelos testes de `tag_execution.py`
    (Etapa 2b), que consomem o relatório de decisão, não o da Etapa 1."""

    def _build(
        arn: str = "arn:aws:ec2:us-east-1:000000000000:instance/i-abc123",
        servico: str = "Amazon EC2",
        regiao: str = "us-east-1",
        tipo_recurso: str | None = None,
        decisao: str = "taguear",
        valor_tag_atual: str | None = None,
        motivo: str = "",
    ) -> dict:
        return {
            "arn": arn,
            "servico": servico,
            "regiao": regiao,
            "tipo_recurso": tipo_recurso,
            "valor_tag_atual": valor_tag_atual,
            "decisao": decisao,
            "motivo": motivo,
            "tag_similar_encontrada": False,
            "tag_similar_chaves": [],
            "iac": {"tipo": "desconhecido", "detectado": False, "stack_name": None},
        }

    return _build


@pytest.fixture
def relatorio_decisao_factory():
    """Fábrica do relatório-container da Etapa 2a
    (`decision.build_decision_report`) — o teste só precisa informar a lista
    de `recursos` (dicts de `decisao_factory`)."""

    def _build(recursos: list[dict], conta_id: str = "000000000000") -> dict:
        return {
            "conta_id": conta_id,
            "executado_em": "2026-01-01T00:00:00Z",
            "valor_tag_esperado": "pc:placeholder",
            "resumo": {},
            "recursos": recursos,
            "erros": [],
        }

    return _build


@pytest.fixture
def fake_session_factory():
    """Fábrica de uma sessão boto3 falsa, sem nenhuma chamada de rede real —
    usada pelos testes de `tag_execution.py` que exercitam a revalidação
    (que usa `session.client(...)`). `clients` é um dict
    `{nome_do_client_boto3: objeto_stub_com_os_metodos_usados}`."""

    class _FakeSession:
        def __init__(self, clients: dict):
            self._clients = clients

        def client(self, service_name: str, region_name: str | None = None):
            return self._clients[service_name]

    def _build(clients: dict):
        return _FakeSession(clients)

    return _build
