"""Lambda da Etapa 3 (reação a recursos novos).

Invocada pelo Step Functions depois do debounce contra corrida com IaC
(`infra/statemachine/etapa3_delay_e_execucao.asl.json`), não direto pela
regra do EventBridge. Só faz I/O; a lógica vem dos módulos das Etapas 1-2c:

1. `event_parser.parse_creation_event`: 0, 1 ou N recursos do evento.
2. `single_resource.build_resource_from_arn`: estado atual de cada recurso.
3. `decision.classify_resource`: Etapa 2a.
4. `tag_execution.run_tagging_execution(..., dry_run=False)`: Etapa 2c
   (revalida e escreve se `taguear`; as outras decisões só vão ao relatório).

Ambiente: `EXPECTED_TAG_VALUE` e `SNS_TOPIC_ARN` (ver `publish.py`).
"""
from __future__ import annotations

import logging
import os

import boto3
from botocore.exceptions import ClientError

from . import cloudtrail_iac, decision, escopo_teste, event_parser, iac_detection, publish, resource_expansion, single_resource, tag_execution

logger = logging.getLogger("aws_prm_tagging.etapa3")
logger.info("boto3 %s (deve ser a versão da PrmSdkLayer, ver requirements-lambda.txt)", boto3.__version__)

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
    iac_evento: dict | None = None,
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
        # Sem conseguir ler o estado atual (já com backoff), não há decisão segura:
        # publica como falha.
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

    if iac_evento and resource.get("iac", {}).get("tipo") not in iac_detection.IAC_DETECTADO:
        # Criado por IaC segundo o CloudTrail (sinal mais forte que tags; o
        # Terraform não deixa tag por padrão). Nunca tagueado; vai para o cliente.
        resource = {**resource, "iac": iac_evento}

    decisao_resultado = decision.classify_resource(resource, expected_tag_value)
    if decisao_resultado is None:
        # Fora de escopo (tipo_recurso inesperado): só contabilizado e logado,
        # como em `decision.build_decision_report`.
        logger.warning("Recurso fora de escopo desta automação, ignorado: %s", extracted.arn)
        return

    tem_erro_classificacao = "erro" in decisao_resultado
    decision_report = {
        "conta_id": conta_id,
        "valor_tag_esperado": expected_tag_value,
        "recursos": [] if tem_erro_classificacao else [decisao_resultado],
        "erros": [decisao_resultado] if tem_erro_classificacao else [],
    }

    # Etapa 2c: revalida, escreve se `taguear` e monta o relatório.
    execution_report = tag_execution.run_tagging_execution(
        decision_report,
        session=session,
        expected_tag_value=expected_tag_value,
        dry_run=False,
        revalidate=True,
    )
    resultado_final = execution_report["recursos"][0]
    if resultado_final["categoria_final"] == "ja_ok" and os.environ.get("PUBLICAR_JA_OK", "true").lower() == "false":
        # PublicarResultadoJaOk=false evita um e-mail por task ECS que já nasce
        # com a tag (propagateTags).
        logger.info("Já em conformidade, publicação suprimida (PUBLICAR_JA_OK=false): %s", resultado_final["arn"])
        return
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
            estado_anterior=resultado_final.get("estado_anterior"),
            acao=resultado_final.get("acao"),
            iac=resultado_final.get("iac"),
            event_id=event_id,
            event_name=event_name,
            event_source=event_source,
        ),
    )


def _eh_instancia(arn: str) -> bool:
    return arn.startswith("arn:aws:ec2:") and ":instance/" in arn


def _iac_das_instancias(session: boto3.Session, recursos: list[event_parser.ExtractedResource]) -> dict[str, dict]:
    """Instância vinda do evento nativo "running" (sem userAgent): o
        RunInstances original é buscado no histórico do CloudTrail (o debounce
        dá tempo de ele aparecer). Falha aqui cai na heurística de tags."""
    achados: dict[str, dict] = {}
    for r in recursos:
        if not _eh_instancia(r.arn):
            continue
        try:
            client = session.client("cloudtrail", region_name=r.regiao)
            iac = cloudtrail_iac.iac_por_historico(client, r.arn, rapido=True)
        except Exception:  # noqa: BLE001 — sinal opcional
            logger.warning("Não foi possível consultar o histórico do CloudTrail para %s", r.arn, exc_info=True)
            continue
        if iac:
            achados[r.arn] = iac
    return achados


def _sns_client_for_topic(session: boto3.Session, topic_arn: str):
    """Client SNS na região do tópico, que fica no hub e pode diferir da
        região da Lambda regional."""
    partes = topic_arn.split(":")
    regiao = partes[3] if len(partes) > 3 and partes[3] else None
    return session.client("sns", region_name=regiao) if regiao else session.client("sns")


def handler(event: dict, context=None, *, session: boto3.Session | None = None, sns_client=None) -> None:
    """`session`/`sns_client` são injetáveis para teste; em produção a Lambda
        recebe só `(event, context)`."""
    expected_tag_value = os.environ["EXPECTED_TAG_VALUE"]
    topic_arn = os.environ["SNS_TOPIC_ARN"]

    detail = event.get("detail") or {}
    event_id = detail.get("eventID") or event.get("id")
    event_name = detail.get("eventName") or event.get("detail-type")
    event_source = detail.get("eventSource") or event.get("source")
    conta_id = detail.get("recipientAccountId") or event.get("account")

    extracted_resources = event_parser.parse_creation_event(event)
    iac_evento = iac_detection.detect_iac_from_cloudtrail(detail)
    if iac_evento:
        logger.info("Evento %s veio de IaC (%s): %s", event_id, iac_evento["ferramenta"], iac_evento["evidencia"])
    # Conta compartilhada (sandbox): FiltroEscopoTeste restringe a reação
    # aos recursos/principais de teste. Vazio em produção = sem efeito.
    fora = [r.arn for r in extracted_resources if not escopo_teste.dentro_do_escopo(event, r.arn)]
    if fora:
        logger.info("Fora do FiltroEscopoTeste, ignorado(s): %s", fora)
        extracted_resources = [r for r in extracted_resources if r.arn not in fora]
    if not extracted_resources:
        # Falha de extração ou evento fora do mapeamento: só log. A varredura
        # da Etapa 4 é o backstop.
        logger.warning(
            "Não foi possível extrair nenhum recurso do evento %s (%s/%s)", event_id, event_source, event_name
        )
        return

    session = session or boto3.Session()
    sns_client = sns_client or _sns_client_for_topic(session, topic_arn)

    # Instância EC2 -> volumes EBS anexados (o root nunca gera CreateVolume
    # próprio). Falha aqui nunca bloqueia a instância; é publicada abaixo.
    volumes, falhas_expansao = resource_expansion.expand_instance_volumes(session, extracted_resources)
    extracted_resources = list(extracted_resources) + volumes
    for falha in falhas_expansao:
        try:
            publish.publish_outcome(
                sns_client,
                topic_arn,
                publish.build_outcome_payload(
                    etapa=_ETAPA,
                    conta_id=conta_id,
                    arn=falha["arn"],
                    servico=None,
                    regiao=falha["regiao"],
                    tipo_recurso=None,
                    categoria_final="falhou",
                    origem="expansao_volumes",
                    resultado="erro_expansao_volumes",
                    detalhe_erro={"codigo": falha["codigo"], "mensagem": falha["mensagem"]},
                    event_id=event_id,
                    event_name=event_name,
                    event_source=event_source,
                ),
            )
        except Exception:
            logger.exception("Falha ao publicar erro de expansão de volumes de %s", falha["arn"])

    # IaC por recurso: o do evento (userAgent) vale para todos; senão, o da
    # instância (histórico) vale para ela e para os volumes dela.
    iac_instancias = {} if iac_evento else _iac_das_instancias(session, extracted_resources)

    falhas_inesperadas = 0
    for extracted in extracted_resources:
        iac_recurso = iac_evento or iac_instancias.get(extracted.arn) or iac_instancias.get(extracted.origem_arn or "")
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
                iac_recurso,
            )
        except Exception as exc:
            # Falha inesperada num recurso não impede os demais do mesmo evento; a
            # execução é marcada como falha no final.
            logger.exception(
                "Falha inesperada ao processar %s — continuando com os demais recursos do evento", extracted.arn
            )
            falhas_inesperadas += 1
            # A falha também é publicada no SNS, em try/except próprio para não
            # derrubar o loop.
            try:
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
                        origem="processamento",
                        resultado="erro_inesperado",
                        detalhe_erro={"codigo": type(exc).__name__, "mensagem": str(exc)},
                        event_id=event_id,
                        event_name=event_name,
                        event_source=event_source,
                    ),
                )
            except Exception:
                logger.exception(
                    "Falha ao publicar o resultado de erro inesperado de %s no SNS — a falha em si já "
                    "foi logada acima, só a publicação que não chegou",
                    extracted.arn,
                )

    if falhas_inesperadas:
        raise RuntimeError(
            f"{falhas_inesperadas} de {len(extracted_resources)} recurso(s) deste evento falharam "
            "de forma inesperada — ver logs acima para detalhe por recurso."
        )
