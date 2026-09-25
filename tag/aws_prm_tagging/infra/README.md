# Infraestrutura da Etapa 3 (automação contínua)

Template AWS SAM (`template.yaml`) que empacota a Etapa 3 — detecção de
criação de recurso via EventBridge, debounce via Step Functions, tagueamento
real reaproveitando `decision.py` (Etapa 2a) e `tag_execution.py` (Etapa
2c). Ver [docs/arquitetura.md](../docs/arquitetura.md) para o desenho
completo e as decisões registradas.

## Escopo — o que ESTE template não é

A arquitetura de destino é **uma única stack CloudFormation por conta
cliente**, implantada via StackSet (ver
[docs/arquitetura-multicliente.md](../docs/arquitetura-multicliente.md)),
cobrindo:

- Etapa 1/2 — Custom Resource disparando mapeamento + tagueamento inicial na
  criação da stack. **Não implementado ainda** (nenhum código de
  empacotamento Lambda existe para isso hoje).
- Etapa 3 — este template.
- Etapa 4 — varredura recorrente/drift. **Não implementado ainda.**

Este `template.yaml` é implantável sozinho (para testar a Etapa 3 isolada
numa conta sandbox), mas **não é a stack final**. Quando as Etapas 1/2/4
forem implementadas, os recursos aqui (função Lambda, tópico SNS, IAM,
Step Functions, regras de EventBridge) devem ser **mesclados** numa única
stack — em particular, `PrmComplianceTopic` (o tópico SNS) deve ficar numa
seção compartilhada e nunca duplicado por etapa, já que a Etapa 4 vai
publicar no mesmo tópico (ver `publish.py`).

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

**Depois disso, um passo manual**: o CloudFormation não tem como incluir um
JSON externo dentro de `AWS::Events::Rule.EventPattern` (ao contrário de
`AWS::Serverless::StateMachine`, que usa `DefinitionUri` para o Step
Functions). Os blocos `EventPattern` em `template.yaml` precisam ser colados
manualmente a partir dos arquivos gerados. Dois testes existem justamente
para nunca deixar isso divergir em silêncio:

- `test/unit/test_event_mapping.py::test_arquivos_gerados_em_infra_nao_estao_desatualizados`
  — os arquivos gerados batem com `event_mapping.py` agora.
- `test/unit/test_infra_event_patterns.py` — os blocos `EventPattern` de
  `template.yaml` batem com os arquivos gerados.

Se o número de arquivos gerados mudar (uma nova regra é necessária),
adicione/remova o recurso `AWS::Events::Rule` correspondente em
`template.yaml` manualmente (copiando o `EventPattern` de
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
parâmetro `NotificationEmail` em `infra/template.yaml` (seção
`Parameters`). Duas formas de usar isso:

- **Trocar o default permanentemente** (todo deploy que não passar
  `--parameter-overrides` explícito usa o novo valor): editar essa única
  linha em `template.yaml` e commitar.
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

Ambas corrigidas em `template.yaml` e reconfirmadas com um segundo smoke
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
# do diretório uso-geral/tag/ (contém o Makefile e aws_prm_tagging/)
sam build --template-file aws_prm_tagging/infra/template.yaml
sam deploy --guided
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
