"""Modo interativo — `python3 iniciar.py`.

Um comando só, que conduz quem opera pelas etapas com resumo e confirmação
a cada passo:

  1. conta (perfil AWS) e valor da tag;
  2. regiões e checagem prévia (somente leitura);
  3. mapeamento (Etapa 1, somente leitura) -> resumo;
  4. decisão (Etapa 2a, sem chamada de API) -> resumo do que seria feito;
  5. simulação (Etapa 2b, dry-run, só leitura) -> relatório do cliente;
  6. tagueamento de verdade (Etapa 2c) -> só depois de digitar o ID da conta;
  7. implantação da automação contínua (stacks hub + regionais), opcional.

Nada é escrito na conta antes do passo 6, e o passo 6 exige digitar o ID da
conta. Tudo o que é gerado fica numa pasta `saida-prm-<conta>-<data>/`.

Só biblioteca padrão + boto3. Para CI/automação existe `--nao-interativo`
(as respostas vêm dos parâmetros; escrita só com `--aplicar` explícito).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess  # nosec B404 — só executa deploy/implantar_conta.sh com lista de argumentos (sem shell)
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable

import boto3
from botocore.exceptions import BotoCoreError, ClientError, ProfileNotFound

from . import decision, pipeline, preflight, regions, relatorio_cliente, report, services, tag_execution
from .tag_status import VALOR_PADRAO

_RAIZ = Path(__file__).resolve().parent.parent

# Larguras do resumo em tabela
_L = 38


class Interface:
    """Entrada/saída do assistente. Injetável para teste e para o modo não
    interativo (respostas pré-definidas)."""

    def __init__(self, entrada: Callable[[str], str] = input, saida: Callable[[str], None] = print,
                 respostas: dict | None = None, interativo: bool = True):
        self._entrada = entrada
        self._saida = saida
        self.respostas = respostas or {}
        self.interativo = interativo

    def mostrar(self, texto: str = "") -> None:
        self._saida(texto)

    def titulo(self, texto: str) -> None:
        self._saida("")
        self._saida(f"=== {texto} " + "=" * max(3, 70 - len(texto)))

    def perguntar(self, chave: str, pergunta: str, padrao: str = "") -> str:
        if not self.interativo:
            return str(self.respostas.get(chave, padrao))
        sufixo = f" [{padrao}]" if padrao else ""
        try:
            resp = self._entrada(f"{pergunta}{sufixo}: ").strip()
        except EOFError:
            resp = ""
        return resp or padrao

    def sim_ou_nao(self, chave: str, pergunta: str, padrao: bool = False) -> bool:
        if not self.interativo:
            return bool(self.respostas.get(chave, padrao))
        dica = "S/n" if padrao else "s/N"
        while True:
            try:
                resp = self._entrada(f"{pergunta} ({dica}): ").strip().lower()
            except EOFError:
                return padrao
            if not resp:
                return padrao
            if resp in ("s", "sim", "y", "yes"):
                return True
            if resp in ("n", "nao", "não", "no"):
                return False
            self._saida("  responda s ou n")


def _tabela(ui: Interface, pares: list[tuple[str, object]]) -> None:
    for chave, valor in pares:
        ui.mostrar(f"  {chave:.<{_L}} {valor}")


def _top(contagem: dict, n: int = 8) -> str:
    itens = sorted(contagem.items(), key=lambda kv: -kv[1])
    texto = ", ".join(f"{k}: {v}" for k, v in itens[:n])
    if len(itens) > n:
        texto += f" (+{len(itens) - n})"
    return texto or "—"


def _gravar(pasta: Path, nome: str, dados: dict) -> Path:
    p = pasta / nome
    p.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# passos
# ---------------------------------------------------------------------------


def passo_conta(ui: Interface, args, fabrica_sessao=boto3.Session):
    ui.titulo("1. Conta e valor da tag")
    perfis = []
    try:
        perfis = boto3.Session().available_profiles
    except Exception:  # noqa: BLE001 — lista de perfis é só uma dica na tela
        logging.getLogger(__name__).debug("não foi possível listar perfis AWS", exc_info=True)
    if perfis and ui.interativo:
        ui.mostrar("  perfis AWS configurados: " + ", ".join(perfis[:15]) + (" ..." if len(perfis) > 15 else ""))
    perfil = args.profile or ui.perguntar("profile", "Perfil AWS da conta do cliente",
                                          os.environ.get("AWS_PROFILE", "default"))
    try:
        sessao = fabrica_sessao(profile_name=perfil or None)
        ident = sessao.client("sts").get_caller_identity()
    except (ProfileNotFound, ClientError, BotoCoreError) as exc:
        ui.mostrar(f"  !! não consegui usar o perfil '{perfil}': {exc}")
        if "botocore[crt]" in str(exc):
            ui.mostrar("     O 'aws login' grava credenciais que o Python só lê com o pacote CRT. Rode:")
            ui.mostrar('       pip install "boto3[crt]"')
        else:
            ui.mostrar("     (sessão expirada? rode 'aws sso login' / 'aws login' e tente de novo)")
        return None
    conta = ident["Account"]
    _tabela(ui, [("conta", conta), ("identidade", ident["Arn"])])
    if not ui.sim_ou_nao("conta_ok", "É esta a conta do cliente?", padrao=not ui.interativo):
        ui.mostrar("  ok, nada foi feito.")
        return None

    valor = args.valor or ui.perguntar("valor", "Valor da tag aws-apn-id", VALOR_PADRAO)
    while not _valor_ok(valor):
        if not ui.interativo:
            ui.mostrar(f"  !! valor inválido: {valor!r} (formato pc:<letras/números>)")
            return None
        valor = ui.perguntar("valor", "  formato pc:<letras/números> — tente de novo")
    return sessao, perfil, conta, valor


def _valor_ok(valor: str | None) -> bool:
    return bool(valor) and re.fullmatch(r"pc:[A-Za-z0-9]+", valor) is not None


def passo_regioes(ui: Interface, args, sessao, pasta: Path) -> list[str] | None:
    ui.titulo("2. Regiões e checagem prévia (somente leitura)")
    ativas = regions.get_active_regions(sessao)
    ui.mostrar(f"  {len(ativas)} região(ões) ativa(s): {', '.join(ativas)}")
    escolha = args.regioes or ui.perguntar("regioes", "Regiões a mapear (Enter = todas; ou lista a,b,c)", "")
    if escolha:
        pedidas = [r.strip() for r in escolha.split(",") if r.strip()]
        fora = sorted(set(pedidas) - set(ativas))
        if fora:
            ui.mostrar(f"  !! não ativas nesta conta: {', '.join(fora)}")
            return None
        ativas = [r for r in ativas if r in pedidas]
    try:
        pf = preflight.run_preflight(sessao)
        _gravar(pasta, "preflight.json", pf)
        sem = [r for r, st in pf["cloudtrail_por_regiao"].items() if st == "sem_trail" and r in ativas]
        incertas = [r for r, st in pf["cloudtrail_por_regiao"].items() if st == "incerto" and r in ativas]
        _tabela(ui, [
            ("CloudTrail (eventos de escrita)", "ok" if not (sem or incertas) else
             (f"SEM trail em {', '.join(sem)}" if sem else "") + (f" incerto em {', '.join(incertas)}" if incertas else "")),
        ])
        if sem:
            ui.mostrar("  · sem trail, a reação em tempo real a recursos novos (Etapa 3) só vê EC2/ECS nessas regiões;")
            ui.mostrar("    o mapeamento e o tagueamento de agora funcionam normalmente. Detalhes: preflight.json")
    except (ClientError, BotoCoreError) as exc:
        ui.mostrar(f"  (checagem de CloudTrail não rodou: {exc} — segue sem ela)")
    return ativas


def passo_mapear(ui: Interface, args, sessao, conta, valor, regioes, pasta: Path) -> dict | None:
    ui.titulo("3. Mapeamento (Etapa 1 — somente leitura)")
    ui.mostrar(f"  Lê os recursos de {len(regioes)} região(ões). Nenhuma escrita. Pode levar alguns minutos.")
    ui.mostrar("  Inclui o histórico do CloudTrail (90 dias, grátis) para achar Terraform/Pulumi/CloudFormation.")
    if not ui.sim_ou_nao("mapear", "Fazer o mapeamento?", padrao=True):
        return None
    lista_servicos = services.load_services()
    todos, falhas = [], []
    for i, regiao in enumerate(regioes, 1):
        rec, f = pipeline.descobrir_regioes(
            sessao, conta, [regiao], lista_servicos, valor, orcamento_cloudtrail_s=args.orcamento_cloudtrail
        )
        todos += rec
        falhas += f
        bloqueada = any(x.get("etapa") == pipeline.ETAPA_REGIAO_NEGADA_SCP for x in f)
        ui.mostrar(f"  [{i}/{len(regioes)}] {regiao:<16} {len(rec):>5} recurso(s)" + ("  (bloqueada por SCP)" if bloqueada else ""))
    etapa1 = report.build_report(conta, valor, report.dedupe_by_arn(todos), None, falhas)
    _gravar(pasta, "mapa.json", etapa1)
    r = etapa1["resumo"]
    ui.mostrar("")
    _tabela(ui, [
        ("recursos encontrados", r["total_recursos"]),
        ("sem a tag", r["por_status_tag"]["sem_tag"]),
        ("já com o nosso valor", r["por_status_tag"]["ok"]),
        ("com outro valor (outro parceiro)", r["por_status_tag"]["conflito"]),
        ("gerenciados por IaC", sum(v for k, v in r["por_status_iac"].items() if k != "desconhecido")),
        ("serviços (top)", _top(r["por_servico"])),
        ("falhas de leitura", r["total_falhas_descoberta"]),
    ])
    if falhas:
        regs = sorted({f["regiao"] for f in falhas if f.get("etapa") == pipeline.ETAPA_REGIAO_NEGADA_SCP})
        if regs:
            ui.mostrar(f"  regiões bloqueadas por SCP (sem acesso, ignoradas): {', '.join(regs)}")
        outras = Counter(f.get("etapa") for f in falhas if f.get("etapa") != pipeline.ETAPA_REGIAO_NEGADA_SCP)
        if outras:
            ui.mostrar(f"  leituras que falharam (por etapa): {_top(dict(outras), 6)} — detalhes em mapa.json/execucao.log")
    return etapa1


def passo_decidir(ui: Interface, etapa1: dict, valor: str, pasta: Path) -> dict:
    ui.titulo("4. Decisão (Etapa 2a — sem chamada de API)")
    dec = decision.build_decision_report(etapa1, valor)
    _gravar(pasta, "decisao.json", dec)
    pd = dec["resumo"]["por_decisao"]
    ferramentas = Counter(
        (r["iac"].get("ferramenta") or r["iac"]["tipo"]) for r in dec["recursos"] if r["decisao"] == "pular_iac"
    )
    _tabela(ui, [
        ("taguear (sem tag)", pd["taguear"]),
        ("sobrescrever (outro parceiro)", pd["sobrescrever"]),
        ("adicionar ao lado de chave parecida", pd["taguear_com_tag_similar"]),
        ("pular — IaC (lista p/ o cliente)", f"{pd['pular_iac']}  {_top(dict(ferramentas))}" if pd["pular_iac"] else 0),
        ("já ok", pd["ja_ok"]),
        ("erros de classificação", dec["resumo"]["total_erros"]),
    ])
    sobrescritos = [r for r in dec["recursos"] if r["decisao"] == "sobrescrever"]
    if sobrescritos:
        valores = Counter(r["valor_tag_atual"] for r in sobrescritos)
        ui.mostrar(f"  valores que seriam sobrescritos: {_top(dict(valores), 5)}")
    return dec


def _resumo_execucao(ui: Interface, execucao: dict) -> None:
    r = execucao["resumo"]
    pc = r["por_categoria_final"]
    sucesso = pc.get("tagueado_sucesso", 0) + pc.get("simulado_sucesso", 0)
    _tabela(ui, [
        ("sucesso" + (" (simulado)" if execucao["modo"] == "dry_run" else ""), sucesso),
        ("  por ação", _top(r.get("por_acao", {}))),
        ("pulados — IaC", pc.get("pulado_iac", 0)),
        ("já ok", pc.get("ja_ok", 0)),
        ("falhas", pc.get("falhou", 0)),
    ])
    falhas = [x for x in execucao["recursos"] if x["categoria_final"] == "falhou"]
    if falhas:
        codigos = Counter(((x.get("detalhe_erro") or {}).get("codigo") or x.get("resultado")) for x in falhas)
        ui.mostrar(f"  motivos das falhas: {_top(dict(codigos), 5)}")


def _gravar_cliente(pasta: Path, nome: str, execucao: dict) -> list[Path]:
    rel = relatorio_cliente.build_relatorio_cliente(execucao)
    arquivos = [_gravar(pasta, f"{nome}_cliente.json", rel)]
    (pasta / f"{nome}_cliente.csv").write_text(relatorio_cliente.to_csv(rel), encoding="utf-8")
    (pasta / f"{nome}_cliente.md").write_text(relatorio_cliente.to_markdown(rel), encoding="utf-8")
    return [pasta / f"{nome}_cliente.md", pasta / f"{nome}_cliente.csv"] + arquivos


def passo_simular(ui: Interface, sessao, dec: dict, valor: str, pasta: Path) -> dict | None:
    ui.titulo("5. Simulação (Etapa 2b — dry-run, só leitura)")
    n = sum(dec["resumo"]["por_decisao"][d] for d in decision.DECISOES_QUE_TAGUEIAM)
    ui.mostrar(f"  Relê o estado atual de {n} recurso(s) e mostra o que seria feito. Nenhuma escrita.")
    if not ui.sim_ou_nao("simular", "Rodar a simulação?", padrao=True):
        return None
    execucao = tag_execution.run_tagging_execution(dec, session=sessao, expected_tag_value=valor, dry_run=True)
    _gravar(pasta, "resultado_dry_run.json", execucao)
    _resumo_execucao(ui, execucao)
    arquivos = _gravar_cliente(pasta, "resultado_dry_run", execucao)
    ui.mostrar(f"  relatório para o cliente (prévia): {arquivos[0]}")
    return execucao


def _filtrar_por_arquivo(dec: dict, caminho: str) -> dict:
    permitidos = {
        linha.split("\t", 1)[0].split(",", 1)[0].strip()
        for linha in Path(caminho).read_text(encoding="utf-8").splitlines()
        if linha.strip() and not linha.startswith("#")
    }
    return {**dec, "recursos": [r for r in dec["recursos"] if r["arn"] in permitidos],
            "erros": [r for r in dec.get("erros", []) if r.get("arn") in permitidos]}


def passo_aplicar(ui: Interface, args, sessao, conta, dec: dict, valor: str, pasta: Path) -> dict | None:
    ui.titulo("6. Tagueamento de verdade (Etapa 2c — ESCREVE na conta)")
    alvo = [r for r in dec["recursos"] if r["decisao"] in decision.DECISOES_QUE_TAGUEIAM]
    lista = pasta / "a_taguear.tsv"
    lista.write_text(
        "# arn\tdecisao\tservico\tvalor_atual\n"
        + "".join(f"{r['arn']}\t{r['decisao']}\t{r['servico']}\t{r.get('valor_tag_atual') or ''}\n" for r in alvo),
        encoding="utf-8",
    )
    ui.mostrar(f"  {len(alvo)} recurso(s) receberiam aws-apn-id={valor} (lista em {lista}).")
    ui.mostrar("  Recursos de IaC NÃO são tocados. Exige consentimento do cliente (guia PRM).")
    if not alvo:
        ui.mostrar("  nada a taguear.")
        return None
    if not ui.sim_ou_nao("aplicar", "Aplicar a tag agora?", padrao=False):
        ui.mostrar("  ok, nada foi escrito. Dá para aplicar depois com:")
        ui.mostrar(f"    python3 -m aws_prm_tagging.main apply --profile <perfil> --input {pasta / 'decisao.json'} --live")
        return None
    if ui.interativo:
        caminho = ui.perguntar("arquivo_arns", "Restringir a um arquivo de ARNs? (Enter = todos da lista)", "")
        if caminho:
            dec = _filtrar_por_arquivo(dec, caminho)
            alvo = [r for r in dec["recursos"] if r["decisao"] in decision.DECISOES_QUE_TAGUEIAM]
            ui.mostrar(f"  restrito a {len(alvo)} recurso(s)")
        confirmacao = ui.perguntar("confirmacao", f"Para confirmar, digite o ID da conta ({conta})")
        if confirmacao != conta:
            ui.mostrar("  ID diferente — cancelado, nada foi escrito.")
            return None
    elif args.arquivo_arns:
        dec = _filtrar_por_arquivo(dec, args.arquivo_arns)
    execucao = tag_execution.run_tagging_execution(dec, session=sessao, expected_tag_value=valor, dry_run=False)
    _gravar(pasta, "resultado_live.json", execucao)
    _resumo_execucao(ui, execucao)
    arquivos = _gravar_cliente(pasta, "resultado_live", execucao)
    ui.mostrar(f"  relatório para o cliente: {arquivos[0]}  (e .csv/.json)")
    return execucao


def passo_implantar(ui: Interface, args, perfil, conta, valor, regioes, pasta: Path, executor=subprocess.run) -> bool:
    ui.titulo("7. Automação contínua (stacks hub + regionais)")
    ui.mostrar("  Reage a recursos NOVOS em tempo real (Etapa 3), alerta remoção da tag e faz varredura diária.")
    ui.mostrar("  O QUE CRIA nesta conta: stack prm-hub (1 região) e prm-regional (cada região): Lambdas,")
    ui.mostrar("  Step Functions, regras do EventBridge, fila DLQ, tópico SNS. CUSTO: por uso — centavos/mês")
    ui.mostrar("  em conta normal; sem custo parado relevante. REMOVER: deploy/remover_conta.sh --profile <perfil>.")
    if not ui.sim_ou_nao("implantar", "Implantar a automação contínua nesta conta?", padrao=False):
        ui.mostrar("  ok. Para implantar depois: deploy/implantar_conta.sh (ver docs/MVP-2.md).")
        return False
    hub = args.hub or ui.perguntar("hub", "Região da stack hub", "us-east-1" if "us-east-1" in regioes else regioes[0])
    email = args.email or ui.perguntar("email", "E-mail para as notificações (vazio = sem e-mail)", "")
    varredura = args.modo_varredura or ui.perguntar("modo_varredura", "Varredura diária: dry-run | live | desligado", "dry-run")
    if varredura not in ("dry-run", "live", "desligado"):
        ui.mostrar(f"  modo inválido: {varredura}")
        return False
    regioes_stack = sorted(set(regioes) | {"us-east-1"})
    cmd = ["bash", str(_RAIZ / "deploy/implantar_conta.sh"), "--profile", perfil, "--valor", valor,
           "--hub", hub, "--regioes", ",".join(regioes_stack),
           # O tagueamento inicial acabou de rodar por aqui (passos 5/6).
           "--modo-inicial", "desligado", "--modo-varredura", varredura, "--sim"]
    if email:
        cmd += ["--email", email]
    faltando = [c for c in ("sam", "make") if shutil.which(c) is None]
    if faltando:
        ui.mostrar(f"  !! falta {', '.join(faltando)} no PATH (pré-requisito do deploy). Comando para depois:")
        ui.mostrar("     " + " ".join(cmd))
        return False
    ui.mostrar("  $ " + " ".join(cmd))
    r = executor(cmd, cwd=_RAIZ)
    ok = getattr(r, "returncode", 1) == 0
    ui.mostrar("  implantação concluída" if ok else "  !! implantação terminou com falha — ver mensagens acima")
    return ok


# ---------------------------------------------------------------------------
# orquestração
# ---------------------------------------------------------------------------


def _log_em_arquivo(caminho: Path, detalhes: bool) -> None:
    """Logs internos (com stack trace) vão para o arquivo; o terminal fica
    com o resumo do assistente. --detalhes mostra os logs também na tela."""
    raiz = logging.getLogger()
    for h in list(raiz.handlers):
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) and not detalhes:
            raiz.removeHandler(h)
    for h in list(raiz.handlers):
        if getattr(h, "_prm_assistente", False):
            raiz.removeHandler(h)
            h.close()
    arq = logging.FileHandler(caminho, encoding="utf-8")
    arq._prm_assistente = True
    arq.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    arq.setLevel(logging.INFO)
    raiz.addHandler(arq)
    raiz.setLevel(logging.INFO)


def executar(args, ui: Interface | None = None, fabrica_sessao=boto3.Session, executor=subprocess.run) -> int:
    ui = ui or Interface(interativo=not args.nao_interativo, respostas=_respostas(args))
    ui.mostrar("Assistente AWS PRM — tagueamento aws-apn-id")
    ui.mostrar("Nada é escrito na conta antes do passo 6, que pede o ID da conta para confirmar.")
    r = passo_conta(ui, args, fabrica_sessao)
    if r is None:
        return 1
    sessao, perfil, conta, valor = r
    pasta = Path(args.saida or f"saida-prm-{conta}-{datetime.now():%Y%m%d-%H%M%S}")
    pasta.mkdir(parents=True, exist_ok=True)
    _log_em_arquivo(pasta / "execucao.log", getattr(args, "detalhes", False))
    ui.mostrar(f"  arquivos desta execução: {pasta.resolve()}  (contêm ARNs da conta — não versionar)")
    regioes = passo_regioes(ui, args, sessao, pasta)
    if not regioes:
        return 1

    etapa1 = passo_mapear(ui, args, sessao, conta, valor, regioes, pasta)
    if etapa1 is None:
        return 0
    dec = passo_decidir(ui, etapa1, valor, pasta)
    if passo_simular(ui, sessao, dec, valor, pasta) is not None:
        passo_aplicar(ui, args, sessao, conta, dec, valor, pasta)
    passo_implantar(ui, args, perfil, conta, valor, regioes, pasta, executor=executor)

    ui.titulo("Fim")
    for p in sorted(pasta.iterdir()):
        ui.mostrar(f"  {p.name}")
    ui.mostrar("  Envie ao cliente o *_cliente.md (recursos de IaC + estado anterior).")
    return 0


def _respostas(args) -> dict:
    """Respostas do modo não interativo: leitura sempre; escrita só com flag."""
    return {
        "conta_ok": True,
        "mapear": True,
        "simular": True,
        "aplicar": bool(args.aplicar),
        "implantar": bool(args.implantar),
        "regioes": args.regioes or "",
    }


def parser(sub: argparse._SubParsersAction | None = None) -> argparse.ArgumentParser:
    p = sub.add_parser("iniciar", help="Modo interativo: mapear, simular, taguear e implantar, passo a passo.") if sub \
        else argparse.ArgumentParser(prog="iniciar", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default=None)
    p.add_argument("--valor", default=None, help=f"pc:<product code> (default: {VALOR_PADRAO})")
    p.add_argument("--regioes", default=None, help="lista a,b,c (default: todas as ativas)")
    p.add_argument("--saida", default=None, help="pasta de saída (default: saida-prm-<conta>-<data>)")
    p.add_argument("--orcamento-cloudtrail", type=float, default=None,
                   help="tempo máximo (s) por região no histórico do CloudTrail (default: sem limite)")
    p.add_argument("--nao-interativo", action="store_true", help="sem perguntas (CI); escrita só com --aplicar")
    p.add_argument("--aplicar", action="store_true", help="(não interativo) aplica a tag de verdade")
    p.add_argument("--arquivo-arns", default=None, help="(não interativo) restringe a escrita a estes ARNs")
    p.add_argument("--implantar", action="store_true", help="(não interativo) implanta as stacks contínuas")
    p.add_argument("--hub", default=None)
    p.add_argument("--email", default=None)
    p.add_argument("--modo-varredura", default=None, choices=["dry-run", "live", "desligado"])
    p.add_argument("--detalhes", action="store_true", help="mostra os logs internos (INFO)")
    p.set_defaults(func=executar_cli)
    return p


def executar_cli(args) -> int:
    if args.nao_interativo and not (args.profile and args.valor):
        print("--nao-interativo exige --profile e --valor")
        return 1
    try:
        return executar(args)
    except KeyboardInterrupt:
        print("\ninterrompido — o que já foi gravado está na pasta de saída; nada além do confirmado foi escrito.")
        return 130


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
