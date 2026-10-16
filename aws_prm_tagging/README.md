# aws_prm_tagging — referência técnica

Pacote Python da automação. O mesmo código roda como CLI (na máquina de quem
opera) e dentro das Lambdas das stacks. Visão geral e operação:
[docs/MVP-2.md](docs/MVP-2.md).

## Onde rodar

Sempre a partir da pasta que **contém** `aws_prm_tagging/` (a raiz do
repositório), para o pacote ser importável:

```bash
pip install -r aws_prm_tagging/requirements-dev.txt
python3 -m aws_prm_tagging.main --help
python3 -m pytest aws_prm_tagging/test/unit/
```

Credenciais: qualquer perfil do AWS CLI (`--profile`). Para `aws login`,
o `boto3[crt]` dos requirements é necessário. A política IAM mínima sai de
`python3 -m aws_prm_tagging.main politica-iam` (leitura) ou
`politica-iam --com-escrita` (inclui `apply --live`).

## CLI

O valor da tag tem padrão `pc:825bh0xhwumf14tg27cthnoss` (product code da
Darede, em `tag_status.VALOR_PADRAO`); `--expected-tag-value` troca.

| Subcomando | Escreve? | O que faz |
| --- | --- | --- |
| `iniciar` | Só após confirmação | Modo interativo: mapear, simular, taguear e implantar |
| `preflight` | Não | Regiões para implantar e trail do CloudTrail por região |
| `map` | Não | Etapa 1: inventário com status da tag e detecção de IaC |
| `decide` | Não (sem API) | Etapa 2a: decisão por recurso |
| `apply` | Só com `--live` | Etapa 2b (dry-run) ou 2c (escrita), relendo cada recurso antes; gera o relatório do cliente |
| `relatorio-cliente` | Não | Refaz o relatório do cliente a partir da saída do `apply` |
| `recomendar` | Não | Tag na origem (EKS, ECS, DRS, IoT) |
| `auditar-org` | Não | SCPs e tag policies (credencial da payer) |
| `verificar-csv` | Não | Compara o CSV embutido com a página oficial |
| `politica-iam` | Não | Imprime a política IAM mínima |

## Módulos

| Módulo | Papel |
| --- | --- |
| `main.py`, `assistente.py` | CLI e modo interativo |
| `pipeline.py` | Etapas 1 → 2a → 2b/2c por região (CLI e Lambda do hub) |
| `regions.py`, `ou_tree.py`, `preflight.py` | Regiões, árvore de OUs, pré-checagem |
| `services.py`, `data/` | CSV oficial e classificação de ARN |
| `resource_discovery.py`, `native_discovery.py` | Descoberta pela Tagging API e pelas APIs de cada serviço |
| `iac_detection.py`, `cloudtrail_iac.py` | Detecção de IaC por tags, `userAgent` e histórico do CloudTrail |
| `tag_status.py`, `decision.py` | Status da tag e decisão (Etapa 2a) |
| `tag_execution.py`, `tag_reads.py`, `single_resource.py` | Revalidação e escrita (Etapas 2b/2c) |
| `report.py`, `relatorio_cliente.py`, `publish.py` | Relatórios e publicação no SNS |
| `event_mapping.py`, `event_parser.py`, `resource_expansion.py` | Eventos da Etapa 3 e extração de ARNs |
| `handler_*.py` | Lambdas: Etapa 3, drift, tagueamento inicial/varredura, Zabbix |
| `recomendacoes.py`, `org_audit.py`, `csv_check.py` | Recomendações, auditoria da organização, CSV |
| `zabbix.py` | Envio ao Zabbix (API ou trapper) |
| `politica_iam.py` | Política IAM derivada do template do hub |
| `escopo_teste.py`, `retry.py` | Filtro para conta compartilhada; backoff |

## Pastas

| Pasta | Conteúdo |
| --- | --- |
| `infra/` | Templates SAM/CloudFormation, state machines, event patterns gerados, template do Zabbix ([infra/README.md](infra/README.md)) |
| `docs/` | `MVP-2.md` (atual), `MVP-1.md` (base histórica), `pendencias.md`, `referencia/` (guia oficial do PRM) |
| `test/unit/` | Testes offline (moto e fakes) |
| `test/sandbox/` | Roteiro e scripts de validação numa conta de teste |

## Mudou a lista de serviços ou um evento?

1. Atualize `data/resource-tagging-included-services.csv` (ver `data/README.md`).
2. Ajuste `event_mapping.py` / `event_parser.py`.
3. `python3 -m aws_prm_tagging.event_mapping` regrava `infra/event_pattern.*.generated.json`
   e sincroniza o template regional; se o número de arquivos mudar, ajuste as
   regras `Etapa3EventRule<N>` em `infra/template-regional.yaml`.
4. Rode a suíte.
