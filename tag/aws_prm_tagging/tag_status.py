"""Classificação do status da tag aws-apn-id em um recurso."""
from __future__ import annotations

from typing import Optional

TAG_KEY = "aws-apn-id"

STATUS_SEM_TAG = "sem_tag"
STATUS_OK = "ok"
STATUS_CONFLITO = "conflito"


def get_tag_status(
    tags: dict[str, str], expected_tag_value: str
) -> tuple[str, Optional[str]]:
    """Retorna (status, valor_encontrado) para a tag aws-apn-id de um recurso.

    A comparação de chave é case-sensitive por design: o guia da AWS exige
    a chave exatamente em minúsculas (`aws-apn-id`); uma variante com
    capitalização diferente (ex.: `AWS-APN-ID`) não é reconhecida pela AWS
    para atribuição de receita e portanto é tratada como "sem_tag".
    """
    found_value = tags.get(TAG_KEY)
    if found_value is None:
        return STATUS_SEM_TAG, None
    if found_value == expected_tag_value:
        return STATUS_OK, found_value
    return STATUS_CONFLITO, found_value


def tags_list_to_dict(tag_list: list[dict]) -> dict[str, str]:
    """Converte a lista [{'Key':..,'Value':..}, ...] (formato comum da maioria
    das APIs AWS) em dict. Também aceita o formato [{'key':..,'value':..}]
    usado por algumas APIs (ex.: bedrock ListTagsForResource)."""
    result: dict[str, str] = {}
    for item in tag_list:
        key = item.get("Key", item.get("key"))
        value = item.get("Value", item.get("value"))
        if key is not None:
            result[key] = value
    return result
