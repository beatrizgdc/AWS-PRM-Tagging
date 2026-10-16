"""Tagueamento inicial assíncrono — item 8 do plano.

Três handlers, todos na stack HUB (uma vez por conta):

1. `custom_resource` — Custom Resource do CloudFormation. No `Create` (e no
   `Update` quando o parâmetro `ReexecutarTagueamentoInicial` muda), INICIA
   a state machine do tagueamento inicial e responde ao CloudFormation na
   hora. O tagueamento em si pode levar muito mais que o limite de 1h de um
   Custom Resource — por isso é assíncrono. No `Delete` não faz nada (a
   automação nunca remove tags).
2. `listar_regioes` — primeiro passo da state machine: regiões ativas da
   conta (mesmo critério da Etapa 1).
3. `processar_regiao` — um item do `Map`: Etapa 1 -> 2a -> 2b/2c para UMA
   região (`pipeline.executar_regiao`) e publica o resumo no tópico do hub.
   Uma Lambda por região mantém cada execução bem abaixo dos 15 min na
   maioria das contas; se estourar, a state machine captura e publica a
   falha da região (quem opera roda o CLI para aquela região).

Modo (`MODO_TAGUEAMENTO_INICIAL`): `dry-run` (default — nada é escrito, o
resumo mostra o que seria feito), `live` ou `desligado`. O guia PRM exige
consentimento do cliente antes de taguear; `dry-run` como default deixa
essa decisão explícita no deploy.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

import boto3

from . import pipeline, publish, regions, services

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MODOS = ("dry-run", "live", "desligado")


# ---------------------------------------------------------------------------
# Resposta ao CloudFormation (sem depender do módulo cfnresponse, que só
# existe quando o código é inline no template)
# ---------------------------------------------------------------------------


def _responder(event: dict, context, status: str, dados: dict | None = None, motivo: str | None = None,
               opener=urllib.request.urlopen) -> None:
    corpo = json.dumps(
        {
            "Status": status,
            "Reason": (motivo or "ver CloudWatch Logs")[:1000]
            + (f" (log: {getattr(context, 'log_stream_name', '-')})" if context else ""),
            "PhysicalResourceId": event.get("PhysicalResourceId") or "prm-tagueamento-inicial",
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "NoEcho": False,
            "Data": dados or {},
        }
    ).encode()
    url = event["ResponseURL"]
    # MVP2 (segurança): o CloudFormation sempre manda uma URL pré-assinada
    # HTTPS do S3. Qualquer outra coisa (file://, http://) é recusada.
    if not str(url).startswith("https://"):
        raise ValueError("ResponseURL precisa ser https://")
    req = urllib.request.Request(
        url, data=corpo, method="PUT", headers={"Content-Type": "", "Content-Length": str(len(corpo))}
    )
    opener(req, timeout=30)  # nosec B310 — esquema validado acima


def custom_resource(event: dict, context=None, *, sfn_client=None, opener=urllib.request.urlopen) -> None:
    tipo = event.get("RequestType")
    props = event.get("ResourceProperties") or {}
    antigas = event.get("OldResourceProperties") or {}
    modo = props.get("Modo", "dry-run")
    try:
        deve_rodar = modo != "desligado" and (
            tipo == "Create"
            or (tipo == "Update" and props.get("Reexecutar") != antigas.get("Reexecutar"))
        )
        dados: dict = {"Iniciado": "false"}
        if deve_rodar:
            if modo not in MODOS:
                raise ValueError(f"Modo inválido: {modo!r} (esperado: {', '.join(MODOS)})")
            sfn = sfn_client or boto3.client("stepfunctions")
            execucao = sfn.start_execution(
                stateMachineArn=os.environ["STATE_MACHINE_ARN"],
                input=json.dumps({"modo": modo}),
            )
            dados = {"Iniciado": "true", "ExecutionArn": execucao["executionArn"]}
            logger.info("Tagueamento inicial iniciado (%s): %s", modo, execucao["executionArn"])
        _responder(event, context, "SUCCESS", dados, opener=opener)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falha no Custom Resource do tagueamento inicial")
        # No Delete, NUNCA falhar (travaria a remoção da stack). No
        # Create/Update, falhar expõe erro de configuração (ex.: permissão
        # para iniciar a state machine) em vez de a stack "subir" sem fazer
        # nada.
        status = "SUCCESS" if tipo == "Delete" else "FAILED"
        _responder(event, context, status, motivo=f"{type(exc).__name__}: {exc}", opener=opener)


ORIGEM_INICIAL = "tagueamento_inicial"
ORIGEM_RECORRENTE = "varredura_recorrente"  # Etapa 4 — mesma state machine, disparada pelo Scheduler

# Nome FIXO da DLQ da stack regional (template-regional.yaml) — permite à
# varredura recorrente ler a profundidade da fila em cada região.
NOME_DLQ_REGIONAL = "prm-etapa3-dlq"

# Categorias de "estado estável": não são novidade numa varredura diária.
_SEM_NOVIDADE = frozenset({"ja_ok", "pulado_iac"})


def listar_regioes(event: dict, context=None, *, session: boto3.Session | None = None) -> dict:
    session = session or boto3.Session()
    ativas = regions.get_active_regions(session)
    # Parâmetro RegioesVarredura do hub: restringe o tagueamento inicial e a
    # varredura a uma lista (ex.: sandbox numa região só, ou cliente com SCP
    # de restrição de região). Vazio = todas as regiões ativas.
    permitidas = [r.strip() for r in os.environ.get("REGIOES_PERMITIDAS", "").split(",") if r.strip()]
    if permitidas:
        ativas = [r for r in ativas if r in permitidas]
    origem = event.get("origem", ORIGEM_INICIAL)
    logger.info("%s: %d região(ões) ativa(s): %s", origem, len(ativas), ", ".join(ativas))
    return {"modo": event.get("modo", "dry-run"), "origem": origem, "regioes": ativas}


def _saude_regional(session: boto3.Session, regiao: str) -> dict:
    """Lê a DLQ da stack regional desta região. Fila inexistente = a stack
    regional não está implantada aqui — lacuna de cobertura da Etapa 3
    (região ativa sem automação contínua)."""
    sqs = session.client("sqs", region_name=regiao)
    try:
        url = sqs.get_queue_url(QueueName=NOME_DLQ_REGIONAL)["QueueUrl"]
    except sqs.exceptions.QueueDoesNotExist:
        return {"stack_regional_detectada": False, "dlq_mensagens": None}
    except Exception as exc:  # noqa: BLE001 — saúde é informativa, nunca derruba a varredura
        logger.warning("Não foi possível ler a DLQ em %s: %s", regiao, exc)
        return {"stack_regional_detectada": None, "dlq_mensagens": None}
    attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]
    return {"stack_regional_detectada": True, "dlq_mensagens": int(attrs.get("ApproximateNumberOfMessages", 0))}


def _tem_novidade(resumo: dict) -> bool:
    if resumo.get("regiao_bloqueada_por_scp"):
        # Estado conhecido da organização (SCP de restrição de regiões): não
        # dá para descobrir nada nem implantar a stack regional ali. Visto
        # na sandbox em 2026-10-02: 13 e-mails por varredura só por isso.
        return False
    return bool(
        set(resumo["por_categoria"]) - _SEM_NOVIDADE
        or resumo["total_falhas_descoberta"]
        or resumo.get("dlq_mensagens")
        or resumo.get("stack_regional_detectada") is False
        or resumo.get("contrato_confirmado") is False
    )


def processar_regiao(event: dict, context=None, *, session: boto3.Session | None = None, sns_client=None) -> dict:
    regiao = event["regiao"]
    modo = event.get("modo", "dry-run")
    if modo not in ("dry-run", "live"):
        raise ValueError(f"Modo inválido para processar região: {modo!r}")
    valor = os.environ["EXPECTED_TAG_VALUE"]
    topic_arn = os.environ["SNS_TOPIC_ARN"]
    session = session or boto3.Session()
    conta = session.client("sts").get_caller_identity()["Account"]

    resumo = pipeline.executar_regiao(
        session, conta, regiao, services.load_services(), valor, live=(modo == "live"), execucao_inicial=True
    )
    origem = event.get("origem", ORIGEM_INICIAL)
    resumo["etapa"] = origem
    # Vira o assunto do e-mail ("PRM Etapa varredura_recorrente - resumo
    # us-east-1 (dry-run)") — ASCII, como o SNS exige no Subject.
    resumo["categoria_final"] = f"resumo {regiao} ({modo})"
    resumo.update(_saude_regional(session, regiao))
    # MVP2 (item 3): confirma que a Lambda usa o SDK da layer, não o do runtime.
    resumo["versao_boto3"] = boto3.__version__
    # Conta que herdou o contrato pelo default da OU (auto-deployment do
    # StackSet) não é considerada validada até alguém confirmar —
    # docs/arquitetura-multicliente.md. Parâmetro ContratoConfirmado do hub.
    resumo["contrato_confirmado"] = os.environ.get("CONTRATO_CONFIRMADO", "false").lower() == "true"

    publicar = (
        origem != ORIGEM_RECORRENTE
        or os.environ.get("PUBLICAR_SEM_NOVIDADE", "false").lower() == "true"
        or _tem_novidade(resumo)
    )
    if publicar:
        sns = sns_client or session.client("sns", region_name=topic_arn.split(":")[3])
        publish.publish_outcome(sns, topic_arn, resumo)
    else:
        logger.info("Varredura em %s sem novidade — resumo não publicado (PUBLICAR_SEM_NOVIDADE=false)", regiao)
    logger.info("Região %s: %s", regiao, json.dumps(resumo["por_categoria"]))
    # Devolve só o essencial para o Map (o resumo completo foi para o SNS).
    return {
        "regiao": regiao,
        "por_categoria": resumo["por_categoria"],
        "total_falhas_escrita": resumo["total_falhas_escrita"],
        "publicado": publicar,
    }
