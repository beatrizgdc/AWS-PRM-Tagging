"""Wrappers de baixo nível (1 chamada de API nativa por tipo de recurso
dedicado — EKS, Bedrock, ELBv2) compartilhados entre `single_resource.py`
(Etapa 3, lê 1 ARN por vez) e `tag_execution.py` (Etapas 2b/2c, revalida em
lote). Cada função aqui só encapsula a chamada de API decorada com
retry/backoff, devolvendo a resposta crua do boto3 — nenhuma lógica de
negócio (formato de saída, agrupamento em lote, tratamento de erro por
ARN) mora aqui, essa parte continua específica de cada chamador, já que os
dois têm necessidades bem diferentes (leitura pontual vs. revalidação em
massa com paginação/lote).

O caminho genérico (`resourcegroupstaggingapi:GetResources`) NÃO está
aqui: `single_resource.py` consulta 1 ARN específico
(`ResourceARNList=[arn]`) enquanto `tag_execution.py` pagina a região
inteira sem filtro (para reconfirmar IaC de todos os recursos de uma vez)
— usos genuinamente diferentes da mesma API, não uma duplicação."""
from __future__ import annotations

from .retry import with_backoff


@with_backoff()
def eks_list_tags(client, resource_arn: str) -> dict:
    return client.list_tags_for_resource(resourceArn=resource_arn)


@with_backoff()
def bedrock_list_tags(client, resource_arn: str) -> dict:
    return client.list_tags_for_resource(resourceARN=resource_arn)


@with_backoff()
def elb_describe_tags(client, arns: list[str]) -> dict:
    return client.describe_tags(ResourceArns=arns)
