# Arquitetura

## Visão geral do fluxo

`main.py` orquestra, nesta ordem:

1. Resolve a sessão boto3 (`--profile` ou cadeia padrão de credenciais).
2. `sts.get_caller_identity` para obter o `conta_id`.
3. `regions.get_active_regions` para listar regiões comerciais ativas.
4. `services.load_services` para carregar a lista oficial de serviços do CSV.
5. Para cada região: `resource_discovery.discover_generic_resources`,
   `discover_bedrock_resources` e `discover_eks_resources`, cada chamada
   isolada em `try/except` — uma falha em uma etapa/região não aborta o
   restante da execução.
6. `ou_tree.discover_ou_tree` (uma vez, não por região).
7. `report.build_report` monta o JSON final e `main.py` grava em disco.

Nenhuma dessas funções tem efeito colateral sobre a conta AWS: todas usam
exclusivamente operações `Describe*`/`List*`/`Get*`.

## Módulos

### `services.py`

Carrega `data/resource-tagging-included-services.csv` (cópia exata do CSV
oficial fornecido pela AWS — nunca a lista de serviços é reescrita ou
complementada à mão) e expõe `load_services() -> list[Service]`.

Também resolve o problema de que a Resource Groups Tagging API devolve ARNs,
não os "Product Service Code" usados no CSV: `classify_arn(arn, services)`
mapeia o namespace do ARN (`arn:aws:<namespace>:...`) para a linha
correspondente do CSV via a tabela interna `_NAMESPACE_TO_CODE`. Essa tabela é
construída a partir de convenções documentadas de nomenclatura de ARN da AWS,
não do CSV (o CSV não traz essa correspondência).

Duas ambiguidades conhecidas, documentadas em comentários no próprio arquivo:

- **`ec2`** é usado tanto por Amazon EC2 quanto pelos recursos de rede
  faturados sob "AWS Transit Gateway"/"Amazon VPC Lattice" (mesmo código de
  produto `AmazonVPC` no CSV, para duas linhas diferentes). Desambiguado por
  tipo de recurso dentro do ARN (`instance`, `volume`, `security-group` etc.
  vão para EC2; `vpc`, `transit-gateway`, `vpc-peering-connection` etc. vão
  para a linha de rede).
- **`rds`** é compartilhado por Amazon RDS, Aurora, Amazon DocumentDB e Amazon
  Neptune (todos usam ARNs `arn:aws:rds:...`). Sem uma chamada adicional à API
  de cada engine para inspecionar o atributo `Engine`, não há como
  diferenciar com certeza pelo ARN — todos caem no bucket "Amazon Relational
  Database Service (RDS)". Isso é um ponto de refinamento futuro, não um bug:
  o recurso ainda é descoberto e classificado corretamente quanto ao status
  da tag, só o rótulo de serviço no relatório pode não distinguir a engine.

`Amazon Bedrock` (plano, não AgentCore) e `Amazon EKS` são deliberadamente
**omitidos** dessa tabela: são tratados por lógica dedicada em
`resource_discovery.py` e nunca reclassificados no passo genérico, para não
gerar duplicatas.

### `regions.py`

`get_active_regions(session)` chama `ec2:DescribeRegions` com
`AllRegions=True` e filtra por `OptInStatus in {"opt-in-not-required",
"opted-in"}` — conforme o guia oficial, PRM hoje só é suportado em regiões
comerciais.

### `resource_discovery.py`

Três funções públicas, cada uma isolada e reutilizável nos próximos estágios:

- **`discover_generic_resources`** — pagina `resourcegroupstaggingapi:GetResources`
  sem filtro de tipo (para não depender de uma lista de filtros mantida à
  mão) e classifica cada ARN retornado via `services.classify_arn`. ARNs que
  não pertencem a nenhum serviço do CSV, ou que pertencem a Bedrock/EKS
  (tratados à parte), são descartados.
- **`discover_bedrock_resources`** — `bedrock:ListInferenceProfiles` com
  `typeEquals=APPLICATION` (profiles de sistema/cross-region não suportam tag
  e são excluídos por construção, não por filtro posterior), depois
  `bedrock:ListTagsForResource` por profile.
- **`discover_eks_resources`** — para cada cluster (`eks:ListClusters` +
  `DescribeCluster`): tags do cluster; para cada node group
  (`ListNodegroups` + `DescribeNodegroup`): tags do node group, e as
  instâncias EC2 associadas via `autoscaling:DescribeAutoScalingGroups` (nodes
  managed) e via as tags automáticas `eks:cluster-name` /
  `kubernetes.io/cluster/<nome>` (nodes self-managed, heurística best-effort
  documentada no código); para essas instâncias, os volumes EBS anexados
  (`ec2:DescribeVolumes`); e os load balancers do cluster, identificados pelas
  tags de convenção do AWS Load Balancer Controller
  (`elbv2.k8s.aws/cluster`, `kubernetes.io/cluster/<nome>` — também
  best-effort, não há uma API que amarre LB a cluster diretamente). Fargate on
  EKS nunca aparece aqui porque não gera instâncias EC2.

Toda chamada de API está envolvida em `try/except ClientError` com log e
`continue`/retorno parcial — uma falha pontual (ex.: `AccessDenied` em uma
região, serviço não disponível em uma região) nunca aborta a execução do
restante do script.

### `tag_status.py`

`get_tag_status(tags, expected_tag_value)` — comparação **case-sensitive** da
chave `aws-apn-id` (a AWS exige a chave exatamente em minúsculas; uma
variante com capitalização diferente não é reconhecida para atribuição e é
tratada como `sem_tag`). Retorna `(status, valor_encontrado)`.

`tags_list_to_dict` converte o formato `[{"Key":..,"Value":..}]` (a maioria
das APIs) e `[{"key":..,"value":..}]` (Bedrock) para `dict`.

### `iac_detection.py`

- Presença da tag `aws:cloudformation:stack-name` → `cloudformation` (cobre
  também CDK, que gera stacks CloudFormation por trás). Checado primeiro —
  tem precedência sobre qualquer sinal de Terraform no mesmo recurso.
- Convenções de tag comuns de Terraform → `terraform_heuristico`. Terraform
  não tem nenhum sinal nativo confiável (ao contrário do CloudFormation) —
  isso é heurística declarada, nunca tratada como certeza. Dois níveis de
  checagem: (1) nomes de chave conhecidos (`terraform=true`,
  `managed-by=terraform` e variantes de capitalização); (2) catch-all —
  qualquer tag cujo **valor** seja literalmente `terraform` (case-insensitive),
  independente do nome da chave, já que orgs usam nomes arbitrários (`IaC`,
  `Provisioner`, `CreatedBy`, `source` etc.) para a mesma convenção. A
  heurística erra deliberadamente para o lado de detectar demais: um falso
  positivo aqui só custa uma tag que deixa de ser aplicada automaticamente
  (fica para revisão manual); um falso negativo poderia levar uma etapa
  futura de escrita a taguear via API um recurso na verdade gerenciado por
  Terraform, causando drift no próximo `terraform apply` — o cenário que a
  AWS explicitamente orienta a evitar.
- Ausência de qualquer sinal → `desconhecido`. Nunca se assume "não é IaC".

### `ou_tree.py`

`discover_ou_tree(session, account_id)`: `organizations:DescribeOrganization`
para checar se a conta atual é a conta de gerenciamento
(`MasterAccountId == account_id`). Se não for, ou se a conta não pertencer a
uma Organization (`AWSOrganizationsNotInUseException`), loga um aviso e
retorna `None` sem quebrar a execução. Se for, monta a árvore recursivamente
via `ListRoots` → `ListOrganizationalUnitsForParent` → `ListAccountsForParent`.

### `report.py`

Agrega a lista de recursos em contadores (`por_status_tag`, `por_status_iac`,
`por_servico`) e monta o JSON final no formato descrito no `README.md`
original do estágio 1. Função pura, sem I/O.

### `retry.py`

Decorator `with_backoff()` — retry com backoff exponencial + jitter
especificamente para códigos de erro de throttling
(`Throttling`, `ThrottlingException`, `TooManyRequestsException`,
`RequestLimitExceeded`, `ProvisionedThroughputExceededException`,
`RequestThrottledException`, `SlowDown`) e para erros de conexão. Qualquer
outro erro (ex.: `AccessDeniedException`) propaga imediatamente — não faz
sentido re-tentar um erro de permissão.

### `main.py`

Único módulo com efeito de I/O (leitura de argumentos de linha de comando e
escrita do JSON de saída). Toda a lógica de negócio vive nos módulos acima,
então os próximos estágios podem importar `resource_discovery`,
`tag_status`, `iac_detection` etc. diretamente em um handler Lambda sem
depender do CLI.

## Por que não há arquivo de variáveis de ambiente

Credenciais AWS nunca são lidas de um arquivo próprio do projeto. `boto3.Session(profile_name=args.profile)`
usa a cadeia de credenciais padrão do boto3/AWS CLI:

1. Variáveis de ambiente (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
   `AWS_SESSION_TOKEN`, `AWS_PROFILE`, `AWS_REGION`).
2. Arquivos `~/.aws/credentials` e `~/.aws/config` (perfil nomeado via
   `--profile`, ou `default`).
3. IAM role (instance profile de EC2, task role de ECS, execution role de
   Lambda), quando executado dentro da AWS.

Criar um `.env` próprio duplicaria esse mecanismo e adicionaria o risco de
uma credencial em texto plano ser commitada por engano. O único parâmetro que
varia por execução e não é uma credencial (`--expected-tag-value`) é passado
explicitamente por linha de comando, nunca hardcoded — ver
`--expected-tag-value` em `main.py` e a ausência de qualquer valor de tag,
código de produto, ID de conta ou nome de cliente no restante do código
(`grep -rniE "pc:|[0-9]{12}"` sobre `aws_prm_tagging/` só retorna o texto de
`--help`).
