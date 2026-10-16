# Infraestrutura da Etapa 3 (automação contínua)

Dois templates (split do item 7 do plano, implementado em 2026-09-30):

| Template | Onde | Conteúdo |
|---|---|---|
| `template-hub.yaml` (SAM) | **1 vez por conta**, numa região (a "região hub") | Tópico SNS de relatório (`TopicName`, default `prm-compliance`) + assinatura de e-mail + **tagueamento inicial** (item 8: Custom Resource → Step Functions → uma Lambda por região) |
| `template-regional.yaml` (SAM) | **Toda região ativa + `us-east-1` sempre** | Lambda, Step Functions (debounce), 6 regras de EventBridge, IAM, DLQ (SQS) |

Por que `us-east-1` sempre: é a única região onde o EventBridge recebe
eventos de serviços globais (Route 53, CloudFront) — confirmado em sandbox.
Por que o tópico fica fora da stack regional: é peça única por conta
(Etapa 3, tagueamento inicial e Etapa 4 publicam no mesmo lugar); a stack regional
publica nele pelo ARN montado a partir de `HubRegion` + `HubTopicName`,
sem `Fn::ImportValue` cross-region.

**Antes de implantar numa conta**, rode o preflight (somente leitura):

```bash
python3 -m aws_prm_tagging.main preflight --profile <perfil> --output prm_preflight.json
```

Ele devolve a lista de regiões para o StackSet regional e avisa se falta
uma trail do CloudTrail registrando eventos de escrita (sem ela, as regras
"AWS API Call via CloudTrail" não recebem eventos — só a regra nativa de
EC2 funciona).

## Rollout via StackSets (ordem)

1. StackSet **hub** (`template-hub.yaml`), 1 região por conta.
2. `sam build --template-file aws_prm_tagging/infra/template-regional.yaml`
   + `sam package` para gerar o template com o código no S3 (StackSet não
   aceita artefato local), e StackSet **regional** com as regiões do
   preflight e os parâmetros `ExpectedTagValue` (override por sub-OU),
   `HubRegion` e `HubTopicName`.
3. Trusted access do StackSets com Organizations na payer do cliente
   (pré-requisito, ver docs/arquitetura-multicliente.md).

Roteiro de validação na sandbox (deploy manual com `sam deploy` em 2
regiões, sem StackSet): `test/manual-live-lote2/README.md`, Parte B.

## Tagueamento inicial (item 8)

No `Create` da stack hub, o Custom Resource inicia a state machine
`TagueamentoInicialStateMachine` e responde ao CloudFormation na hora (o
tagueamento pode durar bem mais que a 1h de um Custom Resource). A state
machine lista as regiões ativas e roda, em paralelo (até 4), uma Lambda
por região com a Etapa 1 → 2a → 2b/2c completa (`pipeline.executar_regiao`
— a mesma função do CLI). Cada região publica um resumo no tópico; falha
de uma região (ex.: estouro dos 15 min) é publicada e não interrompe as
outras.

- `ModoTagueamentoInicial`: `dry-run` (default), `live`, `desligado`.
- Rodar de novo: mudar `ReexecutarTagueamentoInicial`. Trocar só o modo
  **não** dispara execução (evita escrita acidental num update).
- `Delete` da stack nunca remove tags.
- IAM da Lambda: mesma leitura documentada em `docs/producao.md` e mesma
  escrita da Etapa 3 (com `aws:TagKeys`) — `test_tagueamento_inicial.py`
  trava divergência entre os dois templates.

## Etapa 4 — varredura recorrente e drift

- **Varredura recorrente (hub)**: `VarreduraRecorrenteSchedule`
  (EventBridge Scheduler, `AgendaVarredura`, default `rate(1 day)`) inicia
  a MESMA state machine do tagueamento inicial com
  `origem=varredura_recorrente`. `ModoVarreduraRecorrente`: `dry-run`
  (default), `live` (reaplica a tag onde sumiu) ou `desligado` (sem
  agendamento). Cada região só publica quando há novidade (algo a
  taguear, conflito, tag similar, falha, mensagem na DLQ, região ativa
  sem stack regional, `ContratoConfirmado=false`);
  `PublicarVarreduraSemNovidade=true` publica sempre (heartbeat).
- **Drift quase em tempo real (regional)**: `Etapa4DriftRule` →
  `Etapa4DriftFunction`. Pattern em `event_mapping.DRIFT_EVENT_PATTERN`
  (`Untag*`, `RemoveTags*`, `DeleteTags`, `Put/DeleteBucketTagging`,
  `ChangeTagsForResource`, `UpdateTagsForResource`). Só alerta
  (`drift_tag_removida`/`drift_tag_alterada`, com o principal que fez a
  chamada), depois de reler o estado atual do recurso; não reaplica.
- **DLQ com nome fixo** `prm-etapa3-dlq`: é por ela que a varredura lê a
  profundidade da fila e detecta região sem stack regional.

## DLQ (item 9)

Cada região tem uma fila `Etapa3EventsDlq` (retenção de 14 dias) que
recebe: (1) eventos que o EventBridge não conseguiu entregar ao Step
Functions depois do `RetryPolicy` (`DeadLetterConfig` em todo alvo), e (2)
execuções cuja Lambda falhou mesmo após o `Retry` (estado `Catch` →
`EnviarParaDlq` → `FalhaRegistradaNaDlq`). A política da fila autoriza
exatamente as regras da stack — `test_infra_event_patterns.py` trava
qualquer regra nova sem entrada na política. Leitura periódica da DLQ fica
para a Etapa 4 (um alarme do CloudWatch não pode notificar um SNS de outra
região).

## Por que várias regras de EventBridge, não uma só

O event pattern de todos os serviços mapeados juntos ultrapassa a quota
**padrão** de 2.048 caracteres por pattern (`Event pattern size`, ver
[quotas do EventBridge](https://docs.aws.amazon.com/general/latest/gr/ev.html)
— ajustável, mas decidimos não depender de um aumento de quota que
precisaria ser solicitado e replicado em cada uma das ~80-90 contas de
cliente via StackSet). `event_mapping.build_event_patterns()` divide as
alternativas em N patterns, cada um dentro do limite — hoje **5** regras
(`Etapa3EventRule0` a `4`; subiu de 3 para 5 quando cada alternativa passou
a exigir `errorCode` ausente — ver "Como regenerar os patterns" abaixo).

## Chamadas que falharam não disparam a automação

Cada alternativa do event pattern exige `detail.errorCode` ausente
(`{"exists": false}`) — uma chamada de API que FALHOU (ex.: `CreateBucket`
negado por `AccessDenied`, ou nome já em uso) ainda grava um evento no
CloudTrail com o mesmo `eventName`, mas o recurso nunca chegou a existir.
Sem esse filtro, a Etapa 3 gastaria uma invocação Lambda tentando ler/
taguear um ARN que não existe, terminando em `erro_leitura_inicial` sempre
— trabalho e ruído evitáveis. Corrigido depois de um code review apontar
que os patterns originais não filtravam isso.

## Como regenerar os patterns

Sempre que `aws_prm_tagging/event_mapping.py` mudar (serviço novo mapeado,
evento adicionado/removido):

```bash
python3 -m aws_prm_tagging.event_mapping
```

(rodar do diretório pai de `aws_prm_tagging/` — mesma exigência do resto do
projeto, ver ["Onde rodar os comandos"](../README.md#onde-rodar-os-comandos)).
Isso regrava `infra/event_pattern.<N>.generated.json`.

**Atualizado (2026-09-30):** o mesmo comando agora também **sincroniza
automaticamente** os blocos `EventPattern` das regras `Etapa3EventRule<N>`
já existentes em `template-regional.yaml` (`event_mapping.sync_template_event_patterns`).
Só continua manual adicionar/remover um recurso `AWS::Events::Rule` quando
o NÚMERO de patterns mudar.

## Regra de evento nativo do EC2

`Etapa3NativeEc2InstanceRunningRule` escuta "EC2 Instance State-change
Notification" (`running`) — não é um evento de CloudTrail e **não depende
de trail**. Substitui o antigo `RunInstances` e cobre instâncias de
qualquer origem (CreateFleet/Karpenter, Auto Scaling com Spot/tipos
mistos, node groups do EKS). O handler expande cada instância para os
volumes EBS anexados (`resource_expansion.py`, via `ec2:DescribeInstances`).
Pattern em `event_mapping.NATIVE_EVENT_PATTERNS`, com guarda de deriva em
`test/unit/test_infra_event_patterns.py`.

**Pré-requisito de produção:** as demais regras (CloudTrail) só recebem
eventos se a conta tiver uma trail ativa registrando eventos de
gerenciamento — ver `docs/plano-finalizacao.md`, achado A3.

**Contexto histórico**: o CloudFormation não tem como incluir um
JSON externo dentro de `AWS::Events::Rule.EventPattern` (ao contrário de
`AWS::Serverless::StateMachine`, que usa `DefinitionUri` para o Step
Functions). Os blocos `EventPattern` em `template-regional.yaml` precisam ser colados
manualmente a partir dos arquivos gerados. Dois testes existem justamente
para nunca deixar isso divergir em silêncio:

- `test/unit/test_event_mapping.py::test_arquivos_gerados_em_infra_nao_estao_desatualizados`
  — os arquivos gerados batem com `event_mapping.py` agora.
- `test/unit/test_infra_event_patterns.py` — os blocos `EventPattern` de
  `template-regional.yaml` batem com os arquivos gerados.

Se o número de arquivos gerados mudar (uma nova regra é necessária),
adicione/remova o recurso `AWS::Events::Rule` correspondente em
`template-regional.yaml` manualmente (copiando o `EventPattern` de
`event_pattern.<N>.generated.json`) — os dois testes acima apontam
exatamente quando isso é necessário.

## E-mail de notificação (SNS)

O tópico `PrmComplianceTopic` sozinho não é suficiente — sem nenhuma
assinatura, todo resultado publicado (sucesso ou falha de tagueamento)
simplesmente desaparece. Isso foi confirmado na prática: os 2 bugs de
permissão IAM encontrados no smoke test em sandbox (ver "Permissões IAM"
abaixo) não geravam exceção nem apareciam no CloudWatch Logs — só foram
achados inspecionando o CloudTrail diretamente, porque o tópico não tinha
ninguém ouvindo.

Por isso o template já cria uma assinatura de e-mail automaticamente
(`PrmComplianceEmailSubscription`), usando o parâmetro `NotificationEmail`.

**O default é `teste@exemplo.com` — um placeholder, não um e-mail real.**
Com o default, a assinatura fica presa em `PendingConfirmation` para
sempre (ninguém confirma um e-mail que não existe) — o comportamento é
"nenhuma notificação chega", o mesmo de não ter assinatura nenhuma, só que
visível no console (a assinatura aparece, mas nunca confirmada) em vez de
inexistente.

**Trocar o e-mail padrão é UM ÚNICO lugar**: o valor de `Default` do
parâmetro `NotificationEmail` em `infra/template-hub.yaml` (seção
`Parameters`). Duas formas de usar isso:

- **Trocar o default permanentemente** (todo deploy que não passar
  `--parameter-overrides` explícito usa o novo valor): editar essa única
  linha em `template-regional.yaml` e commitar.
- **Sobrescrever só num deploy específico**, sem tocar no template (ex.:
  um e-mail diferente por cliente/ambiente):
  ```bash
  sam deploy --parameter-overrides NotificationEmail=seu-email@dominio.com ...
  ```

**Depois de qualquer uma das duas formas**, a AWS manda um e-mail de
confirmação para o endereço configurado — alguém precisa clicar em
"Confirm subscription" nele antes das notificações começarem a chegar de
verdade. Isso é uma exigência do próprio protocolo `email` do SNS
(proteção contra assinar alguém sem consentimento); nenhum parâmetro de
CloudFormation pula esse passo manual.

## Decisões registradas (valores conservadores, ajustáveis)

Nenhum dos três valores abaixo foi validado contra volume real de criação
de recursos numa conta de cliente — são pontos de partida conservadores,
documentados aqui de propósito para serem revisitados com dado real:

| Parâmetro | Default | Por quê |
|---|---|---|
| `DebounceSeconds` | 1800 (30 min) | Combinado explicitamente — tempo de sobra para IaC aplicar suas próprias tags antes da Etapa 3 agir; não é a única proteção (a revalidação embutida em `tag_execution.run_tagging_execution` cobre o resto). |
| `LambdaReservedConcurrency` | 5 | Evita que rajadas de criação de recurso (ex.: Auto Scaling) contribuam para throttling de API na conta — baixo de propósito; se o CloudWatch mostrar throttling do próprio Lambda (`Throttles` na função), subir. |
| `MaximumEventAgeInSeconds` | 3600 (1h) | Depois disso, o EventBridge desiste de entregar ao alvo — um evento de criação processado com mais de 1h de atraso já perdeu boa parte do valor de ser reativo; a Etapa 4 é o backstop. |

## Permissões IAM — cobertura real

A política `PrmEtapa3NativeTagWrite` tem a ação de tagging NATIVA (exigida
além de `tag:TagResources` para o caminho genérico, ver
[docs/producao.md](../docs/producao.md#permissões-iam-para-a-etapa-2c-apply---live-execução-real))
para **67 ações**, uma por serviço mapeado em `event_mapping.py` — cobre
os serviços que de fato exigem ação nativa (EKS/Bedrock/ELB já têm sua
permissão via `PrmEtapa3TagWrite`, que usa a API dedicada, não o caminho
genérico). Cada nome de ação foi confirmado contra o botocore instalado
(`session.get_service_model(...).operation_names`), não veio de memória —
mesmo processo de verificação de `event_parser.py`.

**Cobertura confirmada em sandbox — CodeBuild não precisa de ação
nativa.** O pacote `codebuild` do botocore não tem nenhuma operação com
"tag" no nome, o que levantava a dúvida se `tag:TagResources` sozinho
bastava. **Testado na conta sandbox em 2026-09-23** (criar projeto →
`tag:TagResources` → confirmar via `batch-get-projects` → apagar projeto,
ver [test/manual-live-etapa3/README.md](../test/manual-live-etapa3/README.md#parte-3--investigar-a-ação-de-tag-do-codebuild)):
o caminho genérico funcionou sozinho, sem precisar de `UpdateProject` nem
de nenhuma entrada nova em `PrmEtapa3NativeTagWrite`. Cobertura real hoje:
**69 de 69 serviços mapeados**, nenhum gap conhecido.

**2 ações da política estavam ERRADAS, não faltando** — só descoberto com
o smoke test de ponta a ponta real (ver
[test/manual-live-etapa3/README.md](../test/manual-live-etapa3/README.md#parte-2--smoke-test-de-ponta-a-ponta-implanta-a-stack)),
porque as duas passavam despercebidas pela verificação estrutural (a ação
errada também existe no botocore, só não é a que `tag:TagResources` invoca
de verdade):

- `s3:PutBucketTagging` sozinho não basta — falta `s3:GetBucketTagging`
  também (`tag:TagResources` lê o TagSet atual antes de escrever). Todo
  bucket teria falhado silenciosamente (sem exceção, só `erro_permissao`
  no relatório/SNS) até este teste.
- `logs:TagLogGroup` estava errado — a ação certa é `logs:TagResource`
  (API unificada, a que `tag:TagResources` de fato chama). Achado porque a
  própria Lambda, ao criar seu log group na primeira execução, gerou um
  evento `CreateLogGroup` real que a Etapa 3 processou e tentou taguear.

Ambas corrigidas em `template-regional.yaml` e reconfirmadas com um segundo smoke
test depois do redeploy.

## Restrição de chave de tag (`aws:TagKeys`)

`PrmEtapa3TagWrite` e `PrmEtapa3NativeTagWrite` têm uma `Condition`
(`ForAllValues:StringEquals` em `aws:TagKeys`) restringindo as ~69+4 ações
de escrita cobertas a só poderem escrever a chave `aws-apn-id` — mesmo que
um bug no código tentasse escrever outra chave, o IAM bloqueia antes de
chegar na API (defesa em profundidade; o código já só escreve essa 1
chave, então isso não deveria mudar nenhum comportamento observável hoje).

**Não testado em sandbox contra as ações nativas** — `aws:TagKeys` é uma
condition key global documentada pela AWS para APIs de tagging, mas isso
não foi confirmado uma a uma contra as ~69 ações específicas da política
(seria um novo roteiro de teste, um por ação, fora do escopo desta
rodada). Se alguma dessas ações não respeitar a condition key como
esperado, o sintoma seria um `AccessDenied` novo numa tentativa de escrita
que antes funcionava — vale ficar atento a isso no próximo smoke test
real.

## Build e deploy

```bash
# do diretório que contém o Makefile e aws_prm_tagging/
sam build --template-file aws_prm_tagging/infra/template-hub.yaml
sam deploy --stack-name prm-hub --region <regiao-hub> --resolve-s3 --capabilities CAPABILITY_IAM \
  --parameter-overrides NotificationEmail=<email> ExpectedTagValue=pc:<code> ModoTagueamentoInicial=dry-run
sam build --template-file aws_prm_tagging/infra/template-regional.yaml
sam deploy --stack-name prm-regional --region <cada-regiao> --resolve-s3 --capabilities CAPABILITY_IAM \
  --parameter-overrides ExpectedTagValue=pc:<code> HubRegion=<regiao-hub>
```

**Validado localmente (sem tocar em conta real) E contra uma conta sandbox
real** (`cfn-lint`, `sam validate --lint`, `sam build`, `sam local invoke`
e, por fim, `sam deploy` de verdade — ver
[test/manual-live-etapa3/README.md](../test/manual-live-etapa3/README.md)
para o roteiro e resultado completos):

- `cfn-lint`/`sam validate --lint` — zero findings. Também roda como teste
  automatizado (`test/unit/test_infra_template_cfn_lint.py`, pula
  silenciosamente se `cfn-lint` não estiver instalado).
- `sam build` — **achou e corrigiu um bug real**: `.samignore` não é um
  mecanismo suportado pelo SAM CLI (confirmado lendo o código-fonte
  instalado — nenhuma referência a isso em `samcli`). O build estava
  empacotando `test/`/`docs/`/`infra/`/`reference/` inteiros dentro da
  Lambda, sem ninguém perceber. Corrigido com `Metadata.BuildMethod:
  makefile` + o `Makefile` na raiz de `CodeUri` (`uso-geral/tag/Makefile`)
  — copia só os módulos `.py` do pacote e o CSV em `data/`. Confirmado
  inspecionando o diretório de build: só os arquivos esperados entram.
- `sam local invoke` — a função importa e executa corretamente dentro de um
  container `public.ecr.aws/lambda/python:3.12` real (mesma imagem que a
  AWS usa), processando um evento fora de escopo do CSV de ponta a ponta
  sem erro.
- **`sam deploy` real, contra a conta sandbox (2026-09-23/24)** — stack
  completa implantada (`prm-etapa3-smoke-test`, `us-east-2`), um bucket S3
  criado de propósito disparou o pipeline inteiro (EventBridge → Step
  Functions → Lambda → decisão → escrita real) e **a tag chegou
  corretamente no recurso**. Achou e corrigiu **2 bugs reais de permissão
  IAM** (`s3:GetBucketTagging` faltando, `logs:TagLogGroup` errado em vez
  de `logs:TagResource` — ver "Permissões IAM" acima) que nenhuma
  verificação estática pegaria, porque as duas passavam despercebidas em
  silêncio (sem exceção, só reportadas como `erro_permissao` no
  relatório/SNS) até um evento real de verdade expor o `FailedResourcesMap`
  do `tag:TagResources` no CloudTrail. Stack e recursos de teste apagados
  ao final, nada ficou na conta.

**O que isso ainda NÃO valida**: volume real de eventos em produção
(rajadas de criação de recurso), e o caminho de Route 53/CloudFront quando
a stack estiver numa região diferente de `us-east-1` — achado novo,
documentado como pendência de arquitetura em
[docs/melhorias-futuras.md](../docs/melhorias-futuras.md#a-stack-cobre-só-1-região--qualquer-recurso-regional-fora-dela-fica-invisível-route-53cloudfront-são-só-o-caso-mais-extremo).
CloudFront (`CreateDistribution`) também não teve seu evento capturado
(pulado por custo de tempo de propagação/exclusão, não de dinheiro) — segue
com leitura tolerante a capitalização como mitigação.
