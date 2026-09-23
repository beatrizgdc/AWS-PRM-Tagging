# Testes unitários

Pytest, 100% offline — sem rede real, sem credencial, sem nenhuma conta AWS
real. Cobre `decision.py` (Etapa 2a, 100% sem boto3), `tag_execution.py`
(Etapas 2b e 2c — usa boto3, mas os testes passam sessões/clients falsos em
vez de rede real; ver `fake_session_factory` em `conftest.py`), `report.py`
(Etapa 1, 100% sem boto3), a validação pura de `main.py`
(`_validate_expected_tag_value`), `services.classify_arn` (também 100% sem
boto3) e as funções puras de `resource_discovery.py` extraídas para não
dependerem de boto3. Ver [test/localstack/README.md](../localstack/README.md)
para o teste e2e da Etapa 1 completa (o resto de `resource_discovery.py`,
que ainda depende de boto3), que precisa de boto3 + LocalStack de verdade.

Rodar (do diretório que contém `aws_prm_tagging/` — ver ["Onde rodar os
comandos"](../../README.md#onde-rodar-os-comandos) no README raiz):

```bash
python3 -m pip install -r aws_prm_tagging/requirements-dev.txt
python3 -m pytest aws_prm_tagging/test/unit/
```

## Convenção para novos arquivos de teste

Um arquivo por área de responsabilidade do módulo testado, não um arquivo
gigante por módulo — nomeado `test_<módulo>_<área>.py`. Para `decision.py`
(Etapa 2a):

- `test_decision_precedencia.py` — regra de precedência de
  `classify_resource` (tag ausente vs. presente, papel do IaC, tag com
  grafia parecida, `motivo` específico por ferramenta AWS quando
  `gerenciado_por_ferramenta_aws` vem preenchido — decisão continua
  `pular_iac`, só o texto muda).
- `test_decision_escopo.py` — filtragem de escopo por `tipo_recurso` (os 5
  sub-tipos de EKS, exclusão de Fargate e de Bedrock fora de application
  inference profile).
- `test_decision_relatorio.py` — `build_decision_report` no nível de lote:
  robustez a entrada malformada, resumo agregado, múltiplos
  `expected_tag_value` sem vazamento de estado.

Para `tag_execution.py` (Etapas 2b e 2c):

- `test_tag_execution_selecao.py` — garantia estrutural de `select_taggable`
  (só `decisao == "taguear"` passa) e roteamento de API por
  `(servico, tipo_recurso)`.
- `test_tag_execution_lotes.py` — agrupamento em lotes de até 20 ARNs no
  caminho genérico, sem misturar região, e confirmação de que os caminhos
  dedicados (EKS/Bedrock/ELB) nunca são agrupados.
- `test_tag_execution_execucao.py` — revalidação de 4 vias (falha de
  leitura / já tagueado / conflito / IaC detectado, cada uma sem chamar o
  executor — falha de leitura tem precedência sobre as outras três),
  `--live` recusando `revalidate=False` (`RevalidacaoObrigatoriaError`),
  idempotência entre reexecuções, dry-run nunca chamando boto3 de escrita,
  `LiveExecutor` (sucesso, falha parcial de lote, classificação de erro),
  idade máxima do relatório de decisão, e o merge do relatório final
  (incluindo `revisar_tag_similar` e os `erros` de classificação da
  Etapa 2a).

Para `report.py` (Etapa 1):

- `test_report_falhas_descoberta.py` — `falhas_descoberta`/
  `total_falhas_descoberta` distinguindo "0 recursos" de "a descoberta
  falhou aqui" no relatório.

Para `main.py`:

- `test_main_validacao.py` — `_validate_expected_tag_value` (formato
  `pc:<product-code>` fechado, `ra-...` e qualquer outro formato
  recusados).

Para `iac_detection.py`:

- `test_iac_detection.py` — precedência CloudFormation/Terraform,
  heurística de Terraform em 2 níveis, e `gerenciado_por_ferramenta_aws`
  identificado por prefixo de nome de stack (Elastic Beanstalk, Control
  Tower, Service Catalog, eksctl).

Para `services.py`:

- `test_services_classify_arn.py` — desambiguação de código de produto
  compartilhado (`vpc-lattice` classificado com nome próprio, não como
  "AWS Transit Gateway"; regressão da desambiguação ec2 compute vs. rede).

Para `resource_discovery.py` (só a parte pura, sem boto3):

- `test_resource_discovery_puro.py` — `_chunk` e
  `_filter_load_balancers_for_cluster` (filtro puro sobre o resultado já
  coletado uma vez por região, não mais relistado a cada cluster).

Para a Etapa 3 (`event_mapping.py`/`event_parser.py`/`single_resource.py`/
`publish.py`/`handler_continuous_tagging.py`):

- `test_event_mapping.py` — a tabela nunca diverge do CSV
  (`validate_against_csv`), o event pattern gerado tem a forma que o
  EventBridge espera, e é dividido em múltiplos patterns dentro da quota de
  tamanho; inclui a guarda de deriva contra os arquivos
  `infra/event_pattern.*.generated.json`.
- `test_event_parser_extracao.py` — extração pura a partir de payloads
  sintéticos de CloudTrail: casos representativos de extractors dedicados
  (lote via `RunInstances`, construção a partir de `requestParameters`,
  `tipo_recurso` de EKS/Bedrock), o fallback genérico, e os casos
  defensivos (payload insuficiente, evento fora do mapeamento).
- `test_event_parser_botocore.py` — **verificação estrutural de TODO
  extractor dedicado** (`_DirectPath`/`_ConstructedPath`) contra o shape
  real da operação de API no `botocore` instalado (não contra dado
  sintético digitado à mão) — a rede de segurança que substitui, em parte,
  não ter um evento CloudTrail real capturado (ver
  [docs/melhorias-futuras.md](../../docs/melhorias-futuras.md)).
- `test_single_resource.py` — os 4 caminhos de leitura (genérico/EKS/
  Bedrock/ELB) usando `fake_session_factory`, incluindo a limitação
  conhecida de `GetResources` não devolver recurso sem tag nenhuma.
- `test_publish.py` — montagem do payload e chamada de `sns:Publish`.
- `test_handler_continuous_tagging.py` — ponta a ponta com sessão/clients
  falsos: recurso em escopo sem IaC (tagueia), gerenciado por IaC (pula e
  sinaliza), já tagueado corretamente (`ja_ok` sem erro), evento duplicado
  (idempotência), evento fora do escopo do CSV (ignorado).

Para `infra/`:

- `test_infra_event_patterns.py` — os blocos `EventPattern` do template
  (colados manualmente, já que o CloudFormation não inclui JSON externo
  nessa propriedade) batem com `infra/event_pattern.*.generated.json`.
- `test_infra_template_cfn_lint.py` — `infra/template.yaml` sem findings do
  `cfn-lint` (via `cfnlint.api.lint_all`, sem Docker nem AWS) — pula
  silenciosamente se `cfn-lint` não estiver instalado (`pytest.importorskip`).
- `test_infra_statemachine.py` — a definição do Step Functions
  (`infra/statemachine/etapa3_delay_e_execucao.asl.json`) é uma Amazon
  States Language válida depois da substituição dos placeholders
  (`${DebounceSeconds}`/`${LambdaArn}`): `StartAt` existe, todo `Next`/
  `Default` aponta para um estado real, todo estado é terminal ou tem
  `Next`.

Ao adicionar um módulo novo (Etapa 4 em diante), crie um novo grupo de
arquivos `test_<módulo>_<área>.py` seguindo o mesmo padrão, em vez de
acrescentar num arquivo já existente de outro módulo.

Fixtures compartilhadas ficam em [`conftest.py`](conftest.py) —
`recurso_factory`/`relatorio_etapa1_factory` para dicts no formato da Etapa
1, `decisao_factory`/`relatorio_decisao_factory` para o formato da Etapa 2a
(entrada da Etapa 2b), `fake_session_factory` para uma sessão boto3 falsa
(sem rede real) nos testes que exercitam a revalidação de `tag_execution.py`
ou a leitura de `single_resource.py`, e `cloudtrail_event_factory` para
eventos sintéticos no formato "AWS API Call via CloudTrail" (Etapa 3).
Reaproveite em vez de recriar esses dicts/stubs à mão em um novo arquivo.

Toda função de teste tem uma docstring curta explicando o *porquê* do
cenário (não só repetindo o nome) — mantenha esse padrão nos testes novos;
é o que torna a suíte legível como documentação das regras de negócio, não
só como verificação.
