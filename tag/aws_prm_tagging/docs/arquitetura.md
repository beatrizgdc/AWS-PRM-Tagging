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

  > **Limitação conhecida e importante:** a documentação oficial da AWS é
  > explícita — *"`GetResources` does not return untagged resources"*
  > ([referência](https://docs.aws.amazon.com/resourcegroupstagging/latest/APIReference/API_GetResources.html)).
  > Ou seja: um recurso que **nunca** recebeu tag nenhuma, de nenhuma chave,
  > é invisível para `discover_generic_resources` — não aparece no relatório
  > como `sem_tag` nem de nenhuma outra forma. (Um recurso que já teve
  > alguma tag no passado e hoje não tem nenhuma aparece normalmente, com
  > `"Tags": []`.) A própria AWS recomenda o AWS Resource Explorer
  > (`tag:none`) para achar esses casos, mas isso exige um índice já criado
  > na conta — criar esse índice é uma escrita, o que quebraria a premissa
  > de "100% somente-leitura" da Etapa 1 se feito por este script. Decisão
  > tomada: por ora, essa lacuna fica documentada como limitação conhecida
  > em vez de resolvida — não pega recursos que nunca foram tagueados nenhuma
  > vez. Isso não foi pego pelo teste em LocalStack porque a emulação de lá
  > não impõe essa mesma restrição (ver
  > [test/localstack/README.md](../test/localstack/README.md)).

- **`discover_bedrock_resources`** — `bedrock:ListInferenceProfiles` com
  `typeEquals=APPLICATION` (profiles de sistema/cross-region não suportam tag
  e são excluídos por construção, não por filtro posterior), depois
  `bedrock:ListTagsForResource` por profile. `tipo_recurso` do resultado:
  `application_inference_profile`.
- **`discover_eks_resources`** — para cada cluster (`eks:ListClusters` +
  `DescribeCluster`): tags do cluster (`tipo_recurso="cluster"`); para cada
  node group (`ListNodegroups` + `DescribeNodegroup`): tags do node group
  (`tipo_recurso="node_group"`), e as instâncias EC2 associadas via
  `autoscaling:DescribeAutoScalingGroups` (nodes managed) e via as tags
  automáticas `eks:cluster-name` / `kubernetes.io/cluster/<nome>` (nodes
  self-managed, heurística best-effort documentada no código) —
  `tipo_recurso="node"`; para essas instâncias, os volumes EBS anexados
  (`ec2:DescribeVolumes`) — `tipo_recurso="ebs_volume"`; e os load balancers
  do cluster, identificados pelas tags de convenção do AWS Load Balancer
  Controller (`elbv2.k8s.aws/cluster`, `kubernetes.io/cluster/<nome>` —
  também best-effort, não há uma API que amarre LB a cluster diretamente) —
  `tipo_recurso="load_balancer"`. Fargate on EKS nunca aparece aqui porque
  não gera instâncias EC2.

  As instâncias EC2 e volumes EBS dos nodes **também** são descobertos pelo
  passo genérico (o namespace `ec2` não está em `_DEDICATED_SERVICE_CODES`),
  então o mesmo ARN pode aparecer duas vezes — uma com `servico="Amazon EC2"`
  e `tipo_recurso=None` (via `discover_generic_resources`), outra com
  `servico="Amazon EKS"` e `tipo_recurso="node"`/`"ebs_volume"` (via
  `discover_eks_resources`). `main.py` chama `report.dedupe_by_arn` depois
  de coletar tudo, que resolve isso mantendo a última ocorrência por ARN —
  como a descoberta de EKS roda depois da genérica no loop de `main.py`, a
  entrada mais específica (a do EKS) é a que sobrevive.

Toda chamada de API está envolvida em `try/except ClientError` com log e
`continue`/retorno parcial — uma falha pontual (ex.: `AccessDenied` em uma
região, serviço não disponível em uma região) nunca aborta a execução do
restante do script.

Cada recurso do relatório tem um campo `tipo_recurso` (`str | None`) —
`None` no passo genérico (onde `servico` já identifica o recurso sem
ambiguidade) e um dos valores acima nos casos especiais de EKS/Bedrock, onde
`servico` sozinho ("Amazon EKS", "Amazon Bedrock") não diferencia qual ARN é
qual.

### `tag_status.py`

`get_tag_status(tags, expected_tag_value)` — comparação **case-sensitive** da
chave `aws-apn-id` (a AWS exige a chave exatamente em minúsculas; uma
variante com capitalização diferente não é reconhecida para atribuição e é
tratada como `sem_tag`). Retorna `(status, valor_encontrado)`.

`find_similar_tag_keys(tags, target_key="aws-apn-id")` — não é um conceito
do guia oficial da AWS (que só define a chave exata exigida); é uma checagem
defensiva própria, pensada para a Etapa 2a/2b: identifica chaves com a mesma
grafia de `aws-apn-id` mas capitalização diferente (ex.: `AWS-APN-ID`,
`Aws-Apn-Id`) — provavelmente erro de digitação humano — para que quem for
agir sobre o relatório não taguei em cima de uma tag "quase certa" sem
revisar antes. Definição deliberadamente restrita a diferença de case; não
normaliza separador (`aws_apn_id`) nem espaço, que seriam uma convenção de
nome diferente. Alimenta dois campos no relatório de cada recurso:
`tag_similar_encontrada` (bool) e `tag_similar_chaves` (lista das chaves
encontradas, vazia quando `false`) — e o agregado
`resumo.total_tag_similar_encontrada` em `report.py`.

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

`dedupe_by_arn` remove entradas duplicadas pelo mesmo ARN antes de montar o
relatório (ver nota sobre EKS em `resource_discovery.py` acima), mantendo a
última ocorrência — `main.py` chama isso depois do loop de regiões, antes de
`build_report`. `build_report` agrega a lista (já deduplicada) em contadores
(`por_status_tag`, `por_status_iac`, `por_servico`,
`total_tag_similar_encontrada` — ver `tag_status.py` acima) e monta o JSON
final no formato descrito no `README.md` do estágio 1. Ambas são funções
puras, sem I/O.

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
