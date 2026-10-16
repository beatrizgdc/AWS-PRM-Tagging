#!/usr/bin/env python3
"""Empacota a automação para rollout via CloudFormation StackSets.

O que faz (tudo LOCAL, nenhuma chamada à AWS):

1. `sam build` de cada template (hub e regional) num diretório próprio.
2. Zipa cada função e a layer do SDK de forma determinística (mesmo
   conteúdo -> mesmo zip -> mesmo sha256) e nomeia o zip pelo hash.
3. Expande o SAM para CloudFormation puro com o `samtranslator` (o mesmo
   motor que a AWS usa no transform). Assim o StackSet não depende de
   transform/macro nem de `CAPABILITY_AUTO_EXPAND`.
4. Troca o bucket do código por `${ArtefatosBucketPrefixo}-${AWS::Region}`:
   a Lambda só lê código de um bucket NA MESMA REGIÃO, então existe um
   bucket por região (criado por `stackset.py publicar` na conta payer).
5. Grava em `dist/stacksets/<versao>/`: os dois templates (JSON), os zips e
   um `manifest.json` (chaves, sha256, versão do boto3 da layer).

Uso (na pasta acima de aws_prm_tagging/):
    python3 deploy/stacksets/empacotar.py --versao 2.0.0
    python3 deploy/stacksets/empacotar.py --versao 2.0.0 --sem-build   # reaproveita dist/build-*
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404 — só para chamar o SAM CLI com lista fixa de argumentos
import sys
import zipfile
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parents[2]
TEMPLATES = {
    "prm-hub": RAIZ / "aws_prm_tagging/infra/template-hub.yaml",
    "prm-regional": RAIZ / "aws_prm_tagging/infra/template-regional.yaml",
}
MARCADOR_BUCKET = "__PRM_ARTEFATOS_BUCKET__"
PREFIXO_CHAVE = "prm-tagging"
_ZIP_DATA = (1980, 1, 1, 0, 0, 0)


class _CfnLoader(yaml.SafeLoader):
    """Lê YAML de CloudFormation (tags curtas !Ref, !Sub, !GetAtt...)."""


def _construtor_tag(loader, sufixo, node):
    nome = "Ref" if sufixo == "Ref" else f"Fn::{sufixo}"
    if isinstance(node, yaml.ScalarNode):
        valor = loader.construct_scalar(node)
        if sufixo == "GetAtt" and isinstance(valor, str):
            valor = valor.split(".", 1)
    elif isinstance(node, yaml.SequenceNode):
        valor = loader.construct_sequence(node, deep=True)
    else:
        valor = loader.construct_mapping(node, deep=True)
    return {nome: valor}


_CfnLoader.add_multi_constructor("!", _construtor_tag)


def carregar_template(caminho: Path) -> dict:
    # _CfnLoader herda de SafeLoader (só acrescenta as tags !Ref/!Sub...).
    return yaml.load(caminho.read_text(encoding="utf-8"), Loader=_CfnLoader)  # nosec B506


def zip_deterministico(origem: Path, destino: Path) -> str:
    """Zip com ordem e datas fixas; devolve o sha256 do arquivo."""
    arquivos = sorted(p for p in origem.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    with zipfile.ZipFile(destino, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in arquivos:
            info = zipfile.ZipInfo(p.relative_to(origem).as_posix(), date_time=_ZIP_DATA)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, p.read_bytes())
    return hashlib.sha256(destino.read_bytes()).hexdigest()


def _sam_build(template: Path, build_dir: Path) -> None:
    cmd = ["sam", "build", "--template-file", str(template), "--build-dir", str(build_dir)]
    print("$", " ".join(cmd), flush=True)
    env = {**os.environ, "SAM_CLI_TELEMETRY": "0"}
    subprocess.run(cmd, cwd=RAIZ, check=True, env=env)  # nosec B603


class _SemPoliticasGerenciadas:
    """Os templates só usam ARNs completos de políticas gerenciadas; nada a
    resolver por nome (o samtranslator só consulta isto se precisar)."""

    def load(self):
        return {}


def expandir(template_sam: dict) -> dict:
    from samtranslator.translator.transform import transform

    # O samtranslator só usa a região para descobrir a partição ("aws");
    # nada de região fica gravado no template (conferido em teste).
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    return transform(template_sam, {}, _SemPoliticasGerenciadas())


def _trocar_marcador(obj):
    if isinstance(obj, dict):
        return {k: _trocar_marcador(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_trocar_marcador(v) for v in obj]
    if obj == MARCADOR_BUCKET:
        return {"Fn::Sub": "${ArtefatosBucketPrefixo}-${AWS::Region}"}
    return obj


def preparar(nome: str, build_dir: Path, versao: str, saida: Path) -> tuple[dict, list[dict]]:
    """Template SAM já construído -> CloudFormation puro + lista de artefatos."""
    construido = build_dir / "template.yaml"
    tpl = carregar_template(construido)
    artefatos = []
    asl_textos: dict[str, str] = {}
    for logical, res in tpl["Resources"].items():
        props = res.get("Properties") or {}
        campo = {"AWS::Serverless::Function": "CodeUri", "AWS::Serverless::LayerVersion": "ContentUri"}.get(res["Type"])
        if campo:
            origem = build_dir / props[campo]
            tmp = saida / f".{logical}.zip"
            sha = zip_deterministico(origem, tmp)
            # Chave pelo conteúdo: as funções têm o mesmo código, então hub e
            # regional compartilham UM zip de código e UM da layer.
            tipo = "sdk-layer" if campo == "ContentUri" else "codigo"
            chave = f"{PREFIXO_CHAVE}/{versao}/{tipo}-{sha[:16]}.zip"
            final = saida / "artefatos" / chave
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(tmp, final)
            props[campo] = {"Bucket": MARCADOR_BUCKET, "Key": chave}
            artefatos.append({"logical_id": logical, "chave": chave, "sha256": sha, "bytes": final.stat().st_size})
        if res["Type"] == "AWS::Serverless::StateMachine" and "DefinitionUri" in props:
            # O ASL tem substituição NUMÉRICA (ex.: "Seconds": ${DebounceSeconds}),
            # que não é JSON válido: vai como texto em DefinitionString (o
            # CloudFormation aplica DefinitionSubstitutions no texto).
            asl = (construido.parent / props.pop("DefinitionUri")).resolve()
            asl_textos[logical] = asl.read_text(encoding="utf-8")
            props["Definition"] = {"Comment": f"__PRM_ASL__{logical}"}
    cfn = expandir(tpl)
    # O samtranslator devolve a definição como `Definition` (dict) ou, com
    # DefinitionSubstitutions, como `DefinitionString` (Fn::Join). Nos dois
    # casos o marcador identifica a state machine; troca pelo ASL original.
    for res in cfn["Resources"].values():
        if res["Type"] != "AWS::StepFunctions::StateMachine":
            continue
        props = res.get("Properties") or {}
        texto = json.dumps({k: props.get(k) for k in ("Definition", "DefinitionString")})
        m = re.search(r"__PRM_ASL__([A-Za-z0-9]+)", texto)
        if not m:
            continue
        props.pop("Definition", None)
        props["DefinitionString"] = asl_textos[m.group(1)]
    cfn = _trocar_marcador(cfn)
    cfn.setdefault("Parameters", {})["ArtefatosBucketPrefixo"] = {
        "Type": "String",
        "AllowedPattern": "^[a-z0-9][a-z0-9.-]{1,40}[a-z0-9]$",
        "Description": (
            "Prefixo dos buckets de artefatos na conta payer: o código é lido de "
            "<prefixo>-<região> (publicar_artefatos.sh)."
        ),
    }
    cfn["Description"] = f"{cfn.get('Description', '').strip()} [StackSet {nome} {versao}]"[:1024]
    return cfn, artefatos


def _versao_boto3() -> str | None:
    texto = (RAIZ / "aws_prm_tagging/requirements-lambda.txt").read_text(encoding="utf-8")
    m = re.search(r"^boto3==(\S+)", texto, flags=re.M)
    return m.group(1) if m else None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--versao", required=True, help="ex.: 2.0.0 (vira parte da chave no S3)")
    p.add_argument("--saida", default=str(RAIZ / "dist/stacksets"))
    p.add_argument("--sem-build", action="store_true", help="reaproveita dist/build-<nome> de um build anterior")
    a = p.parse_args(argv)
    if not re.fullmatch(r"[0-9A-Za-z._-]{1,40}", a.versao):
        print("--versao inválida (use letras, números, ponto, hífen)", file=sys.stderr)
        return 1

    saida = Path(a.saida) / a.versao
    if saida.exists():
        shutil.rmtree(saida)
    saida.mkdir(parents=True)
    manifest = {"versao": a.versao, "boto3_layer": _versao_boto3(), "templates": {}, "artefatos": []}
    for nome, template in TEMPLATES.items():
        build_dir = RAIZ / "dist" / f"build-{nome}"
        if not a.sem_build:
            if build_dir.exists():
                shutil.rmtree(build_dir)
            _sam_build(template, build_dir)
        cfn, artefatos = preparar(nome, build_dir, a.versao, saida)
        corpo = json.dumps(cfn, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        arquivo = saida / f"{nome}.template.json"
        arquivo.write_text(corpo, encoding="utf-8")
        chave_tpl = f"{PREFIXO_CHAVE}/{a.versao}/{nome}.template.json"
        destino_tpl = saida / "artefatos" / chave_tpl
        destino_tpl.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(arquivo, destino_tpl)
        manifest["templates"][nome] = {
            "arquivo": arquivo.name,
            "chave": chave_tpl,
            "bytes": len(corpo.encode()),
            "sha256": hashlib.sha256(corpo.encode()).hexdigest(),
        }
        manifest["artefatos"] += artefatos
        print(f"  {nome}: {len(artefatos)} artefato(s), template {len(corpo.encode())} bytes")
    (saida / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"OK: {saida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
