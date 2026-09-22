"""Lógica de decisão de tagueamento (Etapa 2a) a partir do relatório da Etapa 1.

Módulo puro: nenhuma chamada de rede, sem boto3, sem leitura de variáveis de
ambiente para lógica de negócio. Recebe o relatório (já carregado como dict)
produzido por `report.build_report` e devolve outro dict no mesmo estilo —
sem tocar em disco. Ler o JSON da Etapa 1 e gravar o relatório de decisão em
disco é responsabilidade do chamador (o mesmo padrão que `report.py` já usa:
`main.py` é quem faz I/O de arquivo, não os módulos de lógica).

Cada recurso do relatório da Etapa 1 é classificado em exatamente uma
categoria:

- `taguear`   — tag ausente e recurso não gerenciado por IaC. Candidato a
  tagueamento via API nas Etapas 2b/2c.
- `pular_iac` — recurso gerenciado por IaC (heurística da Etapa 1),
  independente de ter ou não a tag hoje. Nunca tagueado via API/CLI/Console
  — só identificado e sinalizado para tagueamento pelo próprio IaC.
- `ja_ok`     — tag já presente com o valor esperado (comparação exata,
  case-sensitive, herdada de `tag_status.get_tag_status`). Nada a fazer.
- `conflito`  — tag presente com um valor diferente do esperado. Nunca
  sobrescrita automaticamente, gerenciado por IaC ou não — um conflito é
  sempre reportado para revisão humana, nunca resolvido nesta etapa.

Precedência (aplicada nesta ordem):

1. Tag ausente:
   a. IaC detectado (`cloudformation` ou `terraform_heuristico`) -> `pular_iac`.
   b. Caso contrário (incluindo `iac.tipo == "desconhecido"`) -> `taguear`.
2. Tag presente:
   a. Valor bate com o esperado -> `ja_ok`.
   b. Valor diferente -> `conflito` (aqui o IaC é só metadado informativo no
      relatório — nunca muda a decisão).

**Importante:** "tag ausente"/"valor bate com o esperado" acima NÃO são lidos
do `status_tag` já calculado pela Etapa 1 — são recalculados aqui via
`tag_status.get_tag_status`, a partir de `valor_tag_encontrado` (o valor bruto
que a Etapa 1 já extraiu da tag, presente no relatório independentemente do
resultado da comparação) contra o `expected_tag_value` recebido por ESTA
chamada. Isso é proposital: `expected_tag_value` existe como parâmetro desta
etapa justamente porque sub-OUs diferentes de uma mesma conta podem ter
contratos/product codes diferentes — uma única execução da Etapa 1 (que roda
por conta, não por sub-OU) pode alimentar várias execuções da Etapa 2a, cada
uma com o valor esperado da sub-OU correspondente. Confiar no `status_tag`
já congelado pela Etapa 1 quebraria esse caso, pois ele reflete o valor
esperado que a Etapa 1 recebeu na hora da descoberta, não o desta chamada.
`status_tag` ainda é validado (ver `_validar_recurso`) como checagem
estrutural de que o registro tem a forma esperada, mas seu valor não é usado
para decidir.

Suposição assumida sobre o schema da Etapa 1, vale confirmar: `iac.tipo ==
"desconhecido"` é tratado como "IaC não detectado" para fins da regra 1, não
como uma terceira categoria própria — `iac_detection.py` documenta que
"desconhecido" nunca deve ser lido como "não é IaC" na descoberta, mas na
Etapa 2a a ausência de qualquer sinal precisa resultar em alguma decisão, e
a mais segura é seguir o fluxo normal de tagueamento (em vez de travar o
recurso em uma categoria de revisão manual só por falta de sinal).

Escopo: resource_discovery.py (Etapa 1) já filtra o que é elegível via
`tipo_recurso` (cluster/node_group/node/ebs_volume/load_balancer para EKS;
application_inference_profile para Bedrock) — EKS Fargate e Bedrock fora de
application inference profile nunca são descobertos, então normalmente não
aparecem na entrada desta etapa. `_em_escopo` abaixo é uma checagem
defensiva sobre isso (para relatórios sintéticos/malformados ou uma mudança
futura na Etapa 1): qualquer recurso de "Amazon EKS"/"Amazon Bedrock" com um
`tipo_recurso` fora da lista esperada é excluído do relatório de decisão —
não aparece em `recursos` nem em `erros`, só é contado em
`resumo.total_excluidos_fora_de_escopo`.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .iac_detection import IAC_CLOUDFORMATION, IAC_DESCONHECIDO, IAC_TERRAFORM_HEURISTICO
from .tag_status import TAG_KEY, STATUS_CONFLITO, STATUS_OK, STATUS_SEM_TAG, get_tag_status

DECISAO_TAGUEAR = "taguear"
DECISAO_PULAR_IAC = "pular_iac"
DECISAO_JA_OK = "ja_ok"
DECISAO_CONFLITO = "conflito"

_TODAS_AS_DECISOES = (DECISAO_TAGUEAR, DECISAO_PULAR_IAC, DECISAO_JA_OK, DECISAO_CONFLITO)
_TODOS_OS_STATUS_IAC = (IAC_CLOUDFORMATION, IAC_TERRAFORM_HEURISTICO, IAC_DESCONHECIDO)
_IAC_DETECTADO = (IAC_CLOUDFORMATION, IAC_TERRAFORM_HEURISTICO)

# Nomes de serviço exatamente como aparecem no CSV oficial (mesma string já
# hardcoded em `resource_discovery.py` para o nome de exibição de EKS/Bedrock
# — não é dado de cliente/conta/contrato, é nomenclatura fixa da AWS).
_SERVICO_EKS = "Amazon EKS"
_SERVICO_BEDROCK = "Amazon Bedrock"

_EKS_TIPOS_EM_ESCOPO = {"cluster", "node_group", "node", "ebs_volume", "load_balancer"}
_BEDROCK_TIPOS_EM_ESCOPO = {"application_inference_profile"}


def _em_escopo(servico: str, tipo_recurso: str | None) -> bool:
    """Checagem defensiva de escopo para os dois serviços com sub-recursos.

    Para qualquer outro serviço do CSV, `tipo_recurso` não é usado para
    decidir escopo (o campo `servico`, já filtrado na Etapa 1 pelo CSV
    oficial, é suficiente)."""
    if servico == _SERVICO_EKS:
        return tipo_recurso in _EKS_TIPOS_EM_ESCOPO
    if servico == _SERVICO_BEDROCK:
        return tipo_recurso in _BEDROCK_TIPOS_EM_ESCOPO
    return True


def _validar_recurso(resource: Any) -> str | None:
    """Retorna uma mensagem de erro curta se `resource` estiver malformado
    para fins desta etapa, ou `None` se os campos necessários à decisão
    estiverem presentes e com um valor reconhecido."""
    if not isinstance(resource, dict):
        return "entrada não é um objeto de recurso válido (esperado dict)"
    if not isinstance(resource.get("arn"), str) or not resource["arn"]:
        return "campo 'arn' ausente ou inválido"
    if not isinstance(resource.get("servico"), str) or not resource["servico"]:
        return "campo 'servico' ausente ou inválido"
    if not isinstance(resource.get("regiao"), str) or not resource["regiao"]:
        return "campo 'regiao' ausente ou inválido"
    status_tag = resource.get("status_tag")
    if status_tag not in (STATUS_SEM_TAG, STATUS_OK, STATUS_CONFLITO):
        return f"campo 'status_tag' ausente ou com valor inesperado: {status_tag!r}"
    iac = resource.get("iac")
    iac_tipo = iac.get("tipo") if isinstance(iac, dict) else None
    if iac_tipo not in _TODOS_OS_STATUS_IAC:
        return f"campo 'iac.tipo' ausente ou com valor inesperado: {iac_tipo!r}"
    return None


def _decidir(valor_tag_atual: str | None, expected_tag_value: str, iac_tipo: str) -> tuple[str, str]:
    """Aplica a regra de precedência (ver docstring do módulo) e devolve
    `(decisao, motivo)`.

    Recalcula o status da tag via `tag_status.get_tag_status` a partir do
    valor bruto já extraído pela Etapa 1 (`valor_tag_atual`), reconstruindo
    um dict de uma chave só — em vez de confiar no `status_tag` já congelado
    do relatório da Etapa 1 (ver docstring do módulo para o porquê)."""
    tags_reconstruidas = {} if valor_tag_atual is None else {TAG_KEY: valor_tag_atual}
    status_tag, _ = get_tag_status(tags_reconstruidas, expected_tag_value)

    if status_tag == STATUS_SEM_TAG:
        if iac_tipo in _IAC_DETECTADO:
            return (
                DECISAO_PULAR_IAC,
                f"tag aws-apn-id ausente; recurso gerenciado por IaC ({iac_tipo}) "
                "— taguear via IaC, não via API/CLI/Console",
            )
        return (
            DECISAO_TAGUEAR,
            "tag aws-apn-id ausente; recurso não gerenciado por IaC "
            "— elegível para tagueamento via API",
        )
    if status_tag == STATUS_OK:
        return DECISAO_JA_OK, "tag aws-apn-id já presente com o valor esperado"
    return (
        DECISAO_CONFLITO,
        "tag aws-apn-id presente com valor diferente do esperado "
        "— nunca sobrescrever automaticamente",
    )


def classify_resource(resource: Any, expected_tag_value: str) -> dict | None:
    """Classifica um único recurso do relatório da Etapa 1.

    Devolve:
    - `None` se o recurso estiver fora do escopo desta automação (ver
      `_em_escopo`) — o chamador deve simplesmente omiti-lo do relatório.
    - `{"arn": ..., "servico": ..., "erro": "<mensagem curta>"}` se o
      recurso estiver malformado/incompleto para fins de decisão.
    - Um dict de decisão completo (`arn`, `servico`, `regiao`,
      `tipo_recurso`, `valor_tag_atual`, `decisao`, `motivo`,
      `tag_similar_encontrada`, `tag_similar_chaves`, `iac`) caso contrário.

    `expected_tag_value` nunca é lido de configuração global nem de estado
    entre chamadas — cada chamada é independente, permitindo aplicar valores
    esperados diferentes (ex.: sub-OUs com contratos diferentes) ao mesmo
    lote de recursos sem nenhum vazamento de estado entre execuções.
    """
    erro = _validar_recurso(resource)
    if erro is not None:
        arn = resource.get("arn") if isinstance(resource, dict) else None
        servico = resource.get("servico") if isinstance(resource, dict) else None
        return {"arn": arn, "servico": servico, "erro": erro}

    servico = resource["servico"]
    tipo_recurso = resource.get("tipo_recurso")
    if not _em_escopo(servico, tipo_recurso):
        return None

    iac = resource["iac"]
    valor_tag_atual = resource.get("valor_tag_encontrado")
    decisao, motivo = _decidir(valor_tag_atual, expected_tag_value, iac["tipo"])

    return {
        "arn": resource["arn"],
        "servico": servico,
        "regiao": resource["regiao"],
        "tipo_recurso": tipo_recurso,
        "valor_tag_atual": valor_tag_atual,
        "decisao": decisao,
        "motivo": motivo,
        "tag_similar_encontrada": bool(resource.get("tag_similar_encontrada", False)),
        "tag_similar_chaves": list(resource.get("tag_similar_chaves", [])),
        "iac": {
            "tipo": iac["tipo"],
            "detectado": iac["tipo"] in _IAC_DETECTADO,
            "stack_name": iac.get("stack_name"),
        },
    }


def build_decision_report(etapa1_report: dict, expected_tag_value: str) -> dict:
    """Constrói o relatório de decisão (Etapa 2a) a partir do relatório já
    carregado da Etapa 1 (`report.build_report`). Função pura — mesmo input
    sempre produz o mesmo output, sem I/O.

    Este é o contrato de entrada da Etapa 2b (execução em dry-run): o
    formato de `recursos` aqui deve ser suficiente para uma etapa seguinte
    decidir o que chamar na API, sem precisar voltar ao relatório da Etapa 1.
    """
    resources = etapa1_report.get("recursos") or []

    decisoes: list[dict] = []
    erros: list[dict] = []
    total_excluidos = 0

    for resource in resources:
        resultado = classify_resource(resource, expected_tag_value)
        if resultado is None:
            total_excluidos += 1
        elif "erro" in resultado:
            erros.append(resultado)
        else:
            decisoes.append(resultado)

    decisao_counts = Counter(d["decisao"] for d in decisoes)
    servico_counts = Counter(d["servico"] for d in decisoes)
    iac_counts = Counter(d["iac"]["tipo"] for d in decisoes)

    return {
        "conta_id": etapa1_report.get("conta_id"),
        "executado_em": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "valor_tag_esperado": expected_tag_value,
        "resumo": {
            "total_recursos_avaliados": len(decisoes),
            "total_erros": len(erros),
            "total_excluidos_fora_de_escopo": total_excluidos,
            "por_decisao": {d: decisao_counts.get(d, 0) for d in _TODAS_AS_DECISOES},
            "por_servico": dict(sorted(servico_counts.items())),
            "por_status_iac": {t: iac_counts.get(t, 0) for t in _TODOS_OS_STATUS_IAC},
        },
        "recursos": decisoes,
        "erros": erros,
    }
