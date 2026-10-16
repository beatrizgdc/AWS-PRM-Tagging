"""Confere o resultado de validar_etapa2c.sh — somente leitura.

Para cada recurso do manifest:
  1. a Etapa 1 o encontrou? (recursos criados sem tag só aparecem pela
     descoberta nativa — é o teste do item 1)
  2. a categoria final da Etapa 2c é a esperada?
  3. a tag REAL na AWS está como deveria? (lida de volta com GetResources:
     tagueado/ja_ok -> aws-apn-id = valor esperado; conflito -> valor do
     outro parceiro preservado; revisar/pulado -> sem aws-apn-id)
Sai com código 1 se qualquer verificação falhar.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import boto3

TAG = "aws-apn-id"


def _tags_atuais(session, regiao: str, arns: list[str]) -> dict[str, dict]:
    tags: dict[str, dict] = {a: {} for a in arns}
    por_regiao: dict[str, list[str]] = {}
    for a in arns:
        # S3 não tem região no ARN; o bucket foi criado na região do teste
        por_regiao.setdefault(a.split(":")[3] or regiao, []).append(a)
    for r, lista in por_regiao.items():
        client = session.client("resourcegroupstaggingapi", region_name=r)
        for i in range(0, len(lista), 20):
            for m in client.get_resources(ResourceARNList=lista[i : i + 20])["ResourceTagMappingList"]:
                tags[m["ResourceARN"]] = {t["Key"]: t["Value"] for t in m.get("Tags", [])}
    return tags


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--profile")
    p.add_argument("--regiao", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--mapa", required=True)
    p.add_argument("--resultado", required=True)
    p.add_argument("--valor", required=True)
    a = p.parse_args()

    esperados: dict[str, str] = {}
    for linha in open(a.manifest, encoding="utf-8"):
        if linha.strip() and not linha.startswith("#"):
            arn, cat, _ = linha.rstrip("\n").split("\t", 2)
            esperados[arn] = cat

    mapa = {r["arn"]: r for r in json.load(open(a.mapa, encoding="utf-8"))["recursos"]}
    resultado = {r["arn"]: r for r in json.load(open(a.resultado, encoding="utf-8"))["recursos"]}
    session = boto3.Session(profile_name=a.profile)
    tags = _tags_atuais(session, a.regiao, list(esperados))

    # Consistência eventual: o índice da Tagging API (GetResources) pode
    # levar alguns segundos para refletir uma escrita — visto na sandbox em
    # 2026-10-02 com DynamoDB (tagueado_sucesso, mas leitura 1 s depois sem
    # a tag). Antes de acusar divergência, relê por até ~60 s e, para
    # DynamoDB, confere também pela API nativa do serviço.
    def _faltando():
        return [
            arn for arn, cat in esperados.items()
            if cat in ("tagueado_sucesso", "ja_ok") and tags[arn].get(TAG) != a.valor
        ]

    for tentativa in range(6):
        pendentes = _faltando()
        if not pendentes:
            break
        print(f"(aguardando propagação da tag em {len(pendentes)} recurso(s), tentativa {tentativa + 1}/6...)")
        time.sleep(10)
        tags.update({k: v for k, v in _tags_atuais(session, a.regiao, pendentes).items() if v})
    for arn in _faltando():
        if ":dynamodb:" in arn:
            ddb = session.client("dynamodb", region_name=arn.split(":")[3])
            nativas = {t["Key"]: t["Value"] for t in ddb.list_tags_of_resource(ResourceArn=arn).get("Tags", [])}
            if nativas.get(TAG) == a.valor:
                print(f"(DynamoDB: tag presente pela API nativa, ainda não no índice da Tagging API: {arn})")
                tags[arn] = nativas

    falhas = 0
    print(f"{'recurso':70} {'etapa1':12} {'esperado':20} {'obtido':20} tag real")
    for arn, esperado in esperados.items():
        problemas = []
        no_mapa = mapa.get(arn)
        origem = (no_mapa or {}).get("origem_descoberta") or ("generico" if no_mapa else "AUSENTE")
        if not no_mapa:
            problemas.append("não descoberto pela Etapa 1")
        obtido = (resultado.get(arn) or {}).get("categoria_final", "AUSENTE")
        if obtido != esperado:
            problemas.append(f"categoria {obtido} != {esperado}")
        real = tags[arn].get(TAG)
        if esperado in ("tagueado_sucesso", "ja_ok") and real != a.valor:
            problemas.append(f"tag real = {real!r}")
        if esperado == "conflito" and real != "pc:outroparceiro":
            problemas.append(f"CONFLITO SOBRESCRITO: tag real = {real!r}")
        if esperado in ("revisar_tag_similar", "pulado_iac") and real is not None:
            problemas.append(f"não deveria ter {TAG}, tem {real!r}")
        falhas += bool(problemas)
        marca = "OK " if not problemas else "ERRO"
        print(f"{marca} {arn[-66:]:66} {origem:12} {esperado:20} {obtido:20} {real}")
        for pr in problemas:
            print(f"       -> {pr}")
        detalhe = (resultado.get(arn) or {}).get("detalhe_erro")
        if detalhe and problemas:
            print(f"       -> detalhe_erro: {detalhe}")

    print()
    print("RESULTADO:", "tudo conforme o esperado" if not falhas else f"{falhas} recurso(s) com divergência")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
