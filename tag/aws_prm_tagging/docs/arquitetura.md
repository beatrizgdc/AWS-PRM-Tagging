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

## Núcleo compartilhado vs. entrypoint de estágio

Todo módulo no nível raiz do pacote (`services.py`, `regions.py`,
`resource_discovery.py`, `tag_status.py`, `iac_detection.py`, `decision.py`,
`tag_execution.py`, `ou_tree.py`, `report.py`, `retry.py`) é **núcleo
compartilhado**: pode ser importado por qualquer um dos 4 estágios sem saber
qual estágio está chamando. `decision.py` (Etapa 2a) e `tag_execution.py`
(Etapas 2b/2c) estão aqui pelo mesmo motivo que `resource_discovery.py`
está — a Etapa 3 (automação contínua) e a Etapa 4 (varredura recorrente)
também vão precisar classificar e aplicar a tag, não só a Etapa 2.

Só `main.py` é **entrypoint de estágio**: hoje um único CLI com 3
subcomandos, um por estágio implementado (`map`, `decide`, `apply` — ver
[README.md](../README.md#uso)), cada um orquestrando só o I/O específico
daquele estágio (CLI args + leitura/escrita de arquivo). Quando as Etapas
3-4 ganharem seus próprios entrypoints (handlers Lambda, ver
[arquitetura-multicliente.md](arquitetura-multicliente.md)), eles seguem o
mesmo padrão: importam os módulos do núcleo em vez de duplicar lógica. Só
`apply` (Etapa 2b) faz alguma chamada de API na conta do cliente além de
leitura pura — e mesmo assim só chamadas de **leitura** (revalidação); a
Etapa 2b em si nunca escreve (ver `tag_execution.py` abaixo). A primeira
escrita de fato só existe na Etapa 2c, ainda não implementada.

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

### `decision.py`

Etapa 2a — classifica cada recurso do relatório da Etapa 1 em `taguear` /
`pular_iac` / `ja_ok` / `conflito` (regras de precedência completas no
docstring do módulo). Puro: sem boto3, sem rede, sem leitura de arquivo —
recebe o relatório da Etapa 1 já carregado como dict e devolve outro dict.

Reaproveita `tag_status.get_tag_status`, mas **recalculado** a partir de
`valor_tag_encontrado` (o valor bruto que a Etapa 1 já extraiu) contra o
`expected_tag_value` recebido nesta chamada — nunca confia no `status_tag`
já congelado do relatório da Etapa 1. Isso é proposital: a Etapa 1 roda uma
vez por conta, não por sub-OU, mas sub-OUs diferentes podem ter
contratos/product codes diferentes (ver
[arquitetura-multicliente.md](arquitetura-multicliente.md)) — uma única
saída da Etapa 1 pode alimentar várias chamadas da Etapa 2a, cada uma com o
`expected_tag_value` da sub-OU correspondente.

`iac.tipo == "desconhecido"` é tratado como "IaC não detectado" nesta etapa
— só `cloudformation`/`terraform_heuristico` levam a `pular_iac` quando a
tag está ausente (suposição confirmada; ver docstring do módulo).

Filtra defensivamente por `tipo_recurso` os dois serviços com sub-recursos
(EKS: `cluster`/`node_group`/`node`/`ebs_volume`/`load_balancer`; Bedrock:
`application_inference_profile`). Na prática é um no-op contra a saída real
da Etapa 1 — `resource_discovery.py` só produz esses tipos — mas protege
contra um relatório sintético/malformado ou uma mudança futura na Etapa 1.
Recursos fora desse escopo são excluídos do relatório de decisão (não
aparecem em `recursos` nem em `erros`, só contados em
`resumo.total_excluidos_fora_de_escopo`); recursos malformados (campos
obrigatórios ausentes/com tipo errado) viram entradas em `erros` sem
derrubar o processamento do restante do lote.

`build_decision_report` monta o relatório de saída no mesmo estilo de
`report.build_report` (mesmas chaves de topo, agregação via `Counter`,
`por_servico` ordenado) — é o contrato de entrada da Etapa 2b, coberta a
seguir.

### `tag_execution.py`

Etapa 2b (dry-run) e base para a Etapa 2c (execução real, ainda não
implementada) — consome `decision.build_decision_report` (Etapa 2a) e
decide, para cada recurso `taguear`, qual API chamar e com qual
agrupamento. Ao contrário dos módulos anteriores, **não** é 100%
sem-efeito: faz chamadas de leitura reais na conta (revalidação, ver
abaixo) — mas nunca de escrita nesta etapa. Docstring completo do módulo
cobre o raciocínio em detalhe; resumo:

**Garantia estrutural (não por convenção) de que só `"taguear"` gera uma
chamada** — `select_taggable()` é o único ponto de entrada aceito pelo
resto do módulo. Ela converte cada recurso em um `TaggableResource`, um
tipo que **não tem campo `decisao`**. Toda função downstream recebe
`TaggableResource`, nunca o dict bruto da Etapa 2a — não existe caminho de
código que aceite `pular_iac`/`ja_ok`/`conflito` como parâmetro.

**Roteamento de API** (`route_strategy`), confirmado contra a documentação
oficial de cada API (não só a página-índice de "supported services", que é
renderizada via JS e não dá para extrair):

| Recurso | API | Batch? |
|---|---|---|
| Genérico (maioria do CSV, + nodes/volumes EBS de EKS) | `tag:TagResources` | até 20 ARNs/chamada (limite documentado) |
| EKS cluster / node group | `eks:TagResource` | não — `resourceArn` singular |
| Bedrock application inference profile | `bedrock:TagResource` | não — `resourceARN` singular |
| Load balancer de EKS | `elasticloadbalancing:AddTags` | tratado como não (parâmetro aceita array, mas sem confirmação oficial de múltiplos ARNs por chamada — decisão conservadora até validar em sandbox) |

Nodes e volumes EBS de EKS usam o caminho genérico de propósito: são
instância/volume EC2 comuns por baixo do capô, sem necessidade de API
dedicada — só cluster, node group e load balancer entram na tabela de
estratégia dedicada.

**Revalidação e idempotência**: antes de agir sobre cada recurso (real ou
simulado), o orquestrador relê o estado atual da tag na AWS
(`revalidate=True` por padrão, `--no-revalidate` desliga). Se a tag já
estiver com o valor esperado — porque uma execução anterior já aplicou, ou
outra automação aplicou por fora —, o recurso é reportado como
`"ja_tagueado"` e nenhuma chamada de escrita é feita. É isso que torna
reexecuções do mesmo relatório de decisão idempotentes por construção, sem
nenhum arquivo de progresso: o estado da tag na AWS *é* a fonte da verdade
de "já foi feito ou não" — combinado com o fato de que toda API de tagging
usada aqui é uma operação de conjunto (aplicar o mesmo valor duas vezes é
no-op). A leitura de revalidação usa `tag:GetResources` com
`TagFilters=[{"Key": "aws-apn-id"}]` (uma varredura por região cobre todos
os recursos genéricos de uma vez) para o caminho genérico, e
`eks:ListTagsForResource` / `bedrock:ListTagsForResource` /
`elasticloadbalancing:DescribeTags` para os caminhos dedicados — mesmos
clients já usados por `resource_discovery.py` na Etapa 1. Falha na leitura
de revalidação (ex.: `AccessDenied`) não bloqueia a tentativa principal —
só perde o benefício da revalidação para aquele recurso.

**`Executor`**: único ponto de decisão entre "logar o que seria feito" e
"fazer de verdade". `DryRunExecutor` (único que existe até a Etapa 2c) nunca
chama uma API de escrita — só loga, no mesmo formato estruturado que vira o
relatório de saída. Todo o roteamento/agrupamento/revalidação acima é
comum às duas etapas; a Etapa 2c só precisa adicionar um `LiveExecutor` que
implementa o mesmo `Protocol` chamando boto3 de verdade (incluindo o
parsing de `FailedResourcesMap` para falha parcial de lote, que o dry-run
não precisa simular — sempre assume sucesso, já que não há chamada real).

**Formato do relatório de saída**: mesmo estilo de `report.py`/`decision.py`
(`conta_id`, `executado_em`, `valor_tag_esperado`, `resumo` agregado via
`Counter`, lista `recursos` com `resultado` por item) — base direta para a
Etapa 2c e para o dashboard.

### `retry.py`

Decorator `with_backoff()` — retry com backoff exponencial + jitter
especificamente para códigos de erro de throttling
(`Throttling`, `ThrottlingException`, `TooManyRequestsException`,
`RequestLimitExceeded`, `ProvisionedThroughputExceededException`,
`RequestThrottledException`, `SlowDown`) e para erros de conexão. Qualquer
outro erro (ex.: `AccessDeniedException`) propaga imediatamente — não faz
sentido re-tentar um erro de permissão.

### `main.py`

Único módulo com efeito de I/O de CLI (leitura de argumentos de linha de
comando e leitura/escrita de arquivo JSON). Toda a lógica de negócio vive
nos módulos acima, então os próximos estágios podem importar
`resource_discovery`, `tag_status`, `iac_detection`, `decision`,
`tag_execution` etc. diretamente em um handler Lambda sem depender do CLI.

Três subcomandos (`argparse` com `add_subparsers`), um por estágio
implementado — encadeados manualmente por quem roda o CLI (a saída em
arquivo de um é a entrada em arquivo do próximo), não por um orquestrador
único:

- `map` — Etapa 1. Comportamento idêntico ao script original de estágio
  único (mesmos argumentos `--expected-tag-value`/`--profile`/`--output`).
- `decide` — Etapa 2a. Lê `--input` (saída de `map`), escreve o relatório de
  decisão. Não usa boto3.
- `apply` — Etapa 2b. Lê `--input` (saída de `decide`) e reaproveita
  `valor_tag_esperado` de dentro desse relatório para a chamada a
  `tag_execution.run_stage2b` — de propósito, **não** aceita um
  `--expected-tag-value` próprio: aplicar um valor diferente do que foi
  usado para classificar os recursos como `taguear` seria inconsistente
  com a própria decisão que está sendo executada. `--no-revalidate`
  desliga a revalidação (seção `tag_execution.py` acima); `--live` existe
  só para dar um erro explícito ("Etapa 2c ainda não implementada") em vez
  de a flag ser silenciosamente ignorada.

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
