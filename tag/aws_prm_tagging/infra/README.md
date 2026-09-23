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

A política `PrmEtapa3NativeTagWriteLoteInicial` só tem a ação de tagging
NATIVA (exigida além de `tag:TagResources`, ver
[docs/producao.md](../docs/producao.md#permissões-iam-para-a-etapa-2c-apply---live-execução-real))
para os serviços do **lote inicial** com extractor dedicado em
`event_parser.py` (EC2, S3, Lambda, DynamoDB, RDS, EKS, Bedrock, SNS, SQS,
ECR, ECS, EFS, ElastiCache, KMS, CloudFront, Route 53, Secrets Manager, Step
Functions, ELB). Os demais serviços mapeados em `event_mapping.py` (cobertos
só pelo extractor genérico best-effort) vão falhar com `AccessDenied` ao
tentar taguear até a permissão nativa correspondente ser adicionada aqui —
isso é **esperado e seguro** (reportado como `erro_permissao`, nunca uma
ação incorreta), não uma lacuna silenciosa.

## Build e deploy (referência — não validado em sandbox ainda)

```bash
# do diretório uso-geral/tag/ (contém .samignore e aws_prm_tagging/)
sam build --template-file aws_prm_tagging/infra/template.yaml
sam deploy --guided
```

Nenhum `sam build`/`sam deploy`/`sam validate` foi executado contra uma
conta AWS real neste trabalho — só a validação de sintaxe YAML e a checagem
de que os `EventPattern` batem com os arquivos gerados (ver testes acima).
Antes de implantar contra qualquer conta real: rodar `sam validate` e
`cfn-lint`, e testar em sandbox — mesma disciplina já seguida para a Etapa
2c (`apply --live`) no restante do projeto.
