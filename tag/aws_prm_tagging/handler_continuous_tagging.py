"""Handler Lambda da Etapa 3 (automação contínua via EventBridge).

Invocado pelo Step Functions depois do delay de debounce contra corrida com
IaC (ver `infra/statemachine/etapa3_delay_e_execucao.asl.json` e
[docs/arquitetura.md](docs/arquitetura.md)) — não diretamente pela regra do
EventBridge.

Só orquestração de I/O (variáveis de ambiente, sessão boto3, publicação do
resultado) — toda a lógica de negócio é reaproveitada sem duplicação dos
módulos de núcleo já usados pelas Etapas 1-2c:

1. `event_parser.parse_creation_event` — extrai 0, 1 ou N recursos do
   evento (algumas APIs de criação são de lote, ex.: `RunInstances`).
2. `single_resource.build_resource_from_arn` — lê o estado ATUAL de cada
   recurso (a tag pode ter mudado entre o evento e este processamento).
3. `decision.classify_resource` — Etapa 2a, chamada direta, sem nenhuma
   regra de precedência reimplementada aqui.
4. `tag_execution.run_tagging_execution(..., dry_run=False)` — Etapa 2c,
   chamada direta. Independente da decisão ser `taguear` ou não, esta
   função já monta o relatório final corretamente nos dois casos (`taguear`
   passa pela revalidação/escrita real; qualquer outra decisão é só
   carregada para o relatório final) — não há necessidade de nenhum `if`
   especial aqui para decisões que não são `taguear`.

Variáveis de ambiente esperadas: `EXPECTED_TAG_VALUE` (o valor esperado da
tag `aws-apn-id` para esta conta/sub-OU — StackSet parameter, nunca
hardcoded) e `SNS_TOPIC_ARN` (tópico de relatório — ver `publish.py`).
"""
from __future__ import annotations

import logging
import os

import boto3
from botocore.exceptions import ClientError

from . import decision, event_parser, publish, single_resource, tag_execution

logger = logging.getLogger("aws_prm_tagging.etapa3")

_ETAPA = "3"


def _process_one_resource(
    session: boto3.Session,
    sns_client,
    topic_arn: str,
    conta_id: str | None,
    extracted: event_parser.ExtractedResource,
    expected_tag_value: str,
    event_id: str | None,
    event_name: str | None,
    event_source: str | None,
) -> None:
    try:
        resource = single_resource.build_resource_from_arn(
            session,
            extracted.arn,
            extracted.servico,
            extracted.regiao,
            expected_tag_value,
            extracted.tipo_recurso,
        )
    except ClientError as exc:
        # A leitura inicial já passou pelo backoff de `retry.py` — se ainda
        # assim falhou (ex.: AccessDenied, ou o recurso nunca chegou a
        # ficar visível), publica como falha e para por aqui: sem saber o
        # estado atual do recurso, não há decisão segura a tomar (mesmo
        # princípio da revalidação em tag_execution.py).
        erro = exc.response.get("Error", {})
        logger.exception(
            "Falha ao ler o estado atual de %s — publicando como falha, sem tentar taguear", extracted.arn
        )
        publish.publish_outcome(
            sns_client,
            topic_arn,
            publish.build_outcome_payload(
                etapa=_ETAPA,
                conta_id=conta_id,
                arn=extracted.arn,
                servico=extracted.servico,
                regiao=extracted.regiao,
                tipo_recurso=extracted.tipo_recurso,
                categoria_final="falhou",
                origem="leitura_inicial",
                resultado="erro_leitura_inicial",
                detalhe_erro={"codigo": erro.get("Code", ""), "mensagem": erro.get("Message", str(exc))},
                event_id=event_id,
                event_name=event_name,
                event_source=event_source,
            ),
        )
        return

    decisao_resultado = decision.classify_resource(resource, expected_tag_value)
    if decisao_resultado is None:
        # Fora de escopo (tipo_recurso inesperado para EKS/Bedrock) — checagem
        # defensiva, não deveria ocorrer com o tipo_recurso que os próprios
        # extractors já atribuem. Mesmo comportamento de
        # `decision.build_decision_report` para recursos fora de escopo: só
        # contabilizado/logado, não publicado como um resultado individual.
        logger.warning("Recurso fora de escopo desta automação, ignorado: %s", extracted.arn)
        return

    tem_erro_classificacao = "erro" in decisao_resultado
    decision_report = {
        "conta_id": conta_id,
        "valor_tag_esperado": expected_tag_value,
        "recursos": [] if tem_erro_classificacao else [decisao_resultado],
        "erros": [decisao_resultado] if tem_erro_classificacao else [],
    }

    # Etapa 2c real — já revalida o estado do recurso imediatamente antes de
    # escrever, já é idempotente, já roteia pela API certa e já classifica
    # erro. Funciona igual para decisao == "taguear" (passa pela
    # revalidação/escrita) e para qualquer outra decisão ou erro de
    # classificação (só é carregada para o relatório final) — sem
    # necessidade de nenhum caminho especial aqui.
    execution_report = tag_execution.run_tagging_execution(
        decision_report,
        session=session,
        expected_tag_value=expected_tag_value,
        dry_run=False,
        revalidate=True,
    )
    resultado_final = execution_report["recursos"][0]
    publish.publish_outcome(
        sns_client,
        topic_arn,
        publish.build_outcome_payload(
            etapa=_ETAPA,
            conta_id=conta_id,
            arn=resultado_final["arn"],
            servico=resultado_final["servico"],
            regiao=resultado_final["regiao"],
            tipo_recurso=resultado_final["tipo_recurso"],
            categoria_final=resultado_final["categoria_final"],
            origem=resultado_final["origem"],
            estrategia_api=resultado_final["estrategia_api"],
            resultado=resultado_final["resultado"],
            detalhe_erro=resultado_final["detalhe_erro"],
            motivo=resultado_final["motivo"],
            event_id=event_id,
            event_name=event_name,
            event_source=event_source,
        ),
    )


def handler(event: dict, context=None, *, session: boto3.Session | None = None, sns_client=None) -> None:
    """`session`/`sns_client` são injetáveis só para teste (ver
    `test_handler_continuous_tagging.py` e `fake_session_factory` em
    `conftest.py`) — em produção, a Lambda sempre invoca com só
    `(event, context)`, e os dois são construídos a partir da execution
    role da própria função."""
    expected_tag_value = os.environ["EXPECTED_TAG_VALUE"]
    topic_arn = os.environ["SNS_TOPIC_ARN"]

    detail = event.get("detail") or {}
    event_id = detail.get("eventID") or event.get("id")
    event_name = detail.get("eventName")
    event_source = detail.get("eventSource")
    conta_id = detail.get("recipientAccountId") or event.get("account")

    extracted_resources = event_parser.parse_creation_event(event)
    if not extracted_resources:
        # Falha de extração (payload insuficiente) ou evento fora do
        # mapeamento — a decisão de alertar ativamente via SNS fica para
        # depois (ver docs/melhorias-futuras.md); por ora só loga. A Etapa
        # 4 (varredura periódica, fora do escopo deste módulo) é o backstop
        # para qualquer recurso perdido aqui.
        logger.warning(
            "Não foi possível extrair nenhum recurso do evento %s (%s/%s)", event_id, event_source, event_name
        )
        return

    session = session or boto3.Session()
    sns_client = sns_client or session.client("sns")

    falhas_inesperadas = 0
    for extracted in extracted_resources:
        try:
            _process_one_resource(
                session,
                sns_client,
                topic_arn,
                conta_id,
                extracted,
                expected_tag_value,
                event_id,
                event_name,
                event_source,
            )
        except Exception:
            # Uma falha inesperada num recurso não deve impedir o
            # processamento dos demais recursos do mesmo evento (ex.: um
            # RunInstances que criou 3 instâncias) — mesmo princípio já
            # usado em main.py/resource_discovery.py. Recontabilizado no
            # final para marcar a execução do Step Functions como falha
            # (visibilidade operacional), sem abortar o loop no meio.
            logger.exception(
                "Falha inesperada ao processar %s — continuando com os demais recursos do evento", extracted.arn
            )
            falhas_inesperadas += 1

    if falhas_inesperadas:
        raise RuntimeError(
            f"{falhas_inesperadas} de {len(extracted_resources)} recurso(s) deste evento falharam "
            "de forma inesperada — ver logs acima para detalhe por recurso."
        )
