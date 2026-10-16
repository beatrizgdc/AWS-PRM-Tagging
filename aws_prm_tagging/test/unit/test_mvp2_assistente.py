"""MVP2, item 4 — modo interativo (`iniciar`), simulado com moto e respostas
digitadas por script."""
from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from aws_prm_tagging import assistente

VALOR = "pc:abc123"


@pytest.fixture
def conta(monkeypatch):
    for k, v in dict(AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x", AWS_DEFAULT_REGION="us-east-1").items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="b-novo")
        s3.create_bucket(Bucket="b-outro")
        s3.put_bucket_tagging(Bucket="b-outro", Tagging={"TagSet": [{"Key": "aws-apn-id", "Value": "pc:parceiroB"}]})
        s3.create_bucket(Bucket="b-tf")
        s3.put_bucket_tagging(Bucket="b-tf", Tagging={"TagSet": [{"Key": "managed-by", "Value": "terraform"}]})
        yield s3


def _tags(s3, b):
    try:
        return {t["Key"]: t["Value"] for t in s3.get_bucket_tagging(Bucket=b)["TagSet"]}
    except Exception:  # noqa: BLE001
        return {}


def _rodar(tmp_path, respostas, extra_args=(), executor=None):
    fila = iter(respostas)
    saida: list[str] = []
    ui = assistente.Interface(entrada=lambda p: next(fila), saida=saida.append)
    args = assistente.parser().parse_args(["--saida", str(tmp_path / "out"), *extra_args])
    rc = assistente.executar(
        args, ui=ui, fabrica_sessao=lambda profile_name=None: boto3.Session(),
        executor=executor or (lambda *a, **k: None),
    )
    return rc, "\n".join(saida)


#            perfil conta  valor  regioes      mapear simular aplicar arquivo confirmacao implantar
FLUXO_OK = ["", "s", VALOR, "us-east-1", "s", "s", "s", "", "123456789012", "n"]


def test_fluxo_completo_tagueia_e_respeita_iac(conta, tmp_path):
    rc, texto = _rodar(tmp_path, FLUXO_OK)
    assert rc == 0
    assert _tags(conta, "b-novo") == {"aws-apn-id": VALOR}
    assert _tags(conta, "b-outro") == {"aws-apn-id": VALOR}  # sobrescrito
    assert "aws-apn-id" not in _tags(conta, "b-tf")  # IaC intocado
    out = tmp_path / "out"
    for nome in ("mapa.json", "decisao.json", "resultado_dry_run.json", "resultado_live.json",
                 "resultado_live_cliente.md", "a_taguear.tsv", "execucao.log", "preflight.json"):
        assert (out / nome).exists(), nome
    md = (out / "resultado_live_cliente.md").read_text(encoding="utf-8")
    assert "b-tf" in md and "pc:parceiroB" in md
    assert "sobrescrever (outro parceiro)" in texto
    assert "Traceback" not in texto  # stack traces vão para execucao.log


def test_id_de_conta_errado_cancela_sem_escrever(conta, tmp_path):
    respostas = FLUXO_OK[:8] + ["999999999999", "n"]
    rc, texto = _rodar(tmp_path, respostas)
    assert rc == 0
    assert "nada foi escrito" in texto
    assert _tags(conta, "b-novo") == {}
    assert _tags(conta, "b-outro") == {"aws-apn-id": "pc:parceiroB"}


def test_recusar_aplicar_nao_escreve(conta, tmp_path):
    rc, texto = _rodar(tmp_path, ["", "s", VALOR, "us-east-1", "s", "s", "n", "n"])
    assert rc == 0 and _tags(conta, "b-novo") == {}
    assert "apply --profile" in texto


def test_conta_errada_para_no_inicio(conta, tmp_path):
    rc, _ = _rodar(tmp_path, ["", "n"])
    assert rc == 1
    assert not (tmp_path / "out").exists()


def test_valor_invalido_pede_de_novo(conta, tmp_path):
    fila = iter(["", "s", "abc", VALOR, "us-east-1", "n", "n"])
    perguntas: list[str] = []

    def entrada(p):
        perguntas.append(p)
        return next(fila)

    ui = assistente.Interface(entrada=entrada, saida=lambda t: None)
    args = assistente.parser().parse_args(["--saida", str(tmp_path / "out")])
    assert assistente.executar(args, ui=ui, fabrica_sessao=lambda profile_name=None: boto3.Session()) == 0
    assert any("formato pc:" in p for p in perguntas)


def test_implantar_chama_script_sem_tagueamento_inicial(conta, tmp_path, monkeypatch):
    monkeypatch.setattr(assistente.shutil, "which", lambda c: "/usr/bin/" + c)
    chamadas = []

    class _R:
        returncode = 0

    def executor(cmd, cwd=None):
        chamadas.append(cmd)
        return _R()

    respostas = ["", "s", VALOR, "us-east-1", "s", "s", "n", "s", "us-east-1", "eu@exemplo.com", "dry-run"]
    rc, texto = _rodar(tmp_path, respostas, executor=executor)
    assert rc == 0
    cmd = chamadas[0]
    assert cmd[1].endswith("deploy/implantar_conta.sh")
    i = cmd.index("--modo-inicial")
    assert cmd[i + 1] == "desligado"
    assert cmd[cmd.index("--email") + 1] == "eu@exemplo.com"
    assert "O QUE CRIA" in texto and "CUSTO" in texto and "REMOVER" in texto


def test_implantar_sem_sam_mostra_comando(conta, tmp_path, monkeypatch):
    monkeypatch.setattr(assistente.shutil, "which", lambda c: None)
    respostas = ["", "s", VALOR, "us-east-1", "s", "s", "n", "s", "us-east-1", "", "dry-run"]
    rc, texto = _rodar(tmp_path, respostas, executor=lambda *a, **k: pytest.fail("não deveria executar"))
    assert "falta sam, make" in texto and "implantar_conta.sh" in texto


def test_nao_interativo_so_le_sem_aplicar(conta, tmp_path):
    args = ["--nao-interativo", "--profile", "x", "--valor", VALOR, "--regioes", "us-east-1"]
    ui = assistente.Interface(interativo=False, saida=lambda t: None,
                              respostas=assistente._respostas(assistente.parser().parse_args(args)))
    a = assistente.parser().parse_args([*args, "--saida", str(tmp_path / "o")])
    assert assistente.executar(a, ui=ui, fabrica_sessao=lambda profile_name=None: boto3.Session()) == 0
    assert _tags(conta, "b-novo") == {}
    assert (tmp_path / "o" / "resultado_dry_run_cliente.md").exists()


def test_nao_interativo_com_aplicar_escreve(conta, tmp_path):
    args = ["--nao-interativo", "--profile", "x", "--valor", VALOR, "--regioes", "us-east-1", "--aplicar"]
    ui = assistente.Interface(interativo=False, saida=lambda t: None,
                              respostas=assistente._respostas(assistente.parser().parse_args(args)))
    a = assistente.parser().parse_args([*args, "--saida", str(tmp_path / "o")])
    assert assistente.executar(a, ui=ui, fabrica_sessao=lambda profile_name=None: boto3.Session()) == 0
    assert _tags(conta, "b-novo") == {"aws-apn-id": VALOR}
