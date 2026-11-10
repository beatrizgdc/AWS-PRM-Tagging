"""Lambda do resumo diário (stack central, na payer).

Uma vez por dia, lê a fila que acumula as mensagens de todas as contas
(alertas, resumos de execução e resultados por recurso), monta um e-mail só
com o consolidado e o publica no tópico central com `prm_tipo=resumo_diario`.
As mensagens só saem da fila depois que o resumo foi publicado: se a
publicação falhar, elas entram no resumo do dia seguinte.

Ambiente: `FILA_RESUMO_URL`, `SNS_TOPIC_ARN`, `LIMITE_MENSAGENS` (opcional).
"""
from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import boto3

from . import publish

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_LIMITE_PADRAO = 20000
_MARGEM_MS = 60_000  # tempo reservado para montar, publicar e apagar
_MAX_LINHAS = 30  # por lista no e-mail
_MAX_CARACTERES = 200_000  # o SNS aceita até 256 KB


def coletar(sqs, fila_url: str, limite: int, tempo_restante=lambda: 10**9) -> tuple[list[dict], list[str], int]:
    """Lê a fila até esvaziar, atingir `limite` ou o tempo acabar.
    Devolve (mensagens, recibos, ilegiveis)."""
    mensagens: list[dict] = []
    recibos: list[str] = []
    ilegiveis = 0
    while len(recibos) < limite and tempo_restante() > _MARGEM_MS:
        r = sqs.receive_message(QueueUrl=fila_url, MaxNumberOfMessages=10, WaitTimeSeconds=1)
        lote = r.get("Messages", [])
        if not lote:
            break
        for m in lote:
            recibos.append(m["ReceiptHandle"])
            try:
                corpo = json.loads(m["Body"])
                if isinstance(corpo, dict) and "Message" in corpo and "TopicArn" in corpo:
                    corpo = json.loads(corpo["Message"])  # entrega sem RawMessageDelivery
                mensagens.append(corpo if isinstance(corpo, dict) else {"_texto": str(corpo)})
            except (ValueError, TypeError):
                ilegiveis += 1
    return mensagens, recibos, ilegiveis


def apagar(sqs, fila_url: str, recibos: list[str]) -> None:
    for i in range(0, len(recibos), 10):
        lote = [{"Id": str(n), "ReceiptHandle": h} for n, h in enumerate(recibos[i : i + 10])]
        r = sqs.delete_message_batch(QueueUrl=fila_url, Entries=lote)
        if r.get("Failed"):
            logger.warning("Falha ao apagar %d mensagem(ns); voltam no próximo resumo", len(r["Failed"]))


def _linha_alerta(m: dict) -> str:
    erro = m.get("detalhe_erro") or {}
    motivo = erro.get("mensagem") or erro.get("codigo") or m.get("motivo") or ""
    if "por_categoria" in m:
        partes = []
        if m.get("total_falhas_descoberta") and not m.get("regiao_bloqueada_por_scp"):
            partes.append(f"{m['total_falhas_descoberta']} falha(s) de descoberta")
        if m.get("total_falhas_escrita"):
            partes.append(f"{m['total_falhas_escrita']} falha(s) de escrita")
        if m.get("dlq_mensagens"):
            partes.append(f"{m['dlq_mensagens']} mensagem(ns) na DLQ")
        if m.get("stack_regional_detectada") is False:
            partes.append("stack regional ausente")
        motivo = ", ".join(partes) or motivo
        return f"- conta {m.get('conta_id', '?')} | {m.get('regiao', '?')} | execução {m.get('modo', '')}: {motivo}"
    if erro.get("codigo") and erro.get("mensagem"):
        motivo = f"{erro['codigo']}: {erro['mensagem']}"
    if not motivo and m.get("possivel_disputa_com_outro_parceiro"):
        motivo = "tag trocada por outro valor (possível outro parceiro)"
    if not motivo and m.get("acao") == "sobrescrita":
        anterior = (m.get("estado_anterior") or {}).get("valor_anterior_aws_apn_id")
        motivo = f"sobrescreveu o valor de outro parceiro ({anterior})" if anterior else "sobrescreveu outro valor"
    return (
        f"- conta {m.get('conta_id', '?')} | {m.get('regiao', '?')} | {m.get('categoria_final', '?')} | "
        f"{m.get('arn') or m.get('servico') or ''}" + (f" | {motivo}" if motivo else "")
    )


def _lista(linhas: list[str]) -> list[str]:
    if len(linhas) <= _MAX_LINHAS:
        return linhas
    return linhas[:_MAX_LINHAS] + [f"  ... e mais {len(linhas) - _MAX_LINHAS}"]


def montar(mensagens: list[dict], ilegiveis: int = 0, data: str | None = None,
           restam_na_fila: bool = False) -> tuple[str, str]:
    """Devolve (assunto, texto) do e-mail."""
    data = data or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    alertas, resumos, resultados = [], [], []
    for m in mensagens:
        if m.get("_texto") is not None:
            resultados.append(m)
            continue
        tipo = publish.tipo_mensagem(m) if "etapa" in m or "por_categoria" in m else publish.TIPO_ALERTA
        (alertas if tipo == publish.TIPO_ALERTA else resumos if tipo == publish.TIPO_RESUMO else resultados).append(m)
        if tipo == publish.TIPO_ALERTA and "por_categoria" in m:
            resumos.append(m)  # o resumo com problema também entra na tabela de execuções

    contas = {m.get("conta_id") for m in mensagens if m.get("conta_id")}
    t: list[str] = [
        f"Resumo diário da automação AWS PRM ({data}, últimas 24 h)",
        f"Contas com atividade: {len(contas)} | mensagens: {len(mensagens)} | alertas: {len(alertas)}",
        "",
    ]
    if not mensagens:
        t.append("Sem atividade no período. A automação está ativa (este e-mail é o sinal de vida diário).")

    if alertas:
        t += ["PRECISA DE ATENÇÃO (já enviado na hora, um e-mail por alerta)", *_lista([_linha_alerta(m) for m in alertas]), ""]

    if resumos:
        t.append("EXECUÇÕES (tagueamento inicial e varredura)")
        linhas = []
        for m in sorted(resumos, key=lambda x: (str(x.get("conta_id")), str(x.get("regiao")))):
            cats = ", ".join(f"{k} {v}" for k, v in sorted((m.get("por_categoria") or {}).items())) or "nada a fazer"
            linhas.append(f"- conta {m.get('conta_id', '?')} | {m.get('regiao', '?')} ({m.get('modo', '?')}): {cats}")
        t += [*_lista(linhas), ""]

    por_conta: dict[str, Counter] = defaultdict(Counter)
    iac: list[str] = []
    for m in resultados:
        if m.get("_texto") is not None:
            continue
        por_conta[str(m.get("conta_id") or "?")][str(m.get("categoria_final") or "?")] += 1
        if m.get("categoria_final") == "pulado_iac" and m.get("arn"):
            ferramenta = (m.get("iac") or {}).get("ferramenta") or "IaC"
            iac.append(f"- conta {m.get('conta_id', '?')} | {ferramenta} | {m['arn']}")
    for m in resumos:
        for i in m.get("iac_amostra") or []:
            iac.append(f"- conta {m.get('conta_id', '?')} | {i.get('ferramenta') or 'IaC'} | {i.get('arn')}")

    if por_conta:
        t.append("RECURSOS NOVOS E ALTERAÇÕES DE TAG (por conta)")
        for conta, cats in sorted(por_conta.items()):
            t.append(f"- conta {conta}: " + ", ".join(f"{k} {v}" for k, v in sorted(cats.items())))
        t.append("")

    if iac:
        t += [
            "IaC: A TAG PRECISA IR NO CÓDIGO DO CLIENTE (a automação não tagueia)",
            *_lista(sorted(set(iac))),
            "",
        ]

    if ilegiveis:
        t += [f"Mensagens ilegíveis ignoradas: {ilegiveis}", ""]
    if restam_na_fila:
        t += ["Ainda há mensagens na fila: elas entram no próximo resumo.", ""]
    t.append("Relatório completo de uma conta: python3 iniciar.py (opção 1) ou o CLI `apply` com o perfil da conta.")

    texto = "\n".join(t)
    if len(texto) > _MAX_CARACTERES:
        texto = texto[:_MAX_CARACTERES] + "\n... (cortado: limite de tamanho do e-mail)"
    assunto = f"PRM resumo diario {data} - {len(alertas)} alerta(s), {len(contas)} conta(s)"
    return assunto, texto


def handler(event: dict | None = None, context=None, *, sqs=None, sns=None) -> dict[str, Any]:
    fila = os.environ["FILA_RESUMO_URL"]
    topico = os.environ["SNS_TOPIC_ARN"]
    limite = int(os.environ.get("LIMITE_MENSAGENS", _LIMITE_PADRAO))
    sqs = sqs or boto3.client("sqs")
    sns = sns or boto3.client("sns", region_name=topico.split(":")[3])
    tempo = context.get_remaining_time_in_millis if context else (lambda: 10**9)

    mensagens, recibos, ilegiveis = coletar(sqs, fila, limite, tempo)
    assunto, texto = montar(mensagens, ilegiveis, restam_na_fila=len(recibos) >= limite)
    publish._sns_publish(sns, topico, texto, assunto, publish.TIPO_RESUMO_DIARIO)
    apagar(sqs, fila, recibos)
    logger.info("Resumo diário publicado: %d mensagem(ns), %d ilegível(is)", len(mensagens), ilegiveis)
    return {"mensagens": len(mensagens), "ilegiveis": ilegiveis}
