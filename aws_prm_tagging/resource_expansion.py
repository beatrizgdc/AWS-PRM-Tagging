"""Expansão de recursos derivados na Etapa 3 — hoje: instância EC2 ->
volumes EBS anexados.

## Por que existe

Todo `RunInstances`/`CreateFleet` cria pelo menos um volume EBS (o root)
junto com a instância, sem nunca gerar um evento `CreateVolume` próprio.
Esse volume é cobrado sob Amazon EC2 ("Includes Amazon EBS" no CSV) e, sem
esta expansão, só seria pego pela Etapa 1/4 (varredura), nunca pela reação
em tempo real da Etapa 3.

Tentar ler os IDs dos volumes do próprio evento não funciona de forma
confiável: no momento da resposta de `RunInstances` a instância ainda está
em `pending` e o `blockDeviceMapping` normalmente vem vazio. Por isso a
expansão é feita com uma leitura (`ec2:DescribeInstances`) no momento do
processamento — que acontece DEPOIS do debounce do Step Functions (default
30 min), quando os volumes já estão anexados.

Somente leitura, sem custo. Qualquer falha aqui NUNCA impede o
processamento da própria instância — só deixa de acrescentar os volumes, e
é devolvida ao chamador para ser publicada (ver
`handler_continuous_tagging.py`).
"""
from __future__ import annotations

import logging

import boto3
from botocore.exceptions import ClientError

from .event_parser import ExtractedResource
from .retry import with_backoff

logger = logging.getLogger(__name__)

_TAMANHO_LOTE_DESCRIBE_INSTANCES = 100  # conservador — InstanceIds aceita bem mais

# Código devolvido quando a instância já não existe (ex.: Spot interrompida
# ou node do Karpenter consolidado durante o debounce). Não é falha: não há
# volume para taguear.
_CODIGOS_INSTANCIA_INEXISTENTE = frozenset({"InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"})


@with_backoff()
def _describe_instances(client, instance_ids: list[str]) -> dict:
    return client.describe_instances(InstanceIds=instance_ids)


def _instance_id_from_arn(arn: str) -> str | None:
    # arn:aws:ec2:<regiao>:<conta>:instance/<id>
    partes = arn.split(":", 5)
    if len(partes) < 6 or partes[2] != "ec2" or not partes[5].startswith("instance/"):
        return None
    return partes[5].split("/", 1)[1] or None


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def expand_instance_volumes(
    session: boto3.Session, extracted: list[ExtractedResource]
) -> tuple[list[ExtractedResource], list[dict]]:
    """Devolve `(volumes_adicionais, falhas)`.

    - `volumes_adicionais`: um `ExtractedResource` por volume EBS anexado a
      alguma instância de `extracted`, sem repetir ARNs que já estejam em
      `extracted` (ex.: o mesmo volume vindo de um `CreateVolume`).
    - `falhas`: `{"arn", "regiao", "codigo", "mensagem"}` por lote de
      instâncias cuja leitura falhou (permissão, throttling esgotado...).
      Instância inexistente NÃO é falha.
    """
    ja_presentes = {r.arn for r in extracted}
    # regiao -> {instance_id: ExtractedResource da instância}
    por_regiao: dict[str, dict[str, ExtractedResource]] = {}
    for r in extracted:
        instance_id = _instance_id_from_arn(r.arn)
        if instance_id:
            por_regiao.setdefault(r.regiao, {})[instance_id] = r

    adicionais: list[ExtractedResource] = []
    falhas: list[dict] = []
    for regiao, instancias in por_regiao.items():
        client = session.client("ec2", region_name=regiao)
        for lote in _chunk(sorted(instancias), _TAMANHO_LOTE_DESCRIBE_INSTANCES):
            try:
                resp = _describe_instances(client, lote)
            except ClientError as exc:
                erro = exc.response.get("Error", {})
                codigo = erro.get("Code", "")
                if codigo in _CODIGOS_INSTANCIA_INEXISTENTE and len(lote) == 1:
                    logger.info("Instância %s não existe mais — sem volumes para expandir", lote[0])
                    continue
                if codigo in _CODIGOS_INSTANCIA_INEXISTENTE:
                    # Um ID inexistente derruba o lote inteiro — repete 1 a 1
                    # para não perder os volumes das instâncias que existem.
                    sub_adicionais, sub_falhas = _expandir_um_a_um(client, regiao, lote, instancias)
                    adicionais.extend(a for a in sub_adicionais if a.arn not in ja_presentes)
                    ja_presentes.update(a.arn for a in sub_adicionais)
                    falhas.extend(sub_falhas)
                    continue
                logger.warning("Falha ao expandir volumes das instâncias %s em %s: %s", lote, regiao, codigo)
                for instance_id in lote:
                    falhas.append(
                        {
                            "arn": instancias[instance_id].arn,
                            "regiao": regiao,
                            "codigo": codigo,
                            "mensagem": erro.get("Message", str(exc)),
                        }
                    )
                continue
            for volume in _volumes_da_resposta(resp, regiao, instancias):
                if volume.arn not in ja_presentes:
                    adicionais.append(volume)
                    ja_presentes.add(volume.arn)
    return adicionais, falhas


def _expandir_um_a_um(
    client, regiao: str, lote: list[str], instancias: dict[str, ExtractedResource]
) -> tuple[list[ExtractedResource], list[dict]]:
    adicionais: list[ExtractedResource] = []
    falhas: list[dict] = []
    for instance_id in lote:
        try:
            resp = _describe_instances(client, [instance_id])
        except ClientError as exc:
            erro = exc.response.get("Error", {})
            if erro.get("Code", "") in _CODIGOS_INSTANCIA_INEXISTENTE:
                continue
            falhas.append(
                {
                    "arn": instancias[instance_id].arn,
                    "regiao": regiao,
                    "codigo": erro.get("Code", ""),
                    "mensagem": erro.get("Message", str(exc)),
                }
            )
            continue
        adicionais.extend(_volumes_da_resposta(resp, regiao, instancias))
    return adicionais, falhas


def _volumes_da_resposta(
    resp: dict, regiao: str, instancias: dict[str, ExtractedResource]
) -> list[ExtractedResource]:
    volumes: list[ExtractedResource] = []
    for reservation in resp.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            origem = instancias.get(instance.get("InstanceId"))
            if origem is None:
                continue
            conta = origem.arn.split(":")[4]
            for mapping in instance.get("BlockDeviceMappings", []):
                volume_id = (mapping.get("Ebs") or {}).get("VolumeId")
                if volume_id:
                    volumes.append(
                        ExtractedResource(
                            arn=f"arn:aws:ec2:{regiao}:{conta}:volume/{volume_id}",
                            servico=origem.servico,
                            regiao=regiao,
                            tipo_recurso=None,
                        )
                    )
    return volumes
