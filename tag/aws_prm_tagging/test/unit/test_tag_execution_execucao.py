"""`tag_execution.run_stage2b` — revalidação, idempotência, modo dry-run e
formato do relatório de saída (Etapa 2b).

Os testes de revalidação usam `fake_session_factory` (`conftest.py`) com
clients stub simples (sem moto/LocalStack) — suficiente para cobrir a
lógica de orquestração deste módulo sem depender de rede nem de uma
dependência de teste adicional; um teste de integração mais realista contra
LocalStack/moto pode ser adicionado depois, sem mudar esta suíte.
"""
from __future__ import annotations

from botocore.exceptions import ClientError

from aws_prm_tagging import tag_execution

TAG_VALUE = "pc:xxxxxxxxxxxxxxxxxxxxxxxxx"


class _FakeTaggingClient:
    """Stub de `resourcegroupstaggingapi` — só `get_resources`, usado na
    revalidação do caminho genérico."""

    def __init__(self, tagged: dict[str, str] | None = None, erro: bool = False):
        self._tagged = tagged or {}
        self._erro = erro

    def get_resources(self, **kwargs):
        if self._erro:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "sem permissão"}}, "GetResources")
        return {
            "ResourceTagMappingList": [
                {"ResourceARN": arn, "Tags": [{"Key": tag_execution.TAG_KEY, "Value": valor}]}
                for arn, valor in self._tagged.items()
            ]
        }


class _FakeEksClient:
    def __init__(self, tagged: dict[str, str] | None = None):
        self._tagged = tagged or {}

    def list_tags_for_resource(self, resourceArn):
        valor = self._tagged.get(resourceArn)
        return {"tags": {tag_execution.TAG_KEY: valor} if valor else {}}


class _SpyExecutor:
    def __init__(self):
        self.arns_chamados: list[str] = []

    def tag_generic_batch(self, session, regiao, arns, tag_key, tag_value):
        self.arns_chamados.extend(arns)
        return {arn: tag_execution.RESULTADO_SIMULADO_OK for arn in arns}

    def tag_single(self, session, estrategia, resource, tag_key, tag_value):
        self.arns_chamados.append(resource.arn)
        return tag_execution.RESULTADO_SIMULADO_OK


def _relatorio_um_recurso_generico(arn: str, regiao: str = "us-east-1") -> dict:
    return {
        "conta_id": "000000000000",
        "recursos": [
            {"arn": arn, "servico": "Amazon S3", "regiao": regiao, "tipo_recurso": None, "decisao": "taguear"}
        ],
    }


def test_dry_run_nunca_toca_session_quando_revalidate_desligado():
    """`DryRunExecutor` não chama boto3 de escrita nunca — e sem
    revalidação, o orquestrador não deveria nem dereferenciar `session`.
    `session=None` aqui: se algum caminho tentasse usar a sessão, o teste
    quebraria com `AttributeError`, não silenciosamente."""
    relatorio = _relatorio_um_recurso_generico("arn:aws:s3:::bucket-x")
    resultado = tag_execution.run_stage2b(relatorio, session=None, expected_tag_value=TAG_VALUE, revalidate=False)
    assert resultado["recursos"][0]["resultado"] == tag_execution.RESULTADO_SIMULADO_OK


def test_revalidacao_pula_recurso_ja_tagueado_generico(fake_session_factory):
    """Recurso já tem `aws-apn-id` com o valor esperado (ex.: tagueado numa
    execução anterior) -> revalidação encontra e o orquestrador marca
    `ja_tagueado` sem chamar o executor — é isso que torna reexecuções
    idempotentes (ver docstring do módulo)."""
    arn = "arn:aws:s3:::bucket-ja-tagueado"
    session = fake_session_factory({"resourcegroupstaggingapi": _FakeTaggingClient({arn: TAG_VALUE})})
    spy = _SpyExecutor()

    resultado = tag_execution.run_stage2b(
        _relatorio_um_recurso_generico(arn), session=session, expected_tag_value=TAG_VALUE, revalidate=True, executor=spy
    )

    assert resultado["recursos"][0]["resultado"] == tag_execution.RESULTADO_JA_TAGUEADO
    assert spy.arns_chamados == []


def test_revalidacao_prossegue_para_recurso_ainda_sem_tag(fake_session_factory):
    arn = "arn:aws:s3:::bucket-pendente"
    session = fake_session_factory({"resourcegroupstaggingapi": _FakeTaggingClient({})})
    spy = _SpyExecutor()

    resultado = tag_execution.run_stage2b(
        _relatorio_um_recurso_generico(arn), session=session, expected_tag_value=TAG_VALUE, revalidate=True, executor=spy
    )

    assert resultado["recursos"][0]["resultado"] == tag_execution.RESULTADO_SIMULADO_OK
    assert spy.arns_chamados == [arn]


def test_falha_na_revalidacao_nao_bloqueia_tentativa_de_tagueamento(fake_session_factory):
    """Se a leitura de revalidação falhar (ex.: `AccessDenied` na permissão
    de leitura), o recurso não fica travado — a tentativa principal segue
    normalmente, só sem o benefício da revalidação para este recurso."""
    arn = "arn:aws:s3:::bucket-sem-permissao-leitura"
    session = fake_session_factory({"resourcegroupstaggingapi": _FakeTaggingClient(erro=True)})
    spy = _SpyExecutor()

    resultado = tag_execution.run_stage2b(
        _relatorio_um_recurso_generico(arn), session=session, expected_tag_value=TAG_VALUE, revalidate=True, executor=spy
    )

    assert resultado["recursos"][0]["resultado"] == tag_execution.RESULTADO_SIMULADO_OK
    assert spy.arns_chamados == [arn]


def test_revalidacao_dedicada_eks_pula_recurso_ja_tagueado(fake_session_factory, decisao_factory, relatorio_decisao_factory):
    arn = "arn:eks:cluster-ja-tagueado"
    session = fake_session_factory({"eks": _FakeEksClient({arn: TAG_VALUE})})
    spy = _SpyExecutor()
    relatorio = relatorio_decisao_factory(
        [decisao_factory(arn=arn, servico="Amazon EKS", tipo_recurso="cluster")]
    )

    resultado = tag_execution.run_stage2b(
        relatorio, session=session, expected_tag_value=TAG_VALUE, revalidate=True, executor=spy
    )

    assert resultado["recursos"][0]["resultado"] == tag_execution.RESULTADO_JA_TAGUEADO
    assert spy.arns_chamados == []


def test_reexecucao_do_mesmo_relatorio_e_idempotente(fake_session_factory):
    """Simula rodar a Etapa 2b duas vezes sobre o MESMO relatório de decisão
    — a segunda vez, o estado da AWS já reflete o resultado da primeira (via
    o fake client), e o recurso passa a ser pulado como `ja_tagueado` em vez
    de gerar uma nova tentativa."""
    arn = "arn:aws:s3:::bucket-reexecucao"
    relatorio = _relatorio_um_recurso_generico(arn)

    session_primeira_execucao = fake_session_factory({"resourcegroupstaggingapi": _FakeTaggingClient({})})
    spy1 = _SpyExecutor()
    primeira = tag_execution.run_stage2b(
        relatorio, session=session_primeira_execucao, expected_tag_value=TAG_VALUE, revalidate=True, executor=spy1
    )
    assert primeira["recursos"][0]["resultado"] == tag_execution.RESULTADO_SIMULADO_OK
    assert spy1.arns_chamados == [arn]

    session_segunda_execucao = fake_session_factory({"resourcegroupstaggingapi": _FakeTaggingClient({arn: TAG_VALUE})})
    spy2 = _SpyExecutor()
    segunda = tag_execution.run_stage2b(
        relatorio, session=session_segunda_execucao, expected_tag_value=TAG_VALUE, revalidate=True, executor=spy2
    )
    assert segunda["recursos"][0]["resultado"] == tag_execution.RESULTADO_JA_TAGUEADO
    assert spy2.arns_chamados == []


def test_formato_do_relatorio_de_saida():
    relatorio = _relatorio_um_recurso_generico("arn:aws:s3:::bucket-formato")
    resultado = tag_execution.run_stage2b(relatorio, session=None, expected_tag_value=TAG_VALUE, revalidate=False)

    assert resultado["modo"] == "dry_run"
    assert resultado["conta_id"] == "000000000000"
    assert resultado["valor_tag_esperado"] == TAG_VALUE
    assert resultado["resumo"]["total_recursos_processados"] == 1
    assert resultado["resumo"]["por_resultado"] == {tag_execution.RESULTADO_SIMULADO_OK: 1}
    assert resultado["resumo"]["por_estrategia_api"] == {tag_execution.ApiStrategy.GENERICO.value: 1}
    assert "executado_em" in resultado


def test_relatorio_de_decisao_sem_recursos_taguear_produz_relatorio_vazio(relatorio_decisao_factory, decisao_factory):
    relatorio = relatorio_decisao_factory([decisao_factory(decisao="ja_ok")])
    resultado = tag_execution.run_stage2b(relatorio, session=None, expected_tag_value=TAG_VALUE, revalidate=False)
    assert resultado["recursos"] == []
    assert resultado["resumo"]["total_recursos_processados"] == 0
