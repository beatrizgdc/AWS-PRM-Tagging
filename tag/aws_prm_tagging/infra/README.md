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
alternativas em N patterns, cada um dentro do limite — hoje **3** regras
(`Etapa3EventRule0`, `1`, `2`).

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
praticamente todos os ~69 serviços mapeados (EKS/Bedrock/ELB já têm sua
permissão via `PrmEtapa3TagWrite`, que usa a API dedicada, não o caminho
genérico). Cada nome de ação foi confirmado contra o botocore instalado
(`session.get_service_model(...).operation_names`), não veio de memória —
mesmo processo de verificação de `event_parser.py`.

**Único serviço mapeado sem ação de tag encontrada**: `CodeBuild` — o
pacote `codebuild` do botocore não tem nenhuma operação com "tag" no nome;
tags de projeto parecem ser geridas via `UpdateProject` (todo o objeto,
sem uma ação `TagResource` dedicada), mas isso não foi confirmado. Até
resolver, um `CreateProject` do CodeBuild vai gerar uma tentativa de escrita
que falha (`erro_permissao` ou `ValidationException`, dependendo do
mecanismo real) — comportamento seguro. Roteiro de investigação em conta
sandbox (não executado ainda) em
[test/manual-live-etapa3/README.md](../test/manual-live-etapa3/README.md#parte-3--investigar-a-ação-de-tag-do-codebuild).

## Build e deploy

```bash
# do diretório uso-geral/tag/ (contém o Makefile e aws_prm_tagging/)
sam build --template-file aws_prm_tagging/infra/template.yaml
sam deploy --guided
```

**Validado nesta sessão, sem tocar em nenhuma conta AWS real** (`cfn-lint`,
`sam validate --lint`, `sam build` e `sam local invoke` contra um evento
sintético fora de escopo, todos rodados localmente — o `sam local invoke`
sobe o runtime Lambda real via Docker, mas não chama nenhuma API AWS de
verdade nesse caminho de teste):

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

**O que isso ainda NÃO valida** — precisa de conta AWS real (roteiro
elaborado, não executado, em
[test/manual-live-etapa3/README.md](../test/manual-live-etapa3/README.md)):
se a regra do EventBridge de fato casa um evento real, se o Step Functions
invoca a Lambda corretamente, se a tag chega de verdade no recurso, e a
capitalização exata do CloudTrail para os ~9 serviços de protocolo
`query`/`ec2`/`rest-xml` onde isso é incerto (ver docstring de
`event_parser.py`).
