# Arquitetura

## Visão geral do fluxo

`main.py` tem 3 subcomandos hoje (`map`/`decide`/`apply` — ver seção
`main.py` abaixo); esta visão geral é do subcomando `map` (Etapa 1), o mais
envolvido dos três. `decide` (Etapa 2a) e `apply` (Etapas 2b/2c) são bem
mais diretos — um lê a saída do anterior, chama a função de núcleo
correspondente (`decision.build_decision_report`/
`tag_execution.run_tagging_execution`) e grava o resultado; detalhes nas
seções `decision.py`/`tag_execution.py` abaixo.

`map` orquestra, nesta ordem:

1. Resolve a sessão boto3 (`--profile` ou cadeia padrão de credenciais).
2. `sts.get_caller_identity` para obter o `conta_id`.
3. `regions.get_active_regions` para listar regiões comerciais ativas.
4. `services.load_services` para carregar a lista oficial de serviços do CSV.
5. Para cada região: `resource_discovery.discover_generic_resources`,
   `discover_bedrock_resources` e `discover_eks_resources`. Cada uma devolve
   `(recursos, falhas)` — uma falha de API (`ClientError`, ex.: `AccessDenied`,
   throttling esgotado, falha no meio da paginação) é capturada DENTRO da
   função e volta na segunda posição da tupla, sem abortar o restante da
   descoberta nem daquela etapa/região nem das seguintes; `main.py` só
   estende `falhas_descoberta` com o que recebe. O `try/except Exception`
   que também existe em `main.py` ao redor de cada chamada é só um backstop
   para bug de programação genuíno — a falha de API esperada nunca chega a
   levantar exceção até ali.
6. `ou_tree.discover_ou_tree` (uma vez, não por região).
7. `report.build_report` monta o JSON final e `main.py` grava em disco.

Nenhuma dessas funções tem efeito colateral sobre a conta AWS: todas usam
exclusivamente operações `Describe*`/`List*`/`Get*`.

## Núcleo compartilhado vs. entrypoint de estágio

Todo módulo no nível raiz do pacote (`services.py`, `regions.py`,
`resource_discovery.py`, `tag_status.py`, `iac_detection.py`, `decision.py`,
`tag_execution.py`, `ou_tree.py`, `report.py`, `retry.py`, `event_mapping.py`,
`event_parser.py`, `single_resource.py`, `publish.py`) é **núcleo
compartilhado**: pode ser importado por qualquer um dos 4 estágios sem saber
qual estágio está chamando. `decision.py` (Etapa 2a) e `tag_execution.py`
(Etapas 2b/2c) estão aqui pelo mesmo motivo que `resource_discovery.py`
está — a Etapa 3 (automação contínua) e a Etapa 4 (varredura recorrente)
também precisam classificar e aplicar a tag, não só a Etapa 2; `publish.py`
segue o mesmo princípio para o lado de reporte (Etapa 3 é quem primeiro
precisou publicar algo, mas a Etapa 4 reaproveita o mesmo módulo depois).

Dois módulos são **entrypoint de estágio**: `main.py` (CLI com 3
subcomandos, um por estágio implementado como CLI — `map`, `decide`, `apply`
— ver [README.md](../README.md#uso)) e `handler_continuous_tagging.py`
(handler Lambda da Etapa 3, invocado pelo Step Functions — ver
[infra/README.md](../infra/README.md)). Os dois seguem o mesmo padrão: só
orquestram I/O específico do estágio (args de CLI / variáveis de ambiente e
sessão boto3), importando os módulos do núcleo em vez de duplicar lógica.
Quando a Etapa 4 ganhar seu próprio entrypoint (handler Lambda agendado),
segue o mesmo padrão dos dois acima. `apply` cobre tanto a Etapa 2b
(`--live` omitido, default — só leitura na conta, para revalidação) quanto
a Etapa 2c (`--live` — primeira escrita de fato em toda a automação); é a
mesma função (`tag_execution.run_tagging_execution`) nos dois casos, só
trocando qual `Executor` está por baixo (ver `tag_execution.py` abaixo) —
`handler_continuous_tagging.py` (Etapa 3) chama essa MESMA função, com
`dry_run=False`, sobre um relatório de decisão de um único recurso.

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

Três ambiguidades/restrições de escopo conhecidas, documentadas em
comentários no próprio arquivo:

- **`ssm`** — o CSV traz a nota "OpsCenter only" para "AWS Systems
  Manager", mas o namespace `ssm` nos ARNs cobre muito mais que isso
  (parameters, documents, maintenance windows, associations...).
  `classify_arn` restringe pelo tipo de recurso do ARN (mesmo padrão do
  `ec2` abaixo): só `opsitem` é classificado como Systems Manager; qualquer
  outro tipo sob `ssm` devolve `None` e fica fora da descoberta.
- **`ec2`** é usado tanto por Amazon EC2 quanto pelos recursos de rede
  faturados sob a linha "AWS Transit Gateway" do CSV (mesmo código de
  produto `AmazonVPC`). Desambiguado por tipo de recurso dentro do ARN
  (`instance`, `volume`, `security-group` etc. vão para EC2; `vpc`,
  `transit-gateway`, `vpc-peering-connection` etc. vão para a linha de
  rede). O namespace `vpc-lattice` também usa o código `AmazonVPC`, mas
  **não** é ambíguo (identifica o recurso sem dúvida) — por isso é tratado
  à parte, por nome (`_service_by_name`), em vez de cair na mesma busca por
  código que pegaria sempre a primeira linha ("AWS Transit Gateway") e
  rotularia todo recurso VPC Lattice errado no relatório (correção
  aplicada; a tag em si nunca esteve errada, só o rótulo `servico`).
- **`rds`** é compartilhado por Amazon RDS, Aurora, Amazon DocumentDB e Amazon
  Neptune (todos usam ARNs `arn:aws:rds:...`). Sem uma chamada adicional à API
  de cada engine para inspecionar o atributo `Engine`, não há como
  diferenciar com certeza pelo ARN — todos caem no bucket "Amazon Relational
  Database Service (RDS)". Isso é um ponto de refinamento futuro, não um bug:
  o recurso ainda é descoberto e classificado corretamente quanto ao status
  da tag, só o rótulo de serviço no relatório pode não distinguir a engine
  (registrado em [melhorias-futuras.md](melhorias-futuras.md#amazon-documentdb-e-amazon-neptune-aparecem-como-amazon-rds-no-relatório)).

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

Três funções públicas, cada uma isolada e reutilizável nos próximos
estágios. Todas devolvem `(recursos, falhas)` — `falhas` é uma lista de
dicts `{"regiao", "etapa", "erro"}` no mesmo formato que `main.py` grava em
`falhas_descoberta` (ver `report.py` abaixo): uma `ClientError` capturada
durante a descoberta (ex.: `AccessDenied`, throttling esgotado, falha no
meio da paginação) volta como um item de `falhas`, não só um log — antes
essas falhas eram só logadas aqui dentro e nunca chegavam ao JSON de saída.

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
  > em vez de resolvida (registrada em
  > [melhorias-futuras.md](melhorias-futuras.md#cobertura-de-recursos-que-nunca-tiveram-tag-nenhuma))
  > — não pega recursos que nunca foram tagueados nenhuma
  > vez. Isso não foi pego pelo teste em LocalStack porque a emulação de lá
  > não impõe essa mesma restrição (ver
  > [test/localstack/README.md](../test/localstack/README.md)).

- **`discover_bedrock_resources`** — `bedrock:ListInferenceProfiles` com
  `typeEquals=APPLICATION` (profiles de sistema/cross-region não suportam tag
  e são excluídos por construção, não por filtro posterior), depois
  `bedrock:ListTagsForResource` por profile. `tipo_recurso` do resultado:
  `application_inference_profile`. Se a leitura de tags de UM profile falhar,
  o recurso ainda entra no relatório (com `tags={}`, para não ficar
  invisível), mas a falha vira uma entrada em `falhas_descoberta` — sem
  isso, o relatório diria "sem tag" com confiança quando na verdade o
  estado real é desconhecido. Qualquer `ClientError` ao listar profiles
  (`AccessDenied` incluso) também vira `falhas_descoberta` — uma versão
  anterior tratava `AccessDenied`/`UnrecognizedClientException` como "região
  sem Bedrock, esperado" e não registrava nada; testado e comprovado errado:
  `AccessDenied` tipicamente indica falta de permissão IAM, não região sem
  suporte, e as duas coisas não dão pra distinguir com segurança só pelo
  código do erro (precisa de confirmação em sandbox, ver
  [melhorias-futuras.md](melhorias-futuras.md)) — sem essa falha registrada,
  uma role sem `bedrock:ListInferenceProfiles` produzia "0 profiles" com
  confiança total em toda região, escondendo um problema de permissão real
  na conta inteira.
- **`discover_eks_resources`** — para cada cluster (`eks:ListClusters` +
  `DescribeCluster`): tags do cluster (`tipo_recurso="cluster"`); para cada
  node group (`ListNodegroups` + `DescribeNodegroup`): tags do node group
  (`tipo_recurso="node_group"`), e as instâncias EC2 associadas via
  `autoscaling:DescribeAutoScalingGroups` (nodes managed) e via as tags
  automáticas `eks:cluster-name` / `kubernetes.io/cluster/<nome>` (nodes
  self-managed, heurística best-effort documentada no código) —
  `tipo_recurso="node"`; para essas instâncias, os volumes EBS anexados
  (`ec2:DescribeVolumes`, paginado via `NextToken` e em lotes de até 100
  IDs por filtro — `_describe_instances_all`/`_describe_volumes_all`, ver
  `_TAMANHO_LOTE_FILTRO_EC2`) — `tipo_recurso="ebs_volume"`; e os load
  balancers do cluster, identificados pelas tags de convenção do AWS Load
  Balancer Controller (`elbv2.k8s.aws/cluster`, `kubernetes.io/cluster/<nome>`
  — também best-effort, não há uma API que amarre LB a cluster diretamente)
  — `tipo_recurso="load_balancer"`. Fargate on EKS nunca aparece aqui porque
  não gera instâncias EC2.

  A listagem de load balancers da região (`describe_load_balancers` +
  `describe_tags`) roda **uma vez só** por chamada de
  `discover_eks_resources` (`_list_region_load_balancers_with_tags`), não
  uma vez por cluster — `_filter_load_balancers_for_cluster` depois só
  filtra esse resultado já coletado, sem nenhuma chamada de API adicional.
  Antes desta correção, uma conta com N clusters EKS relistava todos os
  load balancers da região N vezes.

  Falha ao listar clusters, descrever um cluster específico, listar/descrever
  um node group — cada um desses pontos "largos" vira uma entrada em
  `falhas_descoberta` (com o cluster/node group envolvido continuando pulado,
  não travando o resto da região). Falhas mais granulares dentro de um
  cluster (um chunk de instâncias/volumes EC2, um Auto Scaling Group
  isolado) continuam só logadas — o mesmo EC2/EBS ainda aparece no
  relatório via `discover_generic_resources` mesmo que o enriquecimento
  específico de EKS falhe, então o recurso em si não fica invisível, só
  perde o `tipo_recurso` mais específico (registrado como limitação
  conhecida em [melhorias-futuras.md](melhorias-futuras.md)).

  As instâncias EC2 e volumes EBS dos nodes **também** são descobertos pelo
  passo genérico (o namespace `ec2` não está em `_DEDICATED_SERVICE_CODES`),
  então o mesmo ARN pode aparecer duas vezes — uma com `servico="Amazon EC2"`
  e `tipo_recurso=None` (via `discover_generic_resources`), outra com
  `servico="Amazon EKS"` e `tipo_recurso="node"`/`"ebs_volume"` (via
  `discover_eks_resources`). `main.py` chama `report.dedupe_by_arn` depois
  de coletar tudo, que resolve isso mantendo a última ocorrência por ARN —
  como a descoberta de EKS roda depois da genérica no loop de `main.py`, a
  entrada mais específica (a do EKS) é a que sobrevive.

Toda chamada de API está envolvida em `try/except ClientError` com log,
`continue`/retorno parcial e (nos pontos "largos" de cada função, ver acima)
uma entrada na lista de `falhas` devolvida — uma falha pontual (ex.:
`AccessDenied` em uma região, serviço não disponível em uma região) nunca
aborta a execução do restante do script.

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

**`gerenciado_por_ferramenta_aws`** (novo campo em `detect_iac`, só
preenchido quando `tipo == "cloudformation"`): identifica pelo prefixo do
nome da stack se o CloudFormation por trás da tag foi gerado internamente
por uma automação da própria AWS — Elastic Beanstalk (`awseb-`), Control
Tower (`StackSet-AWSControlTower`), Service Catalog (`SC-`) — ou por
`eksctl` (`eksctl-`), em vez de escrito pelo cliente. **Isso nunca muda a
decisão** (`pular_iac` continua `pular_iac`, nunca tagueado via API) — só
existe para `decision.py` compor um `motivo` que aponte a ação real:
"taguear via IaC" é um conselho vazio quando não existe template do
cliente para editar (ex.: um recurso do Elastic Beanstalk — o cliente só
usou o console do EB, nunca viu o CloudFormation por trás). `None` quando
a stack não bate com nenhum prefixo conhecido — presumida como stack do
próprio cliente, mantendo o `motivo` genérico de sempre.

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

Recebe também `falhas_descoberta` (opcional, de `main.py`) — uma entrada
`{"regiao", "etapa", "erro"}` por falha de API capturada durante a
descoberta (genérico/bedrock/eks), devolvida pelas próprias funções de
`resource_discovery.py` (ver seção acima); `main.py` só concatena o que
recebe de volta, e continua com um `except Exception` próprio como backstop
para bug de programação genuíno, não para `ClientError` esperado. Existe
para que "0 recursos" e "a descoberta falhou nessa região" nunca fiquem
indistinguíveis no único artefato que a Etapa 4/dashboard consome — antes,
uma falha de descoberta só ia para o log da execução, que nunca chega no
push para o dashboard central.

### `decision.py`

Etapa 2a — classifica cada recurso do relatório da Etapa 1 em `taguear` /
`pular_iac` / `revisar_tag_similar` / `ja_ok` / `conflito` (regras de
precedência completas no docstring do módulo). Puro: sem boto3, sem rede,
sem leitura de arquivo — recebe o relatório da Etapa 1 já carregado como
dict e devolve outro dict.

`revisar_tag_similar`: tag `aws-apn-id` ausente, mas `tag_similar_encontrada`
(ver `tag_status.py` acima) é verdadeiro — uma tag de grafia parecida (ex.:
`AWS-APN-ID`) quase certamente é erro de digitação. Nunca vira `taguear`
diretamente: aplicar `aws-apn-id` por cima via API criaria uma segunda
chave quase-duplicada no recurso em vez de corrigir o erro original — fica
para revisão humana. Só é verificado quando a tag está mesmo ausente e o
recurso não é `pular_iac` (IaC detectado tem precedência: se o recurso já
não seria tagueado via API de qualquer forma, o risco de duplicata não
existe).

`pular_iac` com `motivo` específico por ferramenta: quando
`resource["iac"]["gerenciado_por_ferramenta_aws"]` vem preenchido (ver
`iac_detection.py` acima), o `motivo` aponta a ferramenta certa ("configure
a tag nas opções do Elastic Beanstalk") em vez do texto genérico "taguear
via IaC" — que seria um conselho vazio nesses casos, já que não existe
template do cliente para editar. A decisão continua `pular_iac` de
qualquer forma; só o texto explicativo muda.

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
`por_servico` ordenado) — é o contrato de entrada da Etapa 2b/2c, coberta a
seguir. Propaga `descoberta_executada_em` a partir de
`etapa1_report["executado_em"]` (quando a descoberta rodou, distinto do
`executado_em` desta própria função, que sempre reflete "agora") — é o que
`tag_execution.run_tagging_execution` usa para a checagem opcional de idade
máxima do relatório antes de agir (`max_decision_age_hours`).

### `tag_execution.py`

Etapas 2b (dry-run) e 2c (execução real) — a MESMA função,
`run_tagging_execution(..., dry_run: bool)`, atende as duas (`dry_run=True`
é o default/Etapa 2b). Consome `decision.build_decision_report` (Etapa 2a)
e decide, para cada recurso `taguear`, qual API chamar e com qual
agrupamento; em `dry_run=False` (Etapa 2c) chama essas APIs de verdade — é
a primeira escrita de fato em toda a automação. Docstring completo do
módulo cobre o raciocínio em detalhe; resumo:

**Garantia estrutural (não por convenção) de que só `"taguear"` gera uma
chamada** — `select_taggable()` é o único ponto de entrada aceito pelo
resto do módulo. Ela converte cada recurso em um `TaggableResource`, um
tipo que **não tem campo `decisao`**. Toda função downstream recebe
`TaggableResource`, nunca o dict bruto da Etapa 2a — não existe caminho de
código que aceite `pular_iac`/`revisar_tag_similar`/`ja_ok`/`conflito`
como parâmetro.

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

**Revalidação, idempotência e IaC** (aplica-se aos dois modos — a
revalidação existe independente de dry-run/live): antes de agir sobre cada
recurso, o orquestrador relê o estado atual dele na AWS (`revalidate=True`
por padrão, `--no-revalidate` desliga — mas **nunca aceito junto de
`dry_run=False`**, ver `RevalidacaoObrigatoriaError` abaixo) e reaplica a
MESMA regra de precedência de `decision.py` — não só compara o valor da
tag (agora de verdade nos 3 caminhos de revalidação: genérico, EKS/Bedrock
e ELB — uma versão anterior conferia IaC mas não tag similar, deixando um
`AWS-APN-ID` criado entre a Etapa 2a e esta execução passar despercebido).
Cinco desfechos, nesta ordem, nenhum gera chamada de escrita:

1. **A leitura de revalidação FALHOU** → tem precedência sobre tudo — sem
   saber o estado atual do recurso, a resposta segura é não arriscar
   sobrescrever um conflito que a leitura falhou em enxergar. (Uma versão
   anterior deixava a tentativa prosseguir nesse caso — corrigido: em modo
   live isso significava tentar `tag:TagResources`/etc. às cegas.) Dentro
   desse caso, o código do erro decide o resultado exato: se for um dos
   códigos de "recurso não encontrado" (ex.: um load balancer apagado entre
   a Etapa 1 e esta execução) → `"recurso_nao_encontrado_na_revalidacao"` —
   mesma distinção que já existe na classificação de erro de escrita
   (`_classificar_erro_aws`), "recurso sumiu" é o relatório da Etapa 1 estar
   desatualizado, não deveria disparar o mesmo alerta que uma falha real;
   qualquer outro erro (`AccessDenied`, throttling esgotado etc.) →
   `"revalidacao_falhou"`, genérico.
2. Valor já bate com o esperado → `"ja_tagueado"`.
3. Valor presente e diferente do esperado → `"conflito_na_revalidacao"` —
   nunca sobrescreve um conflito só porque ele apareceu depois da Etapa 2a.
4. Tag ausente e IaC detectado (só quando a tag está mesmo ausente — IaC
   nunca tem precedência sobre um conflito) → `"iac_detectado_na_revalidacao"`
   — cobre o recurso que passou a ser gerenciado por IaC entre a Etapa 1/2a
   e esta execução.
5. Tag ausente, sem IaC, mas uma tag de grafia parecida apareceu entre a
   Etapa 2a e esta execução → `"tag_similar_encontrada_na_revalidacao"` —
   mesma razão de `decision.py`: aplicar `aws-apn-id` por cima criaria uma
   segunda chave quase-duplicada em vez de corrigir o erro original.

Isso é o que torna reexecuções idempotentes por construção, sem nenhum
arquivo de progresso: o estado do recurso na AWS *é* a fonte da verdade de
"já foi feito ou não" — combinado com o fato de que toda API de tagging
usada aqui é uma operação de conjunto (aplicar o mesmo valor duas vezes é
no-op). A leitura de revalidação busca o conjunto COMPLETO de tags (não só
a `aws-apn-id`, senão não daria para checar IaC): `tag:GetResources` sem
filtro para o caminho genérico (varredura por região, parando assim que os
ARNs pedidos são encontrados), e `eks:ListTagsForResource` /
`bedrock:ListTagsForResource` / `elasticloadbalancing:DescribeTags` para os
dedicados — mesmos clients já usados por `resource_discovery.py` na Etapa
1, e já devolvem o conjunto completo sem custo extra.

`elasticloadbalancing:DescribeTags` aceita até 20 ARNs por chamada, mas 1
load balancer inexistente no lote (ex.: apagado entre a Etapa 1 e esta
execução) derruba a chamada inteira (`LoadBalancerNotFound`) — sem
tratamento especial, isso marcaria os outros até 19 ARNs do mesmo lote como
`revalidacao_falhou` também, mesmo existindo e estando OK.
`_revalidate_elb` trata isso: quando o lote falha, tenta de novo 1 ARN por
vez só para aquele lote, isolando qual ARN é de fato o problema em vez de
bloquear os demais.

`RevalidacaoObrigatoriaError`: `run_tagging_execution` recusa
`dry_run=False` junto de `revalidate=False` — em live, a proteção do item
1 acima só existe se a revalidação estiver ligada; desligar as duas coisas
juntas removeria a única salvaguarda contra sobrescrever um conflito.

**Idade máxima do relatório de decisão** (`max_decision_age_hours`,
opcional): recusa agir (`DecisionReportDesatualizadoError`) se a descoberta
subjacente (`decision_report["descoberta_executada_em"]`, propagado por
`decision.build_decision_report` a partir do `executado_em` da Etapa 1) for
mais velha que o limite. Ortogonal à revalidação: revalidação cobre "esse
recurso mudou"; idade do relatório cobre "esse universo de recursos pode
estar amplamente desatualizado".

**`Executor`**: único ponto de decisão entre "logar o que seria feito"
(`DryRunExecutor`, Etapa 2b) e "fazer de verdade" (`LiveExecutor`, Etapa
2c) — mesmo `Protocol`, nunca chamado diretamente pelo resto do módulo.
`LiveExecutor` chama a API nativa de cada estratégia (tabela acima),
decorada com `retry.with_backoff()`, e classifica cada falha em 3
categorias (`RESULTADO_ERRO_PERMISSAO`, `RESULTADO_RECURSO_NAO_ENCONTRADO`,
`RESULTADO_ERRO` genérico) — inclusive falha PARCIAL de um lote
(`FailedResourcesMap`, que pode vir num HTTP 200): só os ARNs listados ali
viram falha, os demais do mesmo lote viram sucesso. Uma falha (de lote ou
de recurso individual) nunca aborta o resto da execução — o próximo
lote/recurso é tentado normalmente.

**Relatório final** (`build_execution_report`): cobre as categorias que
fazem sentido para dashboard/leitura humana — `tagueado_sucesso` (só live)
e `simulado_sucesso` (só dry-run — as duas NUNCA se misturam na mesma
contagem, mesmo que `categoria_final` de ambas apareça no relatório) /
`falhou` / `pulado_iac` / `revisar_tag_similar` / `conflito` / `ja_ok` /
`erro_classificacao` —, mesclando três grupos: os recursos `taguear`
(resultado desta execução — inclui os pulados na revalidação, dobrados na
categoria correspondente); os `pular_iac`/`revisar_tag_similar`/`ja_ok`/
`conflito` que a Etapa 2a já decidiu direto, sem nunca passar por este
módulo; e os `erros` de classificação da Etapa 2a (recursos malformados
que nem chegaram a ser classificados — antes ficavam presos só em
`decision_report["erros"]` e nunca apareciam no relatório desta etapa).
Campo `origem` (`"decisao"` / `"revalidacao"` / `"execucao"`) preserva em
qual momento a classificação foi feita — a Etapa
4 usa isso, por exemplo, para diferenciar um conflito visto já na
descoberta original de um conflito que só apareceu no momento da escrita
(possível tag de outro parceiro AWS aplicada nesse meio-tempo). Mesmo
estilo de `report.py`/`decision.py` no resto (`conta_id`, `executado_em`,
`valor_tag_esperado`, `resumo` agregado via `Counter`).

**`execucao_inicial`** (parâmetro opcional, `False` por padrão): sinal de
observabilidade repassado ao relatório de saída, nunca usado para bloquear
a execução. Pensado para o Lambda futuro marcar a primeira execução de uma
conta — disparada pelo Custom Resource no `RequestType=Create` da stack,
que já é por natureza só a primeira vez — para facilitar revisão humana
posterior sem exigir aprovação prévia (que quebraria a automação
hands-off).

### `retry.py`

Decorator `with_backoff()` — retry com backoff exponencial + jitter
especificamente para códigos de erro de throttling
(`Throttling`, `ThrottlingException`, `TooManyRequestsException`,
`RequestLimitExceeded`, `ProvisionedThroughputExceededException`,
`RequestThrottledException`, `SlowDown`) e para erros de conexão. Qualquer
outro erro (ex.: `AccessDeniedException`) propaga imediatamente — não faz
sentido re-tentar um erro de permissão.

### `event_mapping.py` (Etapa 3)

Tabela `product_service_code` (do CSV oficial) -> evento(s) de criação de
recurso (`eventSource`/`eventName` do CloudTrail), indexada pela mesma
`services.load_services()` — nunca uma segunda lista de serviços própria.
`validate_against_csv()` é a rede de segurança contra divergência silenciosa
com o CSV (testada em `test_event_mapping.py`): toda entrada do CSV precisa
ter uma linha aqui, mesmo que `None` (ainda não mapeada).

**Cobertura parcial de propósito**: hoje cobre um **lote inicial de ~25**
dos ~85 serviços do CSV — os de alta confiança na forma do evento de
criação. Os demais estão `None`, documentados com o motivo (múltiplos tipos
de recurso sem "o" recurso óbvio, serviço muito novo, API não-REST, etc.) —
ver docstring do módulo e
[docs/melhorias-futuras.md](melhorias-futuras.md). Estender essa tabela é
trabalho incremental esperado, não uma correção pontual.

`build_event_patterns()` monta o(s) event pattern(s) do EventBridge a
partir da tabela, agrupados por `event_source` via `$or` (evita o
falso-positivo teórico de um pattern "achatado" misturando `eventSource` e
`eventName` de serviços diferentes) e **divididos em múltiplos patterns**
quando um único pattern ultrapassaria a quota padrão de 2.048 caracteres do
EventBridge (hoje, 3 regras — ver [infra/README.md](../infra/README.md)).
`python3 -m aws_prm_tagging.event_mapping` (do diretório pai) regrava
`infra/event_pattern.<N>.generated.json` — a única forma de manter
`infra/template.yaml` sincronizado com esta tabela, já que o CloudFormation
não tem como incluir um JSON externo dentro de `EventPattern` (dois testes,
`test_event_mapping.py` e `test_infra_event_patterns.py`, travam qualquer
divergência).

### `event_parser.py` (Etapa 3)

`parse_creation_event(event)` extrai 0, 1 ou N `ExtractedResource` (arn,
servico, regiao, tipo_recurso) de um evento `"AWS API Call via CloudTrail"`
do EventBridge — puro, nenhuma chamada de API. Devolve lista (não um único
valor) porque algumas APIs de criação são de lote (`ec2:RunInstances` pode
criar várias instâncias numa única chamada).

Dois níveis de confiança: **extractors dedicados** (`_REGISTRY`, um por
`(eventSource, eventName)` de alta confiança — EC2, S3, Lambda, DynamoDB,
RDS, EKS, Bedrock, SNS, SQS, ECR, ECS, EFS, ElastiCache, KMS, CloudFront,
Route 53, Secrets Manager, Step Functions, ELB) e um **fallback genérico
best-effort** (`_extract_generic`) para qualquer evento mapeado sem
extractor dedicado — procura recursivamente uma chave terminando em "arn"
dentro de `responseElements`; nunca inventa um ARN, devolve lista vazia na
menor incerteza. **Nenhum dos dois foi validado contra um evento CloudTrail
real capturado em sandbox** — ver
[docs/melhorias-futuras.md](melhorias-futuras.md).

### `single_resource.py` (Etapa 3)

`build_resource_from_arn(session, arn, servico, regiao, expected_tag_value,
tipo_recurso)` lê o estado atual de tags de UM ARN já conhecido (via o
evento) e monta o dict exatamente no formato de
`resource_discovery._build_resource` — para que `decision.classify_resource`
(Etapa 2a) possa ser chamado sem nenhuma adaptação. Roteia por
`(servico, tipo_recurso)` entre os 4 caminhos de leitura já usados em
`resource_discovery.py`/`tag_execution.py` (genérico via
`tag:GetResources`, EKS/Bedrock via `ListTagsForResource`, ELB via
`DescribeTags`).

Duplica, de propósito, a mesma chamada de API que
`tag_execution._revalidate_generic`/`_revalidate_eks_or_bedrock`/
`_revalidate_elb` já fazem (para um ARN de cada vez em vez de um lote) —
não extraído um helper compartilhado agora para não mexer em código já
testado/usado em `--live` só por esta reorganização (registrado em
[docs/melhorias-futuras.md](melhorias-futuras.md)).

### `publish.py`

Publicação de um resultado no tópico SNS central — o modelo de "reporte por
push" já descrito em
[arquitetura-multicliente.md](arquitetura-multicliente.md), mas **sem
nenhuma implementação no repositório antes deste módulo** (`report.py` só
grava JSON local; `tag_execution.py` só devolve um dict). Construído como
núcleo compartilhado (não acoplado à Etapa 3) de propósito: a Etapa 4 vai
publicar no mesmo formato depois, no mesmo tópico.

`build_outcome_payload` usa a mesma taxonomia de campos que
`tag_execution.build_execution_report` já usa por recurso
(`categoria_final`/`origem`/`estrategia_api`/`resultado`/`detalhe_erro`/
`motivo`), mais os campos próprios do evento de origem (`etapa`, `event_id`,
`event_name`, `event_source`).

### `handler_continuous_tagging.py` (Etapa 3)

Entrypoint Lambda da Etapa 3 — o equivalente de `main.py` para esta etapa
(só orquestração de I/O: variáveis de ambiente, sessão boto3, publicação;
nenhuma lógica de negócio própria). Invocado pelo Step Functions (depois do
debounce contra corrida com IaC — ver `infra/statemachine/`), não
diretamente pela regra do EventBridge.

Fluxo por recurso extraído do evento: `single_resource.build_resource_from_arn`
(lê o estado atual) -> `decision.classify_resource` (Etapa 2a, chamada
direta) -> `tag_execution.run_tagging_execution(..., dry_run=False)` (Etapa
2c, chamada direta — funciona igual para `taguear` e para qualquer outra
decisão, sem nenhum `if` especial: `build_execution_report` já mescla os
dois casos) -> `publish.publish_outcome`. Uma falha inesperada num recurso
não impede o processamento dos demais recursos do mesmo evento (ex.: um
`RunInstances` em lote) — acumulada e relançada só ao final, para marcar a
execução do Step Functions como falha sem abortar o loop no meio.

`session`/`sns_client` são injetáveis via keyword-only (default: construídos
de verdade) — só para permitir testar o handler de ponta a ponta com
`fake_session_factory`, sem mudar a assinatura `(event, context)` que a
Lambda de fato invoca.

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
  Falhas de descoberta por região/etapa são coletadas e passadas para
  `report.build_report(..., falhas_descoberta=...)` — nunca só logadas.
- `decide` — Etapa 2a. Lê `--input` (saída de `map`), escreve o relatório de
  decisão. Não usa boto3.
- `apply` — Etapas 2b (default) e 2c (`--live`). Lê `--input` (saída de
  `decide`) e reaproveita `valor_tag_esperado` de dentro desse relatório
  para a chamada a `tag_execution.run_tagging_execution` — de propósito,
  **não** aceita um `--expected-tag-value` próprio: aplicar um valor
  diferente do que foi usado para classificar os recursos como `taguear`
  seria inconsistente com a própria decisão que está sendo executada.
  `--no-revalidate` desliga a revalidação; `--live` troca `dry_run=True`
  (default, Etapa 2b) por `dry_run=False` (Etapa 2c — escreve de verdade);
  `--max-decision-age-hours` liga a checagem de idade do relatório de
  decisão (seção `tag_execution.py` acima). `--live` combinado com
  `--no-revalidate` é recusado antes de qualquer chamada AWS (mesma
  garantia reforçada dentro de `tag_execution.py` via
  `RevalidacaoObrigatoriaError`, para quem chamar a função direto sem
  passar pelo CLI).

`--expected-tag-value` (`map`/`decide`) é validado contra
`^pc:[A-Za-z0-9]+$` — erro de verdade, não só aviso, já que o formato está
fechado para este projeto (nunca `ra-...`).

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
