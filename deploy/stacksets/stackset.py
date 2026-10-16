#!/usr/bin/env python3
"""Rollout da automação PRM por CloudFormation StackSets.

Roda na conta PAYER (gerenciamento) do cliente — ou numa conta
administradora delegada de StackSets (`--call-as DELEGATED_ADMIN`).

Subcomandos (todos mostram o plano e pedem confirmação antes de escrever):

  publicar   cria (se faltar) um bucket de artefatos por região na payer e
             envia o código + a layer gerados por `empacotar.py`.
  implantar  cria/atualiza os StackSets `prm-hub` e `prm-regional`
             (service-managed, auto-deployment) e implanta numa OU com o
             valor de contrato daquela OU.
  status     resumo das instâncias por status/região (somente leitura).
  remover    remove as instâncias de uma OU (e o StackSet, se vazio);
             opcionalmente apaga os buckets de artefatos.

Ordem típica:
  python3 deploy/stacksets/empacotar.py --versao 2.0.0
  python3 deploy/stacksets/stackset.py publicar  --profile payer --dist dist/stacksets/2.0.0 --regioes us-east-1,sa-east-1
  python3 deploy/stacksets/stackset.py implantar --profile payer --dist dist/stacksets/2.0.0 \\
      --ou ou-abcd-11111111 --valor pc:XXXX --hub-regiao us-east-1

Limitação da AWS: StackSet service-managed NÃO implanta na própria conta de
gerenciamento. Se a payer tiver workloads, use deploy/implantar_conta.sh nela.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

NOME_HUB = "prm-hub"
NOME_REGIONAL = "prm-regional"
CAPACIDADES = ["CAPABILITY_IAM"]
_VALOR_RE = re.compile(r"^pc:[A-Za-z0-9]+$")
_OU_RE = re.compile(r"^(ou-[a-z0-9]{4,32}-[a-z0-9]{8,32}|r-[a-z0-9]{4,32})$")


# ---------------------------------------------------------------------------
# utilidades
# ---------------------------------------------------------------------------


def _lista(texto: str | None) -> list[str]:
    return [x.strip() for x in (texto or "").split(",") if x.strip()]


def _confirmar(args, pergunta="Confirmar? (digite 'sim'): ") -> bool:
    if getattr(args, "sim", False):
        return True
    try:
        return input(pergunta).strip().lower() == "sim"
    except EOFError:
        return False


def _ler_manifest(dist: Path) -> dict:
    m = dist / "manifest.json"
    if not m.exists():
        raise SystemExit(f"{m} não existe — rode empacotar.py antes")
    return json.loads(m.read_text(encoding="utf-8"))


def _estado_publicacao(dist: Path) -> dict:
    p = dist / "publicado.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ---------------------------------------------------------------------------
# publicar — buckets de artefatos por região
# ---------------------------------------------------------------------------


def politica_bucket(bucket: str, org_id: str, partition: str = "aws") -> dict:
    """Leitura do código só por principais da organização; só TLS."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "LeituraPelaOrganizacao",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": f"arn:{partition}:s3:::{bucket}/prm-tagging/*",
                "Condition": {"StringEquals": {"aws:PrincipalOrgID": org_id}},
            },
            {
                "Sid": "SomenteTLS",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [f"arn:{partition}:s3:::{bucket}", f"arn:{partition}:s3:::{bucket}/*"],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            },
        ],
    }


def _bucket_existe(s3, bucket: str) -> bool:
    try:
        s3.head_bucket(Bucket=bucket)
        return True
    except ClientError as exc:
        codigo = exc.response.get("Error", {}).get("Code", "")
        if codigo in ("404", "NoSuchBucket", "NotFound"):
            return False
        raise


def _criar_bucket(s3, bucket: str, regiao: str, org_id: str) -> None:
    if regiao == "us-east-1":
        s3.create_bucket(Bucket=bucket)
    else:
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": regiao})
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]},
    )
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(politica_bucket(bucket, org_id)))
    # Versões antigas (de publicações anteriores) somem após 30 dias.
    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "prm-versoes-antigas",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "prm-tagging/"},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                }
            ]
        },
    )


def _objeto_igual(s3, bucket: str, chave: str, sha: str) -> bool:
    try:
        meta = s3.head_object(Bucket=bucket, Key=chave).get("Metadata", {})
        return meta.get("sha256") == sha
    except ClientError:
        return False


def cmd_publicar(args, session=None) -> int:
    session = session or boto3.Session(profile_name=args.profile)
    dist = Path(args.dist)
    manifest = _ler_manifest(dist)
    conta = session.client("sts").get_caller_identity()["Account"]
    org_id = args.org_id or session.client("organizations").describe_organization()["Organization"]["Id"]
    prefixo = args.prefixo or f"prm-tagging-{conta}"
    regioes = _lista(args.regioes)
    if not regioes:
        raise SystemExit("--regioes é obrigatório (as regiões onde as contas-membro terão stacks)")
    arquivos = {a["chave"]: a["sha256"] for a in manifest["artefatos"]}
    total_mb = sum((dist / "artefatos" / c).stat().st_size for c in arquivos) / 1e6

    print("== Plano: publicar artefatos (conta payer)")
    print(f"  conta ............ {conta}   organização {org_id}")
    print(f"  versão ........... {manifest['versao']}   (boto3 da layer: {manifest.get('boto3_layer')})")
    print(f"  buckets .......... {prefixo}-<região> em: {', '.join(regioes)}")
    print(f"  arquivos ......... {len(arquivos)} ({total_mb:.1f} MB por região)")
    print("  O QUE CRIA: 1 bucket S3 por região (privado, SSE-S3, versionado, leitura só pela organização).")
    print(f"  CUSTO ESTIMADO: ~{total_mb * len(regioes) / 1000 * 0.023:.4f} US$/mês de armazenamento + centavos de PUT.")
    print(f"  REMOVER: python3 deploy/stacksets/stackset.py remover --profile {args.profile} --somente-artefatos "
          f"--dist {dist} --regioes {','.join(regioes)}")
    if args.somente_mostrar or not _confirmar(args):
        print("nada foi feito")
        return 0

    for regiao in regioes:
        bucket = f"{prefixo}-{regiao}"
        s3 = session.client("s3", region_name=regiao)
        if not _bucket_existe(s3, bucket):
            print(f"-- {regiao}: criando {bucket}")
            _criar_bucket(s3, bucket, regiao, org_id)
        else:
            print(f"-- {regiao}: {bucket} já existe")
        for chave, sha in arquivos.items():
            if _objeto_igual(s3, bucket, chave, sha):
                continue
            with open(dist / "artefatos" / chave, "rb") as f:
                s3.put_object(Bucket=bucket, Key=chave, Body=f, Metadata={"sha256": sha})
            print(f"   enviado {chave}")
    (dist / "publicado.json").write_text(
        json.dumps({"prefixo": prefixo, "regioes": regioes, "org_id": org_id, "conta": conta}, indent=2),
        encoding="utf-8",
    )
    print("OK: artefatos publicados")
    return 0


# ---------------------------------------------------------------------------
# implantar
# ---------------------------------------------------------------------------


def _parametros(pares: dict[str, str]) -> list[dict]:
    return [{"ParameterKey": k, "ParameterValue": v} for k, v in pares.items()]


def _stackset_existe(cfn, nome: str, call_as: str) -> bool:
    try:
        cfn.describe_stack_set(StackSetName=nome, CallAs=call_as)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "StackSetNotFoundException":
            return False
        raise


def _esperar_operacao(cfn, nome: str, op_id: str, call_as: str, intervalo=15, timeout=7200) -> str:
    inicio = time.time()
    while True:
        op = cfn.describe_stack_set_operation(StackSetName=nome, OperationId=op_id, CallAs=call_as)["StackSetOperation"]
        status = op["Status"]
        if status not in ("RUNNING", "QUEUED", "STOPPING"):
            return status
        if time.time() - inicio > timeout:
            return "TIMEOUT"
        print(f"   {nome}: {status}...", flush=True)
        time.sleep(intervalo)


def _falhas_operacao(cfn, nome: str, op_id: str, call_as: str) -> list[dict]:
    falhas = []
    pag = cfn.get_paginator("list_stack_set_operation_results")
    for pagina in pag.paginate(StackSetName=nome, OperationId=op_id, CallAs=call_as):
        for r in pagina.get("Summaries", []):
            if r.get("Status") != "SUCCEEDED":
                falhas.append(
                    {"conta": r.get("Account"), "regiao": r.get("Region"), "status": r.get("Status"),
                     "motivo": r.get("StatusReason")}
                )
    return falhas


def _garantir_stackset(cfn, nome: str, template: str, params: dict, descricao: str, call_as: str) -> str | None:
    """Cria ou atualiza o StackSet. Devolve o OperationId de um update (ou
    None na criação, que não tem operação)."""
    comum = dict(
        StackSetName=nome,
        TemplateBody=template,
        Parameters=_parametros(params),
        Capabilities=CAPACIDADES,
        Description=descricao,
        CallAs=call_as,
        ManagedExecution={"Active": True},
    )
    if not _stackset_existe(cfn, nome, call_as):
        cfn.create_stack_set(
            PermissionModel="SERVICE_MANAGED",
            AutoDeployment={"Enabled": True, "RetainStacksOnAccountRemoval": False},
            **comum,
        )
        print(f"   StackSet {nome} criado")
        return None
    op = cfn.update_stack_set(
        OperationPreferences={"RegionConcurrencyType": "PARALLEL", "FailureTolerancePercentage": 100,
                              "MaxConcurrentPercentage": 25},
        **comum,
    )
    print(f"   StackSet {nome}: update (operação {op['OperationId']})")
    return op["OperationId"]


def _instancias_da_ou(cfn, nome: str, call_as: str) -> set[tuple[str, str]]:
    """(conta, região) já existentes — o StackSet service-managed lista por conta."""
    existentes = set()
    pag = cfn.get_paginator("list_stack_instances")
    for pagina in pag.paginate(StackSetName=nome, CallAs=call_as):
        for i in pagina.get("Summaries", []):
            existentes.add((i.get("OrganizationalUnitId") or "", i["Region"]))
    return existentes


def _implantar_instancias(cfn, nome, ous, regioes, overrides, call_as, args) -> tuple[str, list[dict]]:
    prefs = {
        "RegionConcurrencyType": "PARALLEL",
        "FailureTolerancePercentage": args.tolerancia_falha_pct,
        "MaxConcurrentPercentage": args.max_concorrencia_pct,
    }
    existentes = {(ou, r) for ou, r in _instancias_da_ou(cfn, nome, call_as)}
    novas = [r for r in regioes if not all((ou, r) in existentes for ou in ous)]
    ja = [r for r in regioes if r not in novas]
    resultado = []
    status_final = "SUCCEEDED"
    for regs, chamada in ((novas, cfn.create_stack_instances), (ja, cfn.update_stack_instances)):
        if not regs:
            continue
        op = chamada(
            StackSetName=nome,
            DeploymentTargets={"OrganizationalUnitIds": ous},
            Regions=regs,
            ParameterOverrides=_parametros(overrides),
            OperationPreferences=prefs,
            CallAs=call_as,
        )
        print(f"   {nome}: {'criar' if chamada == cfn.create_stack_instances else 'atualizar'} instâncias em "
              f"{', '.join(regs)} (operação {op['OperationId']})")
        status = _esperar_operacao(cfn, nome, op["OperationId"], call_as, intervalo=args.intervalo)
        resultado += _falhas_operacao(cfn, nome, op["OperationId"], call_as)
        if status != "SUCCEEDED":
            status_final = status
    return status_final, resultado


def _checar_acesso_confiavel(cfn, args) -> bool:
    try:
        st = cfn.describe_organizations_access(CallAs=args.call_as)["Status"]
    except ClientError as exc:
        print(f"!! não foi possível ler o acesso confiável dos StackSets: {exc}")
        return False
    if st == "ENABLED":
        return True
    print("!! Acesso confiável (trusted access) dos StackSets com o Organizations está DESATIVADO.")
    if args.ativar_acesso_confiavel:
        print("   ativando (cloudformation activate-organizations-access)...")
        cfn.activate_organizations_access(CallAs=args.call_as)
        return True
    print("   Ative com --ativar-acesso-confiavel, ou no console: CloudFormation > StackSets > Ativar acesso confiável.")
    return False


def cmd_implantar(args, session=None) -> int:
    session = session or boto3.Session(profile_name=args.profile)
    dist = Path(args.dist)
    manifest = _ler_manifest(dist)
    pub = _estado_publicacao(dist)
    prefixo = args.prefixo or pub.get("prefixo")
    regioes = _lista(args.regioes) or pub.get("regioes") or []
    ous = _lista(args.ou)
    if not prefixo or not regioes:
        raise SystemExit("rode 'publicar' antes (ou informe --prefixo e --regioes)")
    if not _VALOR_RE.match(args.valor or ""):
        raise SystemExit("--valor precisa ser pc:<código alfanumérico>")
    if not ous or not all(_OU_RE.match(o) for o in ous):
        raise SystemExit("--ou precisa ser ID de OU (ou-xxxx-yyyyyyyy) ou da raiz (r-xxxx)")
    if args.hub_regiao not in regioes:
        raise SystemExit(f"--hub-regiao {args.hub_regiao} precisa estar entre as regiões publicadas: {regioes}")
    if args.topico_central_arn and not args.kms_central_arn:
        raise SystemExit("--topico-central-arn exige --kms-central-arn (saída ChaveTopicoArn de template-central.yaml)")
    if "us-east-1" not in regioes:
        print("!! ATENÇÃO: us-east-1 fora da lista — eventos de serviços globais (Route 53, CloudFront) não serão vistos.")

    tpl_hub = (dist / manifest["templates"][NOME_HUB]["arquivo"]).read_text(encoding="utf-8")
    tpl_reg = (dist / manifest["templates"][NOME_REGIONAL]["arquivo"]).read_text(encoding="utf-8")
    for nome, corpo in ((NOME_HUB, tpl_hub), (NOME_REGIONAL, tpl_reg)):
        if len(corpo.encode()) > 51200:
            raise SystemExit(f"template {nome} passou de 51.200 bytes (limite do TemplateBody)")

    central = {"TopicoCentralArn": args.topico_central_arn or "", "TopicoCentralKmsArn": args.kms_central_arn or ""}
    # Parâmetros do StackSet (defaults). O valor do contrato vai como
    # override POR OU — OUs diferentes podem ter contratos diferentes.
    base_hub = {
        "ArtefatosBucketPrefixo": prefixo,
        "ExpectedTagValue": args.valor,
        "NotificationEmail": args.email or "",
        "ModoTagueamentoInicial": args.modo_inicial,
        "ModoVarreduraRecorrente": args.modo_varredura,
        "ContratoConfirmado": "true" if args.contrato_confirmado else "false",
        **central,
    }
    base_reg = {"ArtefatosBucketPrefixo": prefixo, "ExpectedTagValue": args.valor, "HubRegion": args.hub_regiao, **central}
    over_hub = {"ExpectedTagValue": args.valor, "ContratoConfirmado": base_hub["ContratoConfirmado"],
                "ModoTagueamentoInicial": args.modo_inicial, "ModoVarreduraRecorrente": args.modo_varredura}
    over_reg = {"ExpectedTagValue": args.valor}

    print("== Plano: implantar por StackSets")
    print(f"  versão ............ {manifest['versao']}  (artefatos em {prefixo}-<região>)")
    print(f"  OU(s) ............. {', '.join(ous)}  (todas as contas, inclusive as que entrarem depois)")
    print(f"  valor da tag ...... aws-apn-id = {args.valor}  (override desta OU)")
    print(f"  {NOME_HUB} ......... 1 stack por conta em {args.hub_regiao}")
    print(f"  {NOME_REGIONAL} .... 1 stack por conta em: {', '.join(regioes)}")
    print(f"  tagueamento inicial {args.modo_inicial};  varredura recorrente {args.modo_varredura}")
    print(f"  publicação ........ {'tópico central ' + args.topico_central_arn if args.topico_central_arn else 'tópico local de cada conta'}")
    print("  Etapa 3 ........... ESCREVE a tag em recursos NOVOS desde o deploy (regra MVP2: IaC é pulado).")
    if "live" in (args.modo_inicial, args.modo_varredura):
        print("  !! modo LIVE: a tag será escrita também nos recursos EXISTENTES (exige consentimento do cliente)")
    print("  CUSTO: Lambda/Step Functions/EventBridge por uso (centavos/mês por conta em uso normal);")
    print("         sem custo parado relevante. Ver docs/MVP-2.md, seção Custos.")
    print(f"  REMOVER: python3 deploy/stacksets/stackset.py remover --profile {args.profile} --ou {','.join(ous)} "
          f"--regioes {','.join(regioes)} --hub-regiao {args.hub_regiao}")
    if args.somente_mostrar or not _confirmar(args):
        print("nada foi feito")
        return 0

    cfn = session.client("cloudformation")
    if not _checar_acesso_confiavel(cfn, args):
        return 1
    falhas_total = []
    for nome, tpl, base, over, regs in (
        (NOME_HUB, tpl_hub, base_hub, over_hub, [args.hub_regiao]),
        (NOME_REGIONAL, tpl_reg, base_reg, over_reg, regioes),
    ):
        print(f"== {nome}")
        op = _garantir_stackset(cfn, nome, tpl, base, f"AWS PRM tagging {manifest['versao']}", args.call_as)
        if op:
            _esperar_operacao(cfn, nome, op, args.call_as, intervalo=args.intervalo)
            falhas_total += _falhas_operacao(cfn, nome, op, args.call_as)
        status, falhas = _implantar_instancias(cfn, nome, ous, regs, over, args.call_as, args)
        falhas_total += falhas
        print(f"   {nome}: {status}")

    print("\n== Resumo")
    if falhas_total:
        print(f"  {len(falhas_total)} instância(s) com problema (região bloqueada por SCP é causa comum):")
        for f in falhas_total[:50]:
            print(f"   - conta {f['conta']} {f['regiao']}: {f['status']} — {f['motivo']}")
    else:
        print("  todas as instâncias OK")
    print("  Acompanhe: python3 deploy/stacksets/stackset.py status --profile", args.profile)
    return 0 if not falhas_total else 2


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def cmd_status(args, session=None) -> int:
    session = session or boto3.Session(profile_name=args.profile)
    cfn = session.client("cloudformation")
    for nome in (NOME_HUB, NOME_REGIONAL):
        if not _stackset_existe(cfn, nome, args.call_as):
            print(f"{nome}: não existe")
            continue
        ss = cfn.describe_stack_set(StackSetName=nome, CallAs=args.call_as)["StackSet"]
        contagem: dict[str, int] = {}
        problemas = []
        for pagina in cfn.get_paginator("list_stack_instances").paginate(StackSetName=nome, CallAs=args.call_as):
            for i in pagina.get("Summaries", []):
                st = i.get("StackInstanceStatus", {}).get("DetailedStatus") or i.get("Status")
                contagem[st] = contagem.get(st, 0) + 1
                if st not in ("SUCCEEDED", "CURRENT"):
                    problemas.append(f"{i.get('Account')} {i.get('Region')}: {st} {i.get('StatusReason') or ''}")
        print(f"{nome} ({ss.get('Description')}): {contagem or 'sem instâncias'}")
        for p in problemas[:30]:
            print("   -", p)
    return 0


# ---------------------------------------------------------------------------
# remover
# ---------------------------------------------------------------------------


def _apagar_bucket(s3, bucket: str) -> None:
    pag = s3.get_paginator("list_object_versions")
    for pagina in pag.paginate(Bucket=bucket):
        objs = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in pagina.get("Versions", []) + pagina.get("DeleteMarkers", [])]
        for i in range(0, len(objs), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objs[i:i + 1000], "Quiet": True})
    s3.delete_bucket(Bucket=bucket)


def cmd_remover(args, session=None) -> int:
    session = session or boto3.Session(profile_name=args.profile)
    dist = Path(args.dist) if args.dist else None
    pub = _estado_publicacao(dist) if dist else {}
    regioes = _lista(args.regioes) or pub.get("regioes") or []
    ous = _lista(args.ou)
    prefixo = args.prefixo or pub.get("prefixo")
    print("== Plano: remover")
    if not args.somente_artefatos:
        if not ous or not regioes or not args.hub_regiao:
            raise SystemExit("informe --ou, --regioes e --hub-regiao")
        print(f"  instâncias de {NOME_REGIONAL} nas regiões {', '.join(regioes)} e de {NOME_HUB} em {args.hub_regiao}")
        print(f"  das OU(s) {', '.join(ous)}; StackSets apagados se ficarem vazios")
        print("  (os log groups /aws/lambda/... das funções são removidos junto — retenção definida no template)")
    if args.apagar_artefatos or args.somente_artefatos:
        if not prefixo or not regioes:
            raise SystemExit("para apagar artefatos informe --dist (com publicado.json) ou --prefixo e --regioes")
        print(f"  buckets {prefixo}-<região> em {', '.join(regioes)} (todas as versões)")
    if args.somente_mostrar or not _confirmar(args):
        print("nada foi feito")
        return 0

    if not args.somente_artefatos:
        cfn = session.client("cloudformation")
        for nome, regs in ((NOME_REGIONAL, regioes), (NOME_HUB, [args.hub_regiao])):
            if not _stackset_existe(cfn, nome, args.call_as):
                print(f"   {nome}: não existe")
                continue
            op = cfn.delete_stack_instances(
                StackSetName=nome,
                DeploymentTargets={"OrganizationalUnitIds": ous},
                Regions=regs,
                RetainStacks=False,
                OperationPreferences={"RegionConcurrencyType": "PARALLEL", "FailureTolerancePercentage": 100,
                                      "MaxConcurrentPercentage": 50},
                CallAs=args.call_as,
            )
            status = _esperar_operacao(cfn, nome, op["OperationId"], args.call_as, intervalo=args.intervalo)
            print(f"   {nome}: remoção de instâncias {status}")
            restantes = list(_instancias_da_ou(cfn, nome, args.call_as))
            if not restantes:
                cfn.delete_stack_set(StackSetName=nome, CallAs=args.call_as)
                print(f"   {nome}: StackSet apagado")
            else:
                print(f"   {nome}: mantido ({len(restantes)} instância(s) de outras OUs/regiões)")
    if args.apagar_artefatos or args.somente_artefatos:
        for regiao in regioes:
            bucket = f"{prefixo}-{regiao}"
            s3 = session.client("s3", region_name=regiao)
            if _bucket_existe(s3, bucket):
                _apagar_bucket(s3, bucket)
                print(f"   bucket {bucket} apagado")
    print("OK")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def comum(sp, dist_obrigatorio=True):
        sp.add_argument("--profile", required=True, help="perfil AWS da conta payer")
        sp.add_argument("--call-as", default="SELF", choices=["SELF", "DELEGATED_ADMIN"])
        if dist_obrigatorio is not None:
            sp.add_argument("--dist", required=dist_obrigatorio, help="dist/stacksets/<versão> (saída de empacotar.py)")
        sp.add_argument("--sim", action="store_true", help="não pede confirmação")
        sp.add_argument("--somente-mostrar", action="store_true", help="mostra o plano e sai")
        sp.add_argument("--intervalo", type=int, default=15, help=argparse.SUPPRESS)

    sp = sub.add_parser("publicar", help="buckets de artefatos por região + upload")
    comum(sp)
    sp.add_argument("--regioes", required=True)
    sp.add_argument("--prefixo", default=None, help="default: prm-tagging-<conta payer>")
    sp.add_argument("--org-id", default=None, help="default: lido do Organizations")
    sp.set_defaults(func=cmd_publicar)

    sp = sub.add_parser("implantar", help="StackSets hub + regional numa OU")
    comum(sp)
    sp.add_argument("--ou", required=True, help="ID(s) de OU separados por vírgula (ou r-xxxx para a raiz)")
    sp.add_argument("--valor", default="pc:825bh0xhwumf14tg27cthnoss",
                    help="pc:<product code> (default: o da Darede, pc:825bh0xhwumf14tg27cthnoss)")
    sp.add_argument("--hub-regiao", required=True)
    sp.add_argument("--regioes", default=None, help="default: as publicadas")
    sp.add_argument("--prefixo", default=None)
    sp.add_argument("--email", default=None, help="e-mail por conta (evite em OU grande: 1 confirmação por conta)")
    sp.add_argument("--topico-central-arn", default=None)
    sp.add_argument("--kms-central-arn", default=None)
    sp.add_argument("--modo-inicial", default="dry-run", choices=["dry-run", "live", "desligado"])
    sp.add_argument("--modo-varredura", default="dry-run", choices=["dry-run", "live", "desligado"])
    sp.add_argument("--contrato-confirmado", action="store_true")
    sp.add_argument("--ativar-acesso-confiavel", action="store_true")
    sp.add_argument("--max-concorrencia-pct", type=int, default=25)
    sp.add_argument("--tolerancia-falha-pct", type=int, default=100,
                    help="100 = uma região/conta com falha (ex.: SCP) não para as demais")
    sp.set_defaults(func=cmd_implantar)

    sp = sub.add_parser("status", help="resumo das instâncias (somente leitura)")
    comum(sp, dist_obrigatorio=None)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("remover", help="remove instâncias de uma OU (e artefatos, opcional)")
    comum(sp, dist_obrigatorio=False)
    sp.add_argument("--ou", default=None)
    sp.add_argument("--regioes", default=None)
    sp.add_argument("--hub-regiao", default=None)
    sp.add_argument("--prefixo", default=None)
    sp.add_argument("--apagar-artefatos", action="store_true")
    sp.add_argument("--somente-artefatos", action="store_true")
    sp.set_defaults(func=cmd_remover)
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except ClientError as exc:
        print(f"ERRO AWS: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
