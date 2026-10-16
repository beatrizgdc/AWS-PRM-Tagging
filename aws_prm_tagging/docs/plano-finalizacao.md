# Plano de finalização (handover — 2026-09-30)

Documento de continuidade do projeto após a passagem de bastão. Consolida:
os 15 pontos levantados na revisão do handover, os achados novos da
revisão de código, as dependências entre eles, o que já foi feito (Lote 1)
e o roteiro de validação **sem custo** para a conta sandbox.

Regra da sandbox (atualizada em 2026-09-30): a conta pode ser usada, mas
é compartilhada — **toda ação de criação/alteração é avisada antes**, com o
que cria, o custo aproximado e o comando de remoção, e **executada pelo
responsável do projeto** (o assistente não tem acesso à conta). Nada de
custo exorbitante; recursos com custo por hora ficam no ar o mínimo
possível. O que der para validar localmente (pytest/moto) é validado antes.

## Achados novos (não estavam na lista do handover)

| # | Achado | Gravidade | Status |
|---|---|---|---|
| A1 | **Nenhuma função Lambda era tagueada pela Etapa 3.** O CloudTrail grava o evento como `CreateFunction20150331` (documentado pela AWS); a regra esperava `CreateFunction`. CloudFront tem o mesmo padrão de sufixo de versão, e o console usa `CreateDistributionWithTags`, que não estava mapeado. | Alta | Corrigido (Lote 1) |
| A2 | **Capitalização do CloudTrail em serviços JSON.** Vários serviços gravam `responseElements` em lowerCamelCase (Lambda `functionArn`, DynamoDB `tableDescription.tableArn`, KMS `keyMetadata.arn`); os extractors liam só o PascalCase do botocore. Só ~10 serviços tinham sido validados com evento real. | Alta | Corrigido: leitura tolerante por padrão (tenta o exato primeiro) + fallback pelo identificador do request em Lambda/DynamoDB. **Validar em sandbox (roteiro abaixo).** |
| A3 | **A Etapa 3 exige uma trail do CloudTrail ativa na conta** (documentação da AWS para eventos "AWS API Call via CloudTrail"). Não estava listado como pré-requisito. | Alta (produção) | Documentado. Verificação de pré-requisito no deploy fica para o Lote 2. A regra nativa de EC2 (A4) **não** depende de trail. |
| A4 | Auto Scaling com `MixedInstancesPolicy`/Spot também lança via `CreateFleet` — o gap do item 11 atinge node groups gerenciados do EKS, não só Karpenter. | Alta | Corrigido: regra de evento nativo "EC2 Instance State-change Notification" (`running`) substitui `RunInstances`. |
| A5 | Volume root: a resposta de `RunInstances` normalmente ainda não traz os IDs de volume (instância em `pending`). Ler do evento não é confiável. | Média | Corrigido: `resource_expansion.py` lê `ec2:DescribeInstances` depois do debounce. |
| A6 | DocumentDB/Neptune **já eram tagueados** pela regra do RDS, só com rótulo "RDS". O `requestParameters.engine` do evento desambigua. | Baixa | Corrigido: 16 serviços sem mapeamento → 14. |
| A7 | ElastiCache só tinha `CreateCacheCluster`; Redis/Valkey em produção quase sempre é `CreateReplicationGroup` ou Serverless. | Média | Corrigido. |
| A8 | Teste do `cfn-lint` quebrava com versões novas (achado informativo I4010, não erro). | Baixa | Corrigido: o teste ignora achados `I*`. |
| A9 | `handler` criava o client SNS sem região — quebraria no split hub/regional (tópico em outra região). | Média (bloqueia item 7) | Corrigido: client na região do ARN do tópico. |
| A10 | NAT Gateway (cobrado como EC2) caía no rótulo "AWS Transit Gateway". | Baixa (só relatório) | Corrigido. |
| A11 | O roteiro de sandbox anterior criou recursos com custo (RDS, Redshift, Route 53). Conflita com a restrição atual. | — | Novo roteiro sem custo abaixo. |
| A12 | Indício de que o AWS Load Balancer Controller remove, na reconciliação, tags aplicadas fora dele (existe a flag `--external-managed-tags` justamente para isso). Tag via API em ALB/NLB de EKS pode não ser durável. | Média | A confirmar — exige EKS real (custo). Afeta o desenho do item 12. |
| A13 | Os patterns do EventBridge eram colados à mão no template. | Baixa | Automatizado: `python3 -m aws_prm_tagging.event_mapping` agora também sincroniza os blocos `EventPattern` do `template.yaml`. |
| A14 | Nenhum teste verificava se um evento REAL casa com as regras (foi essa lacuna que escondeu A1). | Média | Novo `test_infra_event_patterns_semantica.py` (matcher de patterns do moto, offline). |

## Os 15 pontos do handover — validação e status

| Item | Validação | Status |
|---|---|---|
| 1. Descoberta de recursos sem tag | Confirmado: `GetResources` não devolve recurso que nunca teve tag. A 2c e a Etapa 3 já tratam ARN desconhecido corretamente (tratam como "sem tag") — o gap é só de **descoberta**. | **Resolvido (Lote 2):** `native_discovery.py` — ~50 enumeradores somente leitura, unidos ao `GetResources`; comparação de ARN ignora o campo de conta (divergência do snapshot EBS achada no teste). Validado com moto (consistência de ARN + CLI ponta a ponta). **Falta: sandbox, Parte A.** |
| 2. Etapa 2a | Revisada, testes passando. | Ok |
| 3. Etapa 2b | Revisada, testes passando. | Ok |
| 4. Etapa 2c | Falta: permissões IAM nativas por serviço, condição `aws:TagKeys`, códigos de erro "não encontrado" do ELB. | **Roteiro pronto (Lote 2):** `test/manual-live-lote2/validar_etapa2c.sh` — ensaiado de ponta a ponta contra moto server. Escrita restrita por `--somente-arns` (conta compartilhada). `aws:TagKeys` validado implicitamente na Parte B. Códigos de erro do ELB seguem pendentes (ALB tem custo por hora — só com autorização). |
| 5. Etapa 3 parcial | Coberto pelos achados A1–A7. Multi-região depende do item 7. | Lote 1 feito; multi-região no Lote 2. |
| 6. Etapa 4 | Escopo do desenho original (docs/arquitetura-multicliente.md e melhorias-futuras.md), validado internamente em 2026-09-30. | **Resolvido (Lote 4):** varredura recorrente (Scheduler → mesma state machine do item 8, `origem=varredura_recorrente`, publica só com novidade; lê DLQ e detecta região sem stack regional; flag `contrato_confirmado`); drift quase em tempo real (stack regional, só alerta, relê o estado antes); `auditar-org` (SCP + tag policies, conta de gerenciamento); `verificar-csv` (tabela oficial × CSV × mapeamento). |
| 7. Split hub/regional | Pré-requisito A9 resolvido. Falta: templates hub e regional, `us-east-1` sempre incluída, trusted access do StackSets. | **Resolvido (Lote 2):** `template-hub.yaml` + `template-regional.yaml`; `preflight` gera a lista de regiões (sempre com `us-east-1`). **Falta: sandbox, Parte B.** Trusted access do StackSets continua pré-requisito por cliente. |
| 8. Custom Resource assíncrono | Depende de 1, 4 e 7. Desenho: o Custom Resource responde SUCCESS na hora e dispara um Step Functions com `Map` por região (uma Lambda por região, cada uma bem abaixo dos 15 min). | **Resolvido (Lote 3):** stack hub com Custom Resource → Step Functions (`Map` por região, concorrência 4) → Lambda por região (`pipeline.executar_regiao`); falha de uma região é publicada no SNS e não interrompe as outras. Modo `dry-run` (default) / `live` / `desligado`; reexecução mudando `ReexecutarTagueamentoInicial`. **Na sandbox compartilhada, só dry-run** (live alcançaria recursos de terceiros). |
| 9. SQS e DLQ | Buffer SQS: **não necessário por ora** (o Step Functions já absorve rajadas). DLQ: **recomendado** — barato (SQS free tier) e hoje um evento que esgota as tentativas some sem rastro. A regra nativa de EC2 aumenta o volume de eventos, o que reforça a DLQ. | **Resolvido (Lote 2):** DLQ por região, recebendo falha de entrega do EventBridge **e** falha de processamento (Catch no Step Functions). Buffer SQS continua fora, por decisão. |
| 10. Serviços sem mapeamento | 16 → 14 (A6) → 15 com o CSV novo (WorkSpaces Core Managed Instances entrou sem mapeamento: ARN não confirmado). Proposta por serviço enviada em 2026-09-30 — **aguardando aprovação**; QuickSight, IoT Core/SiteWise (e SES, ver item 15) dependem de pergunta à AWS sobre se a tag gera atribuição. CodeStar: fica sem mapeamento de propósito (descontinuado em 31/07/2024, sem criação possível e sem custo próprio — documentado em `event_mapping.py`). Restantes: SageMaker, QuickSight, Glue, Bedrock AgentCore, IoT, AppStream, DataSync, Deadline, Omics, Security Hub, DRS, Comprehend, IoT SiteWise, CodeStar (descontinuado). | **Resolvido (Lotes 3 e 4).** 90 de 92 códigos com reação em tempo real. Deadline Cloud e WorkSpaces Core Managed Instances mapeados em 2026-09-30 com o formato de ARN da Service Authorization Reference da AWS. Sem mapeamento por evento, por decisão: DRS (source server não tem API de criação — varredura + recomendação de `stagingAreaTags`) e CodeStar (descontinuado). SES: a cobrança é por e-mail; o guia confirma que o SES está na lista do método **User Agent String** (nota: exclui managed infrastructure hours) — o tagueamento de identidades/configuration sets fica como complemento. |
| 11. CreateFleet/restores/volumes root | CreateFleet/Karpenter/ASG misto via evento nativo (A4); RDS restores/réplica/snapshots; DynamoDB restores; EC2 snapshots/NAT/EIP; volumes root via expansão (A5). | Lote 1 feito. Validação real de EC2 só se a sandbox estiver em free tier. |
| 12. Caminhos do guia para EKS | Os caminhos do guia são configuração na infraestrutura do cliente (launch template, anotação de Ingress/Service, `--extra-tags` do CSI, e `tags` no `EC2NodeClass` do Karpenter). Alterar launch template automaticamente faz rolling dos nodes e gera drift de IaC. | **Resolvido (Lote 3):** subcomando `recomendar` (somente leitura) gera, por cluster, o artefato pronto para o IaC: TagSpecifications do launch template, `spec.tags` do EC2NodeClass (Karpenter), `defaultTags` do Load Balancer Controller, `extraVolumeTags` do EBS CSI. Saída JSON + Markdown para o cliente. |
| 13. Nodes EKS como "Amazon EC2" | Etapa 3: nodes via `RunInstances` eram rotulados "Amazon EC2"; nodes via `CreateFleet` (Karpenter, node group com Spot/vários tipos) **não eram pegos**. Etapa 1 rotulava como "Amazon EKS". | Lote 1 feito: evento nativo + campo `servico_faturamento` (e `cluster_eks`) no relatório da Etapa 1. A atribuição nunca foi afetada (a tag é a mesma). |
| 14. Tasks ECS | No Fargate o custo fica na **task**. Tag no service não chega à task sem `propagateTags` (SERVICE ou TASK_DEFINITION). Hoje o código tagueia só cluster/service → gasto de Fargate provavelmente não é atribuído. | **Resolvido (Lote 3):** regra nativa "ECS Task State Change" (RUNNING, Fargate) tagueia a task via `ecs:TagResource`, **sem debounce**; services e tasks entram na descoberta nativa; `recomendar` aponta services sem `propagateTags`. Parâmetro `PublicarResultadoJaOk=false` evita e-mail por task já conforme. |
| 15. CSV × PDF | O zip oficial dá 404 (confirmado também pelo navegador). CSV regenerado a partir da tabela da página: **94 linhas, 8 serviços novos** — ver `reference/FONTE-CSV.md`. | **Resolvido (2026-09-30).** Mapeados na Etapa 3: DAX, Connect Decisions, End User Messaging, GameLift Streams, RTB Fabric, SES. EVS coberto pelo EC2. WorkSpaces Core Managed Instances sem mapeamento por evento até o formato de ARN ser confirmado. |

### Item 15 — diferenças CSV × guia (histórico — resolvido, ver `reference/FONTE-CSV.md`)

A análise abaixo foi feita com a página de 90 linhas; a lista final usada
(94 linhas) inclui ainda Amazon Connect Decisions, Amazon GameLift
Streams, AWS RTB Fabric e Amazon SES, todos com atribuição a partir de
1º/09/2026.

Faltam no CSV:

| Serviço | Product Service Code | Nota no guia |
|---|---|---|
| Amazon DynamoDB Accelerator (DAX) | AmazonDAX | None |
| Amazon Elastic VMware Service (Amazon EVS) | AmazonEC2 | Só o EC2 subjacente |
| AWS End User Messaging | AmazonPinpoint | Exclui taxas de operadora, números etc. |
| Amazon WorkSpaces Core Managed Instances | AmazonWorkSpacesInstances | Exclui medição horária e licenças de terceiros |

Notas divergentes:

| Serviço | CSV | Guia |
|---|---|---|
| Amazon Bedrock | "Excludes spend from AWS Marketplace Private Offers" | "Includes only Amazon models and Open Source models" |
| Amazon DynamoDB | "Excludes DAX" | None (DAX virou linha própria) |
| Amazon EC2 | sem "Savings Plan for ML instance types" | inclui |

Impacto no código ao atualizar o CSV: `services.py` já mapeia `dax`,
`mobiletargeting`, `sms-voice` e `workspaces-instances` (inertes hoje).
`event_mapping.py` vai exigir uma entrada (mapeada ou `None`) para cada
código novo — o teste `test_toda_entrada_do_csv_tem_alguma_entrada_na_tabela`
aponta exatamente quais. A nota do Bedrock importa para o negócio: se o
guia estiver certo, a tag só gera atribuição quando as invocações usam
modelos Amazon/open source.

## Decisões registradas (2026-09-30)

- **Sandbox**: pode ser usada normalmente. Toda ação de criação/alteração
  nos roteiros vem com: o que cria, custo aproximado e comando de remoção.
  A execução na conta é sempre feita pelo responsável do projeto (sem
  acesso direto do assistente).
- **DLQ**: entra junto com o split hub/regional (uma por região).
- **Item 12**: configuração recomendada por cluster.
- **Item 14**: só `ecs:TagResource`; `propagateTags` como recomendação.
- **Item 15**: CSV regenerado a partir da página oficial.

## O que mudou no Lote 2 (2026-09-30)

- `native_discovery.py` (novo, item 1) e integração no `map`
  (`--sem-descoberta-nativa` desliga); `origem_descoberta` e
  `total_descobertos_so_via_api_nativa` no relatório.
- `preflight.py` + subcomando `preflight` (achado A3 e passo 2 do item 7).
- `map --regioes` e `apply --somente-arns` (escopo em conta compartilhada
  e rollout gradual).
- `infra/template.yaml` → `infra/template-regional.yaml` (sem SNS, com
  DLQ) + `infra/template-hub.yaml`; state machine com `Catch` → DLQ.
- `docs/producao.md`: política de leitura gerada a partir do código (65
  ações), com teste de deriva.
- Roteiro `test/manual-live-lote2/` (script, verificador, limpeza, README
  com o que cria/custo/remoção), ensaiado contra moto server.
- Testes: 350 → 368.

Achado do Lote 2: um enumerador que levantasse exceção fora de
`ClientError` (ex.: `UnknownServiceError` num botocore antigo) derrubava a
descoberta nativa inteira da região — isolado por enumerador.

## O que mudou no Lote 3 (2026-09-30)

- Item 10: 11 serviços mapeados na Etapa 3 (extractors conferidos contra o
  modelo do SDK); 6 regras de CloudTrail (era 5); permissões de escrita
  correspondentes.
- Item 14: regra nativa ECS, `Choice` na state machine (task ECS não
  espera debounce), `PUBLICAR_JA_OK`, services/tasks na descoberta nativa.
- Itens 12/14/DRS/IoT: `recomendacoes.py` + subcomando `recomendar`.
- Item 8: `pipeline.py` (loop da Etapa 1 extraído do CLI — mesma função
  para CLI e Lambda), `handler_initial_tagging.py`, state machine
  `tagueamento_inicial.asl.json`, `template-hub.yaml` agora SAM (3 Lambdas
  + Custom Resource); guardas de deriva de IAM entre hub e regional.
- Roteiro `test/manual-live-lote3/README.md`.
- Testes: 368 → 436.

Achados do Lote 3: (1) a extração do loop para `pipeline.py` deixou uma
variável órfã no log final do `map` (NameError só no fim da execução) —
pego pelo pyflakes + 2 testes; (2) o teste antigo da state machine não
aceitava estados `Choice` (que pela ASL não têm `Next`/`End`) — corrigido.

## O que mudou no Lote 4 (2026-09-30)

- Correção vinda da sandbox: `recomendar` tratava
  `UninitializedAccountException` do DRS (conta que nunca habilitou o
  serviço) como falha — agora é "serviço não usado", sem registro.
- Varredura recorrente: `handler_initial_tagging` ganhou `origem`, regra de
  novidade, `_saude_regional` (DLQ + presença da stack regional) e
  `contrato_confirmado`; hub com `AWS::Scheduler::Schedule` condicional e
  4 parâmetros novos; DLQ regional com nome fixo `prm-etapa3-dlq`.
- Drift: `handler_drift.py`, `event_mapping.DRIFT_EVENT_PATTERN` (prefixo
  `Untag`/`RemoveTags` cobre nomes versionados), função + regra + permissão
  no template regional, DLQ.
- `org_audit.py` + `auditar-org`; `csv_check.py` + `verificar-csv`.
- Roteiro `test/manual-live-lote4/README.md`.
- Testes: 436 → 485.

Decisão de desenho registrada: o drift em tempo real **só alerta**;
reaplicar fica com a varredura em modo `live`, para não brigar com uma
remoção intencional sem revisão humana.

## Dependências e ordem

```
Lote 1 (bugs, sem dependência) ──────────────────────────────── FEITO
Item 15 (decisão CSV) ──► ajustes de mapeamento dos códigos novos
Item 1 (descoberta sem tag) ──► Item 8 ──► Item 6
Item 4 (2c validada) ──► Item 8 (tagueamento inicial roda em live)
Item 7 (split) ──► Item 9 (DLQ por região) ──► Item 8 ──► Item 6
Itens 12, 14, 10 ──► dependem de decisão de produto
```

- **Lote 2**: itens 1, 7, 9, verificação de trail (A3) e validação do 4 — **feito**; falta rodar na sandbox (`test/manual-live-lote2/README.md`).
- **Lote 3**: item 8, e itens 12/14/10 conforme as decisões — **feito**; falta rodar na sandbox (`test/manual-live-lote3/README.md`).
- **Lote 4**: item 6 (Etapa 4) — **feito**; falta rodar na sandbox (`test/manual-live-lote4/README.md`).

## O que mudou no Lote 1 (arquivos)

- `event_parser.py`: leitura tolerante a capitalização por padrão;
  `normalize_event_name` (sufixo de versão); extractors novos (EC2
  CreateFleet/snapshots/NAT/EIP, RDS restores/réplica/snapshots, DynamoDB
  restores, ElastiCache replication group/serverless, CloudFront WithTags);
  Lambda/DynamoDB com fallback pelo request; `product_code_resolver` para
  DocumentDB/Neptune; suporte ao evento nativo de EC2.
- `event_mapping.py`: regras novas; `NATIVE_EVENT_PATTERNS`;
  `RunInstances` removido do CloudTrail; sincronização automática do
  template.
- `resource_expansion.py` (novo): instância → volumes EBS.
- `handler_continuous_tagging.py`: expansão de volumes; client SNS na
  região do tópico; campos de evento para eventos nativos.
- `resource_discovery.py`/`decision.py`/`report.py`: `servico_faturamento`
  e `cluster_eks` (item 13).
- `services.py`: NAT Gateway como EC2; namespaces do item 15 (inertes).
- `infra/template.yaml` (hoje `template-regional.yaml`, ver Lote 2): regra `Etapa3NativeEc2InstanceRunningRule`;
  `ec2:DescribeInstances` na role; patterns regenerados.
- Testes: `test_lote1_cobertura_etapa3.py`,
  `test_infra_event_patterns_semantica.py`, ajustes em testes existentes.
  Suíte: 246 → 308 testes passando. `moto` entrou em
  `requirements-dev.txt`.

Nenhuma permissão de ESCRITA nova foi necessária: snapshots/NAT/EIP usam
`ec2:CreateTags`, restores/snapshots de RDS usam `rds:AddTagsToResource`,
replication group usa `elasticache:AddTagsToResource` — todas já na role.

## Roteiro de validação na sandbox — Lote 1 (só recursos gratuitos)

Rodar do diretório pai de `aws_prm_tagging/`. **Só conta sandbox.** Cada
recurso criado é apagado no mesmo passo.

### 0. Pré-requisito: existe trail ativa? (A3)

```bash
aws cloudtrail describe-trails --profile <sandbox> --query 'trailList[].[Name,HomeRegion,IsMultiRegionTrail]'
aws cloudtrail get-trail-status --name <nome> --profile <sandbox> --query IsLogging
```

Se não houver trail ativa, **não criar uma sem autorização**: o primeiro
registro de eventos de gerenciamento é gratuito, mas o armazenamento dos
logs no S3 não é custo zero. Nesse caso, pule os passos 2–3 da Etapa 3 e
valide só com `lookup-events` (passo 1, que lê o Event History gratuito de
90 dias e não depende de trail).

### 1. Capturar eventos reais (sem implantar nada) — valida A1/A2

Recursos gratuitos, criados e apagados em seguida:

| Serviço | Criar | EventName esperado | Apagar |
|---|---|---|---|
| Lambda | `aws lambda create-function` com zip mínimo e role existente | `CreateFunction20150331` | `aws lambda delete-function` |
| DynamoDB | `aws dynamodb create-table ... --billing-mode PAY_PER_REQUEST` | `CreateTable` | `aws dynamodb delete-table` |
| SQS | `aws sqs create-queue --queue-name prm-teste` | `CreateQueue` | `aws sqs delete-queue` |
| CloudWatch Logs | `aws logs create-log-group --log-group-name prm-teste` | `CreateLogGroup` | `aws logs delete-log-group` |
| ECR | `aws ecr create-repository --repository-name prm-teste` | `CreateRepository` | `aws ecr delete-repository` |
| Step Functions | `aws stepfunctions create-state-machine` (Pass state) | `CreateStateMachine` | `aws stepfunctions delete-state-machine` |

Depois de ~1 min:

```bash
aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventSource,AttributeValue=lambda.amazonaws.com \
  --max-results 5 --profile <sandbox> --output json > /tmp/evento_lambda.json
```

Para cada evento capturado, rodar o parser contra o evento real:

```bash
python3 - <<'PY'
import json
from aws_prm_tagging import event_parser
d = json.load(open("/tmp/evento_lambda.json"))
for e in d["Events"]:
    ct = json.loads(e["CloudTrailEvent"])
    evento = {"detail-type": "AWS API Call via CloudTrail", "account": ct["recipientAccountId"],
              "region": ct["awsRegion"], "detail": ct}
    print(ct["eventName"], "->", event_parser.parse_creation_event(evento))
PY
```

Critério: cada evento de criação devolve exatamente o ARN do recurso
criado. Qualquer lista vazia é um bug de extractor — guardar o JSON do
evento como fixture de regressão.

### 2. Validar os patterns na AWS real (gratuito)

`aws events test-event-pattern` é uma chamada gratuita que avalia o pattern
no próprio EventBridge (confirma o que o teste com moto já cobre
localmente):

```bash
aws events test-event-pattern --profile <sandbox> \
  --event-pattern file://aws_prm_tagging/infra/event_pattern.2.generated.json \
  --event '{"id":"1","account":"000000000000","source":"aws.lambda","time":"2026-09-30T00:00:00Z","region":"us-east-1","resources":[],"detail-type":"AWS API Call via CloudTrail","detail":{"eventSource":"lambda.amazonaws.com","eventName":"CreateFunction20150331"}}'
```

(Descobrir em qual arquivo está a fonte `aws.lambda` com
`grep -l aws.lambda aws_prm_tagging/infra/event_pattern.*.json`.)

### 3. Smoke test de ponta a ponta (stack implantada)

Mesmo roteiro da Parte 2 de `test/manual-live-etapa3/README.md`, com
`DebounceSeconds=60` para não esperar 30 min. Lambda, Step Functions
(4.000 transições/mês gratuitas; ~3 por evento), EventBridge, IAM e SNS
ficam dentro do free tier. Criar, em sequência, uma função Lambda, uma
tabela DynamoDB on-demand e uma fila SQS; confirmar a tag em cada uma e a
notificação no SNS; apagar recursos e stack.

**Não incluir** criação de instância EC2, a menos que a conta sandbox
esteja comprovadamente em free tier (t2.micro/t3.micro + 8 GB de EBS).
Sem isso, a regra nativa de EC2 e a expansão de volumes ficam validadas só
pelos testes unitários e pelo passo 2.


## Ajustes antes do teste consolidado na sandbox (2026-09-30)

- **`FiltroEscopoTeste`** (stack regional, `escopo_teste.py`): a Etapa 3
  não tem dry-run; numa conta compartilhada ela taguearia recursos de
  outras pessoas enquanto a stack estivesse no ar. O filtro limita Etapa 3
  e drift aos eventos de um principal e/ou a ARNs com um prefixo. Vazio em
  produção.
- **Bug corrigido na state machine da Etapa 3**: o `Choice` do Lote 3 lia
  `$.detail-type` sem `IsPresent` — evento sem esse campo quebrava antes da
  Lambda, fora do `Catch`, e não ia para a DLQ.
- Guia único: `test/GUIA-SANDBOX.md` (ordem, o que escreve tag, inventário
  por região e limpeza — inclusive os log groups das Lambdas, que o
  `sam delete` não remove).

## Rodada de sandbox de 2026-10-02

- Validado na AWS real: `verificar-csv` (página oficial), `preflight`,
  `recomendar`, Etapa 1 com descoberta nativa (0 falhas de descoberta em
  us-east-1, 197 recursos nunca tagueados encontrados) + Etapa 2c (SQS e
  ECR confirmados; DynamoDB pendente — provável consistência eventual na
  leitura de conferência), deploy das stacks hub e regional, Custom
  Resource. Nenhuma tag escrita fora do escopo; limpeza sem resíduos.
- Correções derivadas: verificador com espera de propagação; `.gitignore`
  da raiz; descoberta de S3 quando `us-east-1` não está no escopo
  (`discover_s3_buckets_por_regiao` — com `--regioes us-east-2` o bucket
  sem tag de Ohio ficava invisível).
- Próxima rodada: `test/GUIA-SANDBOX.md`, com todas as escritas em
  us-east-2.

## Pendências fora do código (2026-09-30)

| Pendência | O que falta | Bloqueia? |
|---|---|---|
| Rodar os roteiros de sandbox dos Lotes 2, 3 e 4 | Execução pelo responsável do projeto | Sim, para considerar validado |
| Centralização de métricas e eventos (Zabbix corporativo em validação; plano B a definir) | Decisão do time sobre o destino central | Não para o tagueamento nem para os testes; sim para o dashboard |
| Trusted access do StackSets por cliente | Passo operacional na payer de cada cliente — a Darede tem acesso administrativo e fará | Sim, para o rollout via StackSets |
| Mapeamento OU → contrato (product code) | Informação de negócio (já registrada como "em aberto" em arquitetura-multicliente.md) | Sim, para o rollout |


## Validação na sandbox real — 2026-10-02 (conta sandbox da Darede, us-east-2)

| Teste | Resultado |
|---|---|
| Etapa 1 + 2c (script, 10 cenários) | 10/10 OK — inclui SQS, ECR e DynamoDB (com espera de propagação) |
| D1 Lambda (`CreateFunction20150331`) | tagueado + e-mail — **bug A1 confirmado corrigido** |
| D2 DynamoDB (camelCase) | tagueado + e-mail |
| D3 IoT billing group (item 10) | tagueado + e-mail |
| D4 drift | e-mail `drift_tag_removida` com o principal; tag não reaplicada |
| D5 DLQ | execução `FAILED`, 1 mensagem na DLQ |
| E varredura recorrente | `SUCCEEDED`; resumos por região |

Achados corrigidos a partir dos e-mails da varredura (Lote 5):

1. **S3 na Lambda**: o boto3 do runtime do Lambda não traz `BucketRegion`
   no `ListBuckets`; o fallback `GetBucketLocation` não estava na role e
   um único bucket negado descartava todos os buckets da região. Agora:
   permissão declarada, falha isolada por bucket, e cada região trata só
   os próprios buckets (antes us-east-1 tratava os de todas as regiões).
2. **EKS `falhou` no hub**: a role do hub não tinha
   `eks:ListTagsForResource` (releitura antes de escrever). Hub agora tem
   as mesmas leituras de revalidação da Etapa 3, com teste de deriva.
3. **Regiões bloqueadas por SCP**: geravam 4 falhas e 1 e-mail por região
   a cada varredura (13 na sandbox). Agora: uma entrada
   `regiao_negada_por_scp`, nada mais é tentado ali, e não conta como
   novidade.

Testes: 496 → 505. Pendente (opcional): redeploy só do hub + uma
varredura para ver os 3 ajustes na AWS real.

## MVP 1 — 2026-10-02

Fechamento: os 15 pontos implementados (Zabbix fora, por decisão do time).
Entregue com `deploy/implantar_conta.sh` e `deploy/remover_conta.sh`
(testados contra AWS simulada: ordem das chamadas, parâmetros, falha isolada
por região, proteção de log groups de terceiros), varredura final do
código (lint, templates, dados da sandbox anonimizados, documentos
desatualizados corrigidos) e o documento principal `docs/MVP-1.md`.
Pendências para o MVP 2 estão na seção "Limitações conhecidas e
pendências" daquele documento.
