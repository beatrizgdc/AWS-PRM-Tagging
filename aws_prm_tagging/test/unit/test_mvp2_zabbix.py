"""MVP2, item 1 — integração com o Zabbix [simulado localmente: servidor
trapper falso em TCP local, API via opener falso, SSM via moto]."""
from __future__ import annotations

import io
import json
import re
import socketserver
import struct
import threading
import uuid
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

from aws_prm_tagging import handler_zabbix, zabbix

HOST = "aws-prm"
CONTA = "111122223333"
TEMPLATE = Path(__file__).resolve().parents[2] / "infra" / "zabbix" / "template-prm-tagging.yaml"


def _chaves(valores):
    return {v["key"]: v["value"] for v in valores}


# ---------------------------------------------------------------------------
# mapeamento mensagem -> itens
# ---------------------------------------------------------------------------


def test_etapa3_falha_sucesso_iac():
    base = {"etapa": "3", "conta_id": CONTA, "arn": "arn:aws:s3:::b"}
    assert _chaves(zabbix.valores_da_mensagem({**base, "categoria_final": "falhou"}, HOST))[f"prm.falha[{CONTA}]"] == "1"
    assert f"prm.sucesso[{CONTA}]" in _chaves(zabbix.valores_da_mensagem({**base, "categoria_final": "tagueado_sucesso"}, HOST))
    assert f"prm.iac[{CONTA}]" in _chaves(zabbix.valores_da_mensagem({**base, "categoria_final": "pulado_iac"}, HOST))
    ja_ok = _chaves(zabbix.valores_da_mensagem({**base, "categoria_final": "ja_ok"}, HOST))
    assert set(ja_ok) == {"prm.evento", f"prm.heartbeat[{CONTA}]"}


def test_resumo_de_varredura_dry_run():
    msg = {
        "etapa": "varredura_recorrente", "conta_id": CONTA, "regiao": "us-east-1", "modo": "dry-run",
        "por_categoria": {"simulado_sucesso": 4, "falhou": 1, "ja_ok": 9}, "total_falhas_escrita": 1,
        "total_falhas_descoberta": 2, "dlq_mensagens": 3, "stack_regional_detectada": False,
        "regiao_bloqueada_por_scp": False, "cliente_resumo": {"total_recursos_iac": 7},
    }
    v = _chaves(zabbix.valores_da_mensagem(msg, HOST))
    assert v[f"prm.pendentes[{CONTA}]"] == "5"
    assert v[f"prm.falhas_varredura[{CONTA}]"] == "3"
    assert v[f"prm.dlq[{CONTA}]"] == "3"
    assert v[f"prm.regiao_sem_stack[{CONTA}]"] == "1"
    assert v[f"prm.iac[{CONTA}]"] == "7"


def test_regiao_bloqueada_por_scp_nao_vira_falha_nem_lacuna():
    msg = {"etapa": "varredura_recorrente", "conta_id": CONTA, "modo": "dry-run", "por_categoria": {},
           "total_falhas_escrita": 0, "total_falhas_descoberta": 1, "stack_regional_detectada": False,
           "regiao_bloqueada_por_scp": True}
    v = _chaves(zabbix.valores_da_mensagem(msg, HOST))
    assert v[f"prm.falhas_varredura[{CONTA}]"] == "0"
    assert f"prm.regiao_sem_stack[{CONTA}]" not in v


def test_drift_e_falha_de_regiao_pela_execucao():
    drift = _chaves(zabbix.valores_da_mensagem({"etapa": "4-drift", "categoria_final": "drift_tag_removida",
                                                "conta_id": CONTA}, HOST))
    assert drift[f"prm.drift[{CONTA}]"] == "1"
    falha = {"regiao": "eu-west-1", "modo": "live", "origem": "varredura_recorrente",
             "execucao": f"arn:aws:states:us-east-1:{CONTA}:execution:sm:abc", "erro": {"Error": "States.Timeout"}}
    assert _chaves(zabbix.valores_da_mensagem(falha, HOST))[f"prm.falha_regiao[{CONTA}]"] == "1"


def test_mensagem_desconhecida_vira_so_evento():
    v = zabbix.valores_da_mensagem({"texto": "qualquer coisa"}, HOST, agora=10)
    assert v == [{"host": HOST, "key": "prm.evento", "value": '{"texto":"qualquer coisa"}', "clock": 10}]


def test_evento_grande_e_truncado():
    v = zabbix.valores_da_mensagem({"x": "a" * 100_000}, HOST)
    assert len(v[0]["value"]) == 60_000


def test_todas_as_chaves_existem_no_template_zabbix():
    tpl = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))["zabbix_export"]
    t = tpl["templates"][0]
    prototipos = {ip["key"].split("[")[0] for ip in t["discovery_rules"][0]["item_prototypes"]}
    usadas = set(re.findall(r'item\("(prm\.[a-z_]+)"', (Path(zabbix.__file__)).read_text(encoding="utf-8")))
    assert usadas <= prototipos, usadas - prototipos
    assert t["items"][0]["key"] == "prm.evento" and t["discovery_rules"][0]["key"] == "prm.contas"
    ids = re.findall(r"uuid: ([0-9a-f]{32})", TEMPLATE.read_text(encoding="utf-8"))
    assert len(ids) == len(set(ids)) and all(uuid.UUID(i).version == 4 for i in ids)
    for ip in t["discovery_rules"][0]["item_prototypes"]:
        for tr in ip.get("trigger_prototypes", []):
            assert f"/{t['template']}/{ip['key']}" in tr["expression"]


# ---------------------------------------------------------------------------
# protocolo trapper (servidor falso)
# ---------------------------------------------------------------------------


class _TrapperFalso(socketserver.BaseRequestHandler):
    recebidos: list = []
    resposta = {"response": "success", "info": "processed: 2; failed: 0; total: 2; seconds spent: 0.0001"}

    def handle(self):
        cab = self.request.recv(13)
        assert cab[:5] == b"ZBXD\x01"
        tamanho = struct.unpack("<I", cab[5:9])[0]
        corpo = b""
        while len(corpo) < tamanho:
            corpo += self.request.recv(tamanho - len(corpo))
        _TrapperFalso.recebidos.append(json.loads(corpo))
        r = json.dumps(_TrapperFalso.resposta).encode()
        self.request.sendall(b"ZBXD\x01" + struct.pack("<II", len(r), 0) + r)


@pytest.fixture
def trapper():
    _TrapperFalso.recebidos = []
    srv = socketserver.TCPServer(("127.0.0.1", 0), _TrapperFalso)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address
    srv.shutdown()
    srv.server_close()


def test_pacote_trapper_cabecalho():
    p = zabbix.pacote_trapper([{"host": "h", "key": "k", "value": "1", "clock": 1}])
    assert p[:5] == b"ZBXD\x01"
    assert struct.unpack("<II", p[5:13]) == (len(p) - 13, 0)
    assert json.loads(p[13:])["request"] == "sender data"


def test_enviar_trapper_ponta_a_ponta(trapper):
    valores = [{"host": HOST, "key": "prm.evento", "value": "x", "clock": 1}]
    resp = zabbix.enviar_trapper(*trapper, valores)
    assert resp["response"] == "success"
    assert _TrapperFalso.recebidos[0]["data"] == valores


def test_enviar_trapper_falha_parcial_so_loga(trapper, caplog):
    _TrapperFalso.resposta = {"response": "success", "info": "processed: 1; failed: 1; total: 2"}
    try:
        zabbix.enviar_trapper(*trapper, [{"host": HOST, "key": "k", "value": "1", "clock": 1}])
    finally:
        _TrapperFalso.resposta = {"response": "success", "info": "processed: 2; failed: 0; total: 2"}
    assert "falhas parciais" in caplog.text


def test_enviar_trapper_recusado(trapper):
    _TrapperFalso.resposta = {"response": "failed", "info": "x"}
    try:
        with pytest.raises(zabbix.ErroZabbix):
            zabbix.enviar_trapper(*trapper, [{"host": HOST, "key": "k", "value": "1", "clock": 1}])
    finally:
        _TrapperFalso.resposta = {"response": "success", "info": "processed: 1; failed: 0"}


# ---------------------------------------------------------------------------
# API history.push (opener falso)
# ---------------------------------------------------------------------------


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_enviar_api_monta_history_push():
    pedidos = []

    def opener(req, timeout):
        pedidos.append(req)
        return _Resp(json.dumps({"jsonrpc": "2.0", "result": {"response": "success", "data": [{"itemid": "1"}]}}).encode())

    zabbix.enviar_api("https://zbx.exemplo.com/", "tok", [{"host": HOST, "key": "prm.evento", "value": "x", "clock": 1}],
                      opener=opener)
    req = pedidos[0]
    assert req.full_url == "https://zbx.exemplo.com/api_jsonrpc.php"
    assert req.headers["Authorization"] == "Bearer tok"
    corpo = json.loads(req.data)
    assert corpo["method"] == "history.push" and corpo["params"][0]["key"] == "prm.evento"


def test_enviar_api_exige_https_e_trata_erro():
    with pytest.raises(zabbix.ErroZabbix):
        zabbix.enviar_api("http://zbx", "tok", [], opener=lambda *a, **k: pytest.fail("não deveria chamar"))
    erro = lambda req, timeout: _Resp(json.dumps({"error": {"code": -32602, "message": "Invalid params."}}).encode())  # noqa: E731
    with pytest.raises(zabbix.ErroZabbix):
        zabbix.enviar_api("https://zbx", "tok", [], opener=erro)


# ---------------------------------------------------------------------------
# handler (evento SNS)
# ---------------------------------------------------------------------------


def _evento_sns(*mensagens):
    return {"Records": [{"Sns": {"Message": m if isinstance(m, str) else json.dumps(m), "Subject": "s"}} for m in mensagens]}


def test_handler_envia_lld_antes_dos_valores(monkeypatch):
    monkeypatch.setenv("ZABBIX_HOST", HOST)
    monkeypatch.setenv("ZABBIX_MODO", "trapper")
    enviados = []
    r = handler_zabbix.handler(
        _evento_sns({"etapa": "3", "conta_id": CONTA, "categoria_final": "falhou"}, "texto solto"),
        enviar=enviados.append,
    )
    lote = enviados[0]
    assert lote[0]["key"] == "prm.contas"
    assert json.loads(lote[0]["value"]) == [{"{#CONTA}": CONTA}]
    assert r["contas"] == [CONTA]
    assert sum(1 for v in lote if v["key"] == "prm.evento") == 2


def test_handler_modo_api_le_token_do_ssm(monkeypatch):
    for k, v in dict(AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x", AWS_DEFAULT_REGION="us-east-1",
                     ZABBIX_HOST=HOST, ZABBIX_MODO="api", ZABBIX_URL="https://zbx",
                     ZABBIX_TOKEN_PARAM="/prm/zabbix/token").items():
        monkeypatch.setenv(k, v)
    handler_zabbix._TOKEN_CACHE.clear()
    chamadas = []
    monkeypatch.setattr(zabbix, "enviar_api", lambda url, token, vs: chamadas.append((url, token, len(vs))))
    with mock_aws():
        boto3.client("ssm").put_parameter(Name="/prm/zabbix/token", Value="segredo", Type="SecureString")
        handler_zabbix.handler(_evento_sns({"etapa": "3", "conta_id": CONTA, "categoria_final": "ja_ok"}))
    assert chamadas[0][:2] == ("https://zbx", "segredo")


def test_handler_falha_de_envio_propaga_para_ir_a_dlq(monkeypatch):
    monkeypatch.setenv("ZABBIX_HOST", HOST)

    def falha(vs):
        raise zabbix.ErroZabbix("fora do ar")

    with pytest.raises(zabbix.ErroZabbix):
        handler_zabbix.handler(_evento_sns({"texto": "x"}), enviar=falha)
