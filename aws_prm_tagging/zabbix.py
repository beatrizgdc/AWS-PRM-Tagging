"""Integração com o Zabbix.

Desenho: o SNS continua sendo o barramento único. Uma Lambda própria
(`handler_zabbix.py`, stack `template-zabbix.yaml`) assina o tópico — o
central da organização ou o de uma conta — e converte cada mensagem em
valores de itens *trapper* do Zabbix. O núcleo da automação não conhece o
Zabbix; trocar ou somar outro destino é só assinar outra coisa no tópico.

Dois modos de envio (variável ZABBIX_MODO):

- `api`: HTTPS para `<url>/api_jsonrpc.php`, método `history.push`
  (Zabbix 7.0+), com token de API (Bearer). Passa por proxy/firewall como
  qualquer HTTPS — é o recomendado.
- `trapper`: protocolo do `zabbix_sender` (TCP 10051) direto no server ou
  proxy. Para Zabbix < 7.0 ou quando a porta 10051 já é a usada.

Itens (ver `infra/zabbix/template-prm-tagging.yaml`, para importar no Zabbix):

- `prm.evento` (texto) — toda mensagem, em JSON, para histórico/consulta;
- descoberta de contas `prm.contas` (LLD) e, por conta `{#CONTA}`:
  `prm.sucesso[c]`, `prm.falha[c]`, `prm.iac[c]`, `prm.drift[c]`,
  `prm.pendentes[c]`, `prm.falhas_varredura[c]`, `prm.dlq[c]`,
  `prm.regiao_sem_stack[c]`, `prm.falha_regiao[c]`, `prm.heartbeat[c]`.

Só biblioteca padrão (socket/urllib/json).
"""
from __future__ import annotations

import json
import logging
import re
import socket
import struct
import time
import urllib.request

logger = logging.getLogger(__name__)

_TAM_MAX_TEXTO = 60_000  # itens de texto do Zabbix aceitam até 64 KB
_CONTA_RE = re.compile(r"^\d{12}$")


# ---------------------------------------------------------------------------
# Mensagem SNS -> valores de itens
# ---------------------------------------------------------------------------


def _conta(msg: dict) -> str | None:
    conta = msg.get("conta_id")
    if isinstance(conta, str) and _CONTA_RE.match(conta):
        return conta
    # Falha de região publicada pela state machine: só traz o ARN da execução.
    execucao = msg.get("execucao") or ""
    partes = execucao.split(":")
    if len(partes) > 4 and _CONTA_RE.match(partes[4]):
        return partes[4]
    arn = msg.get("arn") or ""
    partes = arn.split(":")
    if len(partes) > 4 and _CONTA_RE.match(partes[4]):
        return partes[4]
    return None


_PENDENTES = ("simulado_sucesso", "falhou")


def valores_da_mensagem(msg: dict, host: str, agora: int | None = None) -> list[dict]:
    """Lista de `{host, key, value, clock}` para uma mensagem publicada pela
    automação. Mensagens desconhecidas viram só `prm.evento`."""
    clock = int(agora if agora is not None else time.time())
    texto = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))[:_TAM_MAX_TEXTO]
    valores = [{"host": host, "key": "prm.evento", "value": texto, "clock": clock}]
    conta = _conta(msg)
    if not conta:
        return valores

    def item(chave: str, valor) -> None:
        valores.append({"host": host, "key": f"{chave}[{conta}]", "value": str(valor), "clock": clock})

    item("prm.heartbeat", 1)
    etapa = str(msg.get("etapa") or "")
    categoria = str(msg.get("categoria_final") or "")

    if "erro" in msg and "regiao" in msg and "por_categoria" not in msg:
        item("prm.falha_regiao", 1)  # Lambda de uma região falhou no tagueamento inicial/varredura
    elif "por_categoria" in msg:  # resumo por região (tagueamento inicial / varredura recorrente)
        pc = msg.get("por_categoria") or {}
        if msg.get("modo") == "dry-run":
            item("prm.pendentes", sum(int(pc.get(c, 0)) for c in _PENDENTES))
        falhas = int(msg.get("total_falhas_escrita") or 0)
        if not msg.get("regiao_bloqueada_por_scp"):
            falhas += int(msg.get("total_falhas_descoberta") or 0)
        item("prm.falhas_varredura", falhas)
        if msg.get("dlq_mensagens") is not None:
            item("prm.dlq", int(msg["dlq_mensagens"]))
        if msg.get("stack_regional_detectada") is False and not msg.get("regiao_bloqueada_por_scp"):
            item("prm.regiao_sem_stack", 1)
        iac = (msg.get("cliente_resumo") or {}).get("total_recursos_iac")
        if iac is not None:
            item("prm.iac", int(iac))
    elif etapa.startswith("4-drift") or categoria.startswith("drift_"):
        item("prm.drift", 1)
    elif etapa == "3":
        if categoria == "falhou":
            item("prm.falha", 1)
        elif categoria == "tagueado_sucesso":
            item("prm.sucesso", 1)
        elif categoria == "pulado_iac":
            item("prm.iac", 1)
    return valores


def lld_contas(contas: list[str], host: str, agora: int | None = None) -> dict:
    """Valor da regra de descoberta `prm.contas` (LLD) — cria os itens por conta."""
    return {
        "host": host,
        "key": "prm.contas",
        "value": json.dumps([{"{#CONTA}": c} for c in sorted(set(contas))]),
        "clock": int(agora if agora is not None else time.time()),
    }


# ---------------------------------------------------------------------------
# Envio
# ---------------------------------------------------------------------------


class ErroZabbix(Exception):
    pass


def pacote_trapper(valores: list[dict]) -> bytes:
    """Cabeçalho do protocolo Zabbix ("ZBXD" + 0x01 + tamanho de 8 bytes)."""
    corpo = json.dumps({"request": "sender data", "data": valores}, ensure_ascii=False).encode("utf-8")
    return b"ZBXD\x01" + struct.pack("<II", len(corpo), 0) + corpo


def _ler_resposta_trapper(sock) -> dict:
    cab = b""
    while len(cab) < 13:
        parte = sock.recv(13 - len(cab))
        if not parte:
            raise ErroZabbix("conexão fechada antes do cabeçalho")
        cab += parte
    if cab[:4] != b"ZBXD":
        raise ErroZabbix(f"resposta sem cabeçalho ZBXD: {cab!r}")
    tamanho = struct.unpack("<I", cab[5:9])[0]
    corpo = b""
    while len(corpo) < tamanho:
        parte = sock.recv(tamanho - len(corpo))
        if not parte:
            break
        corpo += parte
    return json.loads(corpo.decode("utf-8"))


def enviar_trapper(servidor: str, porta: int, valores: list[dict], timeout: float = 10.0) -> dict:
    with socket.create_connection((servidor, porta), timeout=timeout) as sock:
        sock.sendall(pacote_trapper(valores))
        resp = _ler_resposta_trapper(sock)
    if resp.get("response") != "success":
        raise ErroZabbix(f"trapper recusou: {resp}")
    info = str(resp.get("info", ""))
    m = re.search(r"failed:\s*(\d+)", info)
    if m and int(m.group(1)) > 0:
        # Item inexistente/desabilitado no Zabbix (ex.: conta nova antes do LLD
        # rodar). Não é erro de transporte: loga e segue.
        logger.warning("Zabbix processou com falhas parciais: %s", info)
    return resp


def enviar_api(url: str, token: str, valores: list[dict], timeout: float = 10.0, opener=urllib.request.urlopen) -> dict:
    endpoint = url.rstrip("/")
    if not endpoint.endswith("api_jsonrpc.php"):
        endpoint += "/api_jsonrpc.php"
    corpo = json.dumps({"jsonrpc": "2.0", "method": "history.push", "params": valores, "id": 1}).encode("utf-8")
    if not endpoint.startswith("https://"):
        raise ErroZabbix("ZABBIX_URL precisa ser https:// (o token vai no cabeçalho)")
    req = urllib.request.Request(
        endpoint, data=corpo, method="POST",
        headers={"Content-Type": "application/json-rpc", "Authorization": f"Bearer {token}"},
    )
    with opener(req, timeout=timeout) as resp:  # nosec B310 — https validado acima
        dados = json.loads(resp.read().decode("utf-8"))
    if "error" in dados:
        raise ErroZabbix(f"API recusou: {dados['error']}")
    falhas = [d for d in (dados.get("result") or {}).get("data", []) if "error" in d]
    if falhas:
        logger.warning("Zabbix recusou %d valor(es): %s", len(falhas), falhas[:5])
    return dados
