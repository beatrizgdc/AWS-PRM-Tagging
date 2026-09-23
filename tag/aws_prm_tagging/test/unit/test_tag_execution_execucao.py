"""`tag_execution.run_tagging_execution` — revalidação, idempotência, modo
dry-run/live e formato do relatório de saída (Etapas 2b/2c).

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


# Tag de convenção que `iac_detection.py` reconhece como sinal de
# CloudFormation/CDK (`_CFN_STACK_NAME_TAG`, privada naquele módulo) — usada
# aqui só para simular, nos fakes, um recurso gerenciado por IaC.
_TAG_CLOUDFORMATION = "aws:cloudformation:stack-name"


class _FakeTaggingClient:
    """Stub de `resourcegroupstaggingapi` — só `get_resources`, usado na
    revalidação do caminho genérico. `tagged` é o atalho comum (só o valor
    da aws-apn-id); `tags_completos` permite simular o conjunto completo de
    tags de um recurso (ex.: para os cenários de IaC/conflito detectados só
    na revalidação) — os dois podem ser combinados."""

    def __init__(
        self,
        tagged: dict[str, str] | None = None,
        tags_completos: dict[str, dict[str, str]] | None = None,
        erro: bool = False,
    ):
        self._tags_por_arn: dict[str, dict[str, str]] = {
            arn: dict(tags) for arn, tags in (tags_completos or {}).items()
        }
        for arn, valor in (tagged or {}).items():
            self._tags_por_arn.setdefault(arn, {})[tag_execution.TAG_KEY] = valor
        self._erro = erro

    def get_resources(self, **kwargs):
        if self._erro:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "sem permissão"}}, "GetResources")
        return {
            "ResourceTagMappingList": [
                {"ResourceARN": arn, "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}
                for arn, tags in self._tags_por_arn.items()
            ]
        }


class _FakeEksClient:
    def __init__(self, tagged: dict[str, str] | None = None, tags_completos: dict[str, dict[str, str]] | None = None):
        self._tags_por_arn: dict[str, dict[str, str]] = {
            arn: dict(tags) for arn, tags in (tags_completos or {}).items()
        }
        for arn, valor in (tagged or {}).items():
            self._tags_por_arn.setdefault(arn, {})[tag_execution.TAG_KEY] = valor

    def list_tags_for_resource(self, resourceArn):
        return {"tags": self._tags_por_arn.get(resourceArn, {})}


class _SpyExecutor:
    def __init__(self):
        self.arns_chamados: list[str] = []

    def tag_generic_batch(self, session, regiao, arns, tag_key, tag_value):
        self.arns_chamados.extend(arns)
        return {arn: tag_execution.ResourceOutcome(resultado=tag_execution.RESULTADO_SIMULADO_OK) for arn in arns}

    def tag_single(self, session, estrategia, resource, tag_key, tag_value):
        self.arns_chamados.append(resource.arn)
        return tag_execution.ResourceOutcome(resultado=tag_execution.RESULTADO_SIMULADO_OK)


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
    resultado = tag_execution.run_tagging_execution(relatorio, session=None, expected_tag_value=TAG_VALUE, revalidate=False)
    assert resultado["recursos"][0]["resultado"] == tag_execution.RESULTADO_SIMULADO_OK


def test_revalidacao_pula_recurso_ja_tagueado_generico(fake_session_factory):
    """Recurso já tem `aws-apn-id` com o valor esperado (ex.: tagueado numa
    execução anterior) -> revalidação encontra e o orquestrador marca
    `ja_tagueado` sem chamar o executor — é isso que torna reexecuções
    idempotentes (ver docstring do módulo)."""
    arn = "arn:aws:s3:::bucket-ja-tagueado"
    session = fake_session_factory({"resourcegroupstaggingapi": _FakeTaggingClient({arn: TAG_VALUE})})
    spy = _SpyExecutor()

    resultado = tag_execution.run_tagging_execution(
        _relatorio_um_recurso_generico(arn), session=session, expected_tag_value=TAG_VALUE, revalidate=True, executor=spy
    )

    assert resultado["recursos"][0]["resultado"] == tag_execution.RESULTADO_JA_TAGUEADO
    assert spy.arns_chamados == []


def test_revalidacao_prossegue_para_recurso_ainda_sem_tag(fake_session_factory):
    arn = "arn:aws:s3:::bucket-pendente"
    session = fake_session_factory({"resourcegroupstaggingapi": _FakeTaggingClient({})})
    spy = _SpyExecutor()

    resultado = tag_execution.run_tagging_execution(
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

    resultado = tag_execution.run_tagging_execution(
        _relatorio_um_recurso_generico(arn), session=session, expected_tag_value=TAG_VALUE, revalidate=True, executor=spy
    )

    assert resultado["recursos"][0]["resultado"] == tag_execution.RESULTADO_SIMULADO_OK
    assert spy.arns_chamados == [arn]


def test_revalidacao_generica_detecta_conflito_e_nunca_tenta_escrever(fake_session_factory):
    """Regressão do bug corrigido: antes, a revalidação só checava se o
    valor batia com o esperado — um valor DIFERENTE encontrado na
    revalidação caía no mesmo balaio de "sem tag" e disparava uma tentativa
    de escrita, que teria sobrescrito um conflito automaticamente (proibido
    pela regra de negócio). Agora precisa virar `conflito_na_revalidacao`
    e nunca chamar o executor."""
    arn = "arn:aws:s3:::bucket-conflito-surgiu-depois"
    session = fake_session_factory(
        {"resourcegroupstaggingapi": _FakeTaggingClient(tagged={arn: "pc:outro-parceiro"})}
    )
    spy = _SpyExecutor()

    resultado = tag_execution.run_tagging_execution(
        _relatorio_um_recurso_generico(arn), session=session, expected_tag_value=TAG_VALUE, revalidate=True, executor=spy
    )

    assert resultado["recursos"][0]["resultado"] == tag_execution.RESULTADO_CONFLITO_NA_REVALIDACAO
    assert spy.arns_chamados == []


def test_revalidacao_generica_detecta_iac_e_nunca_tenta_escrever(fake_session_factory):
    """Recurso sem a tag aws-apn-id, mas que passou a ter a tag de
    convenção do CloudFormation entre a Etapa 1/2a e esta execução — a
    revalidação precisa pegar isso (não só a Etapa 1 original) e nunca
    tentar taguear via API um recurso gerenciado por IaC."""
    arn = "arn:aws:s3:::bucket-virou-iac-depois"
    session = fake_session_factory(
        {"resourcegroupstaggingapi": _FakeTaggingClient(tags_completos={arn: {_TAG_CLOUDFORMATION: "minha-stack"}})}
    )
    spy = _SpyExecutor()

    resultado = tag_execution.run_tagging_execution(
        _relatorio_um_recurso_generico(arn), session=session, expected_tag_value=TAG_VALUE, revalidate=True, executor=spy
    )

    assert resultado["recursos"][0]["resultado"] == tag_execution.RESULTADO_IAC_DETECTADO_NA_REVALIDACAO
    assert spy.arns_chamados == []


def test_revalidacao_generica_conflito_tem_precedencia_sobre_iac(fake_session_factory):
    """Mesma regra de precedência de `decision.py`: quando a tag está
    PRESENTE com valor diferente, IaC é só metadado — nunca muda o
    resultado para `pulado_iac`. Só entra como IaC quando a tag está
    ausente."""
    arn = "arn:aws:s3:::bucket-conflito-e-iac"
    session = fake_session_factory(
        {
            "resourcegroupstaggingapi": _FakeTaggingClient(
                tags_completos={arn: {tag_execution.TAG_KEY: "pc:outro-parceiro", _TAG_CLOUDFORMATION: "minha-stack"}}
            )
        }
    )
    spy = _SpyExecutor()

    resultado = tag_execution.run_tagging_execution(
        _relatorio_um_recurso_generico(arn), session=session, expected_tag_value=TAG_VALUE, revalidate=True, executor=spy
    )

    assert resultado["recursos"][0]["resultado"] == tag_execution.RESULTADO_CONFLITO_NA_REVALIDACAO
    assert spy.arns_chamados == []


def test_revalidacao_dedicada_eks_detecta_conflito_e_nunca_tenta_escrever(
    fake_session_factory, decisao_factory, relatorio_decisao_factory
):
    arn = "arn:eks:cluster-conflito-surgiu-depois"
    session = fake_session_factory({"eks": _FakeEksClient(tagged={arn: "pc:outro-parceiro"})})
    spy = _SpyExecutor()
    relatorio = relatorio_decisao_factory(
        [decisao_factory(arn=arn, servico="Amazon EKS", tipo_recurso="cluster")]
    )

    resultado = tag_execution.run_tagging_execution(
        relatorio, session=session, expected_tag_value=TAG_VALUE, revalidate=True, executor=spy
    )

    assert resultado["recursos"][0]["resultado"] == tag_execution.RESULTADO_CONFLITO_NA_REVALIDACAO
    assert spy.arns_chamados == []


def test_revalidacao_dedicada_eks_pula_recurso_ja_tagueado(fake_session_factory, decisao_factory, relatorio_decisao_factory):
    arn = "arn:eks:cluster-ja-tagueado"
    session = fake_session_factory({"eks": _FakeEksClient({arn: TAG_VALUE})})
    spy = _SpyExecutor()
    relatorio = relatorio_decisao_factory(
        [decisao_factory(arn=arn, servico="Amazon EKS", tipo_recurso="cluster")]
    )

    resultado = tag_execution.run_tagging_execution(
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
    primeira = tag_execution.run_tagging_execution(
        relatorio, session=session_primeira_execucao, expected_tag_value=TAG_VALUE, revalidate=True, executor=spy1
    )
    assert primeira["recursos"][0]["resultado"] == tag_execution.RESULTADO_SIMULADO_OK
    assert spy1.arns_chamados == [arn]

    session_segunda_execucao = fake_session_factory({"resourcegroupstaggingapi": _FakeTaggingClient({arn: TAG_VALUE})})
    spy2 = _SpyExecutor()
    segunda = tag_execution.run_tagging_execution(
        relatorio, session=session_segunda_execucao, expected_tag_value=TAG_VALUE, revalidate=True, executor=spy2
    )
    assert segunda["recursos"][0]["resultado"] == tag_execution.RESULTADO_JA_TAGUEADO
    assert spy2.arns_chamados == []


def test_formato_do_relatorio_de_saida():
    relatorio = _relatorio_um_recurso_generico("arn:aws:s3:::bucket-formato")
    resultado = tag_execution.run_tagging_execution(relatorio, session=None, expected_tag_value=TAG_VALUE, revalidate=False)

    assert resultado["modo"] == "dry_run"
    assert resultado["execucao_inicial"] is False
    assert resultado["conta_id"] == "000000000000"
    assert resultado["valor_tag_esperado"] == TAG_VALUE
    assert resultado["resumo"]["total_recursos"] == 1
    assert resultado["resumo"]["por_categoria_final"][tag_execution.CATEGORIA_TAGUEADO_SUCESSO] == 1
    assert resultado["resumo"]["por_resultado"] == {tag_execution.RESULTADO_SIMULADO_OK: 1}
    assert resultado["resumo"]["por_estrategia_api"] == {tag_execution.ApiStrategy.GENERICO.value: 1}
    assert resultado["recursos"][0]["categoria_final"] == tag_execution.CATEGORIA_TAGUEADO_SUCESSO
    assert resultado["recursos"][0]["origem"] == "execucao"
    assert "executado_em" in resultado


def test_modo_live_aparece_no_relatorio_quando_dry_run_false():
    relatorio = _relatorio_um_recurso_generico("arn:aws:s3:::bucket-live")
    spy = _SpyExecutor()
    resultado = tag_execution.run_tagging_execution(
        relatorio, session=None, expected_tag_value=TAG_VALUE, revalidate=False, dry_run=False, executor=spy
    )
    assert resultado["modo"] == "live"


def test_flag_execucao_inicial_repassada_ao_relatorio():
    """Sinal de observabilidade (ver docstring do módulo) — nunca bloqueia,
    só aparece no relatório para quem for revisar a primeira execução de
    uma conta (disparada pelo Custom Resource no `Create` da stack)."""
    relatorio = _relatorio_um_recurso_generico("arn:aws:s3:::bucket-primeira-execucao")
    resultado = tag_execution.run_tagging_execution(
        relatorio, session=None, expected_tag_value=TAG_VALUE, revalidate=False, execucao_inicial=True
    )
    assert resultado["execucao_inicial"] is True


def test_relatorio_final_mescla_recursos_pular_iac_ja_ok_e_conflito_da_etapa_2a(
    relatorio_decisao_factory, decisao_factory
):
    """O relatório final não é só o subconjunto `taguear` — cobre as 5
    categorias esperadas pela Etapa 2c, carregando direto os recursos que a
    Etapa 2a já classificou como `pular_iac`/`ja_ok`/`conflito` (nunca
    passam por `select_taggable`, nunca geram chamada nenhuma)."""
    relatorio = relatorio_decisao_factory(
        [
            decisao_factory(arn="arn:pular-iac", decisao="pular_iac", servico="Amazon EC2"),
            decisao_factory(arn="arn:ja-ok", decisao="ja_ok", servico="Amazon S3"),
            decisao_factory(arn="arn:conflito", decisao="conflito", servico="Amazon RDS"),
        ]
    )
    resultado = tag_execution.run_tagging_execution(relatorio, session=None, expected_tag_value=TAG_VALUE, revalidate=False)

    assert resultado["resumo"]["total_recursos"] == 3
    por_arn = {r["arn"]: r for r in resultado["recursos"]}
    assert por_arn["arn:pular-iac"]["categoria_final"] == tag_execution.CATEGORIA_PULADO_IAC
    assert por_arn["arn:ja-ok"]["categoria_final"] == tag_execution.CATEGORIA_JA_OK
    assert por_arn["arn:conflito"]["categoria_final"] == tag_execution.CATEGORIA_CONFLITO
    for entrada in por_arn.values():
        assert entrada["origem"] == "decisao"
        assert entrada["resultado"] is None  # nunca passou por este módulo
        assert entrada["estrategia_api"] is None
    # por_resultado/por_estrategia_api (granularidade fina) não contam
    # entradas de origem "decisao" — elas nunca tiveram resultado/estratégia.
    assert resultado["resumo"]["por_resultado"] == {}
    assert resultado["resumo"]["por_estrategia_api"] == {}
    assert resultado["resumo"]["por_categoria_final"] == {
        tag_execution.CATEGORIA_TAGUEADO_SUCESSO: 0,
        tag_execution.CATEGORIA_FALHOU: 0,
        tag_execution.CATEGORIA_PULADO_IAC: 1,
        tag_execution.CATEGORIA_CONFLITO: 1,
        tag_execution.CATEGORIA_JA_OK: 1,
    }


def test_relatorio_final_mescla_taguear_com_as_outras_categorias(relatorio_decisao_factory, decisao_factory):
    """Um relatório de decisão real tem as 4 categorias juntas — confere que
    o merge cobre isso sem perder nenhuma."""
    relatorio = relatorio_decisao_factory(
        [
            decisao_factory(arn="arn:taguear", decisao="taguear", servico="Amazon S3"),
            decisao_factory(arn="arn:pular-iac", decisao="pular_iac", servico="Amazon EC2"),
            decisao_factory(arn="arn:ja-ok", decisao="ja_ok", servico="Amazon S3"),
            decisao_factory(arn="arn:conflito", decisao="conflito", servico="Amazon RDS"),
        ]
    )
    resultado = tag_execution.run_tagging_execution(relatorio, session=None, expected_tag_value=TAG_VALUE, revalidate=False)

    assert resultado["resumo"]["total_recursos"] == 4
    por_arn = {r["arn"]: r for r in resultado["recursos"]}
    assert por_arn["arn:taguear"]["categoria_final"] == tag_execution.CATEGORIA_TAGUEADO_SUCESSO
    assert por_arn["arn:taguear"]["origem"] == "execucao"
    assert por_arn["arn:taguear"]["resultado"] == tag_execution.RESULTADO_SIMULADO_OK


def test_relatorio_de_decisao_sem_recursos_taguear_ainda_mostra_o_recurso(relatorio_decisao_factory, decisao_factory):
    """Diferente do comportamento antigo (que só reportava o subconjunto
    `taguear`), um relatório de decisão só com `ja_ok` agora aparece no
    relatório final — é exatamente esse recurso que a Etapa 4 precisa
    enxergar."""
    relatorio = relatorio_decisao_factory([decisao_factory(arn="arn:ja-ok-unico", decisao="ja_ok")])
    resultado = tag_execution.run_tagging_execution(relatorio, session=None, expected_tag_value=TAG_VALUE, revalidate=False)
    assert len(resultado["recursos"]) == 1
    assert resultado["recursos"][0]["categoria_final"] == tag_execution.CATEGORIA_JA_OK
    assert resultado["resumo"]["total_recursos"] == 1


def test_relatorio_de_decisao_totalmente_vazio_produz_relatorio_vazio(relatorio_decisao_factory):
    resultado = tag_execution.run_tagging_execution(
        relatorio_decisao_factory([]), session=None, expected_tag_value=TAG_VALUE, revalidate=False
    )
    assert resultado["recursos"] == []
    assert resultado["resumo"]["total_recursos"] == 0


# ---------------------------------------------------------------------------
# LiveExecutor (Etapa 2c) — chamadas de escrita reais
# ---------------------------------------------------------------------------


class _FakeTagResourcesClient:
    """Stub de `resourcegroupstaggingapi.tag_resources` — `falhas` simula
    `FailedResourcesMap` (falha parcial dentro de um lote "bem sucedido" no
    nível HTTP); `erro` simula a chamada inteira falhando."""

    def __init__(self, falhas: dict[str, dict] | None = None, erro: bool = False):
        self._falhas = falhas or {}
        self._erro = erro
        self.chamadas: list[tuple[list[str], dict]] = []

    def tag_resources(self, ResourceARNList, Tags):
        self.chamadas.append((list(ResourceARNList), dict(Tags)))
        if self._erro:
            raise ClientError({"Error": {"Code": "ValidationException", "Message": "limite excedido"}}, "TagResources")
        return {"FailedResourcesMap": self._falhas}


class _FakeEksWriteClient:
    def __init__(self, erro_codigo: str | None = None):
        self._erro_codigo = erro_codigo
        self.chamadas: list[tuple[str, dict]] = []

    def tag_resource(self, resourceArn, tags):
        self.chamadas.append((resourceArn, dict(tags)))
        if self._erro_codigo:
            raise ClientError({"Error": {"Code": self._erro_codigo, "Message": "erro simulado"}}, "TagResource")


def test_live_executor_generico_sucesso(fake_session_factory):
    client = _FakeTagResourcesClient()
    session = fake_session_factory({"resourcegroupstaggingapi": client})
    relatorio = _relatorio_um_recurso_generico("arn:aws:s3:::bucket-live-sucesso")

    resultado = tag_execution.run_tagging_execution(
        relatorio, session=session, expected_tag_value=TAG_VALUE, revalidate=False, dry_run=False
    )

    assert resultado["modo"] == "live"
    assert resultado["recursos"][0]["resultado"] == tag_execution.RESULTADO_TAGUEADO_SUCESSO
    assert resultado["recursos"][0]["categoria_final"] == tag_execution.CATEGORIA_TAGUEADO_SUCESSO
    assert client.chamadas == [(["arn:aws:s3:::bucket-live-sucesso"], {tag_execution.TAG_KEY: TAG_VALUE})]


def test_live_executor_generico_falha_parcial_de_lote(fake_session_factory, relatorio_decisao_factory, decisao_factory):
    """`FailedResourcesMap` com sucesso HTTP: só os ARNs listados viram
    falha, os demais do mesmo lote viram sucesso."""
    client = _FakeTagResourcesClient(
        falhas={"arn:falhou": {"ErrorCode": "AccessDeniedException", "ErrorMessage": "sem permissão"}}
    )
    session = fake_session_factory({"resourcegroupstaggingapi": client})
    relatorio = relatorio_decisao_factory(
        [
            decisao_factory(arn="arn:sucesso", servico="Amazon S3", regiao="us-east-1"),
            decisao_factory(arn="arn:falhou", servico="Amazon S3", regiao="us-east-1"),
        ]
    )

    resultado = tag_execution.run_tagging_execution(
        relatorio, session=session, expected_tag_value=TAG_VALUE, revalidate=False, dry_run=False
    )

    por_arn = {r["arn"]: r for r in resultado["recursos"]}
    assert por_arn["arn:sucesso"]["resultado"] == tag_execution.RESULTADO_TAGUEADO_SUCESSO
    assert por_arn["arn:falhou"]["resultado"] == tag_execution.RESULTADO_ERRO_PERMISSAO
    assert por_arn["arn:falhou"]["categoria_final"] == tag_execution.CATEGORIA_FALHOU
    assert por_arn["arn:falhou"]["detalhe_erro"] == {"codigo": "AccessDeniedException", "mensagem": "sem permissão"}


def test_live_executor_generico_chamada_inteira_falha_marca_todo_o_lote(fake_session_factory, relatorio_decisao_factory, decisao_factory):
    client = _FakeTagResourcesClient(erro=True)
    session = fake_session_factory({"resourcegroupstaggingapi": client})
    relatorio = relatorio_decisao_factory(
        [
            decisao_factory(arn="arn:a", servico="Amazon S3", regiao="us-east-1"),
            decisao_factory(arn="arn:b", servico="Amazon S3", regiao="us-east-1"),
        ]
    )

    resultado = tag_execution.run_tagging_execution(
        relatorio, session=session, expected_tag_value=TAG_VALUE, revalidate=False, dry_run=False
    )

    for r in resultado["recursos"]:
        assert r["resultado"] == tag_execution.RESULTADO_ERRO
        assert r["detalhe_erro"]["codigo"] == "ValidationException"


def test_live_executor_falha_num_lote_nao_impede_o_proximo_lote(fake_session_factory, relatorio_decisao_factory, decisao_factory):
    """25 ARNs na mesma região viram 2 lotes (20 + 5). O client falha em
    toda chamada — confirma que o segundo lote ainda é TENTADO (a chamada
    de `tag_resources` acontece 2 vezes) mesmo com o primeiro já tendo
    falhado, em vez de abortar o resto da execução na primeira falha."""
    client = _FakeTagResourcesClient(erro=True)
    session = fake_session_factory({"resourcegroupstaggingapi": client})
    relatorio = relatorio_decisao_factory(
        [decisao_factory(arn=f"arn:{i}", servico="Amazon S3", regiao="us-east-1") for i in range(25)]
    )

    resultado = tag_execution.run_tagging_execution(
        relatorio, session=session, expected_tag_value=TAG_VALUE, revalidate=False, dry_run=False
    )

    assert len(client.chamadas) == 2
    assert all(r["resultado"] == tag_execution.RESULTADO_ERRO for r in resultado["recursos"])


def test_live_executor_dedicado_eks_sucesso(fake_session_factory, relatorio_decisao_factory, decisao_factory):
    client = _FakeEksWriteClient()
    session = fake_session_factory({"eks": client})
    relatorio = relatorio_decisao_factory(
        [decisao_factory(arn="arn:cluster-live", servico="Amazon EKS", tipo_recurso="cluster")]
    )

    resultado = tag_execution.run_tagging_execution(
        relatorio, session=session, expected_tag_value=TAG_VALUE, revalidate=False, dry_run=False
    )

    assert resultado["recursos"][0]["resultado"] == tag_execution.RESULTADO_TAGUEADO_SUCESSO
    assert client.chamadas == [("arn:cluster-live", {tag_execution.TAG_KEY: TAG_VALUE})]


def test_live_executor_dedicado_recurso_nao_encontrado(fake_session_factory, relatorio_decisao_factory, decisao_factory):
    """Recurso sumiu entre a Etapa 1 e a execução real — categoria própria
    (`recurso_nao_encontrado`), não confundida com falha de permissão."""
    client = _FakeEksWriteClient(erro_codigo="ClusterNotFoundException")
    session = fake_session_factory({"eks": client})
    relatorio = relatorio_decisao_factory(
        [decisao_factory(arn="arn:cluster-sumiu", servico="Amazon EKS", tipo_recurso="cluster")]
    )

    resultado = tag_execution.run_tagging_execution(
        relatorio, session=session, expected_tag_value=TAG_VALUE, revalidate=False, dry_run=False
    )

    assert resultado["recursos"][0]["resultado"] == tag_execution.RESULTADO_RECURSO_NAO_ENCONTRADO
    assert resultado["recursos"][0]["categoria_final"] == tag_execution.CATEGORIA_FALHOU
    assert resultado["recursos"][0]["detalhe_erro"]["codigo"] == "ClusterNotFoundException"


# ---------------------------------------------------------------------------
# Idade máxima do relatório de decisão
# ---------------------------------------------------------------------------


def test_idade_maxima_nao_verificada_quando_parametro_omitido():
    relatorio = _relatorio_um_recurso_generico("arn:aws:s3:::bucket-sem-check-idade")
    relatorio["descoberta_executada_em"] = "2000-01-01T00:00:00Z"  # bem velho
    resultado = tag_execution.run_tagging_execution(relatorio, session=None, expected_tag_value=TAG_VALUE, revalidate=False)
    assert resultado["resumo"]["total_recursos"] == 1


def test_idade_maxima_recusa_relatorio_velho_demais():
    relatorio = _relatorio_um_recurso_generico("arn:aws:s3:::bucket-velho")
    relatorio["descoberta_executada_em"] = "2000-01-01T00:00:00Z"
    try:
        tag_execution.run_tagging_execution(
            relatorio, session=None, expected_tag_value=TAG_VALUE, revalidate=False, max_decision_age_hours=24
        )
        assert False, "deveria ter levantado DecisionReportDesatualizadoError"
    except tag_execution.DecisionReportDesatualizadoError:
        pass


def test_idade_maxima_aceita_relatorio_recente():
    from datetime import datetime, timezone

    agora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    relatorio = _relatorio_um_recurso_generico("arn:aws:s3:::bucket-recente")
    relatorio["descoberta_executada_em"] = agora
    resultado = tag_execution.run_tagging_execution(
        relatorio, session=None, expected_tag_value=TAG_VALUE, revalidate=False, max_decision_age_hours=24
    )
    assert resultado["resumo"]["total_recursos"] == 1


def test_idade_maxima_recusa_relatorio_sem_o_campo():
    """Relatório de decisão sem `descoberta_executada_em` (ex.: gerado por
    uma versão antiga de `decision.py`) — não dá pra verificar a idade,
    então recusa por segurança em vez de assumir que está tudo bem."""
    relatorio = _relatorio_um_recurso_generico("arn:aws:s3:::bucket-sem-campo")
    try:
        tag_execution.run_tagging_execution(
            relatorio, session=None, expected_tag_value=TAG_VALUE, revalidate=False, max_decision_age_hours=24
        )
        assert False, "deveria ter levantado DecisionReportDesatualizadoError"
    except tag_execution.DecisionReportDesatualizadoError:
        pass
