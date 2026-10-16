# Infraestrutura

| Template | Onde | Conteúdo |
| --- | --- | --- |
| `template-hub.yaml` (SAM) | 1 vez por conta, na região hub | Tópico SNS `prm-compliance` (+ e-mail opcional), tagueamento inicial (Custom Resource → Step Functions → uma Lambda por região), varredura recorrente (Scheduler) |
| `template-regional.yaml` (SAM) | Toda região ativa + `us-east-1` sempre | Etapa 3 (EventBridge → Step Functions com debounce → Lambda), drift (Etapa 4), DLQ `prm-etapa3-dlq` |
| `template-central.yaml` | Payer, opcional | Tópico SNS da organização (KMS próprio) e fila para consumidores |
| `template-zabbix.yaml` (SAM) | Onde estiver o tópico | Lambda que envia as mensagens ao Zabbix (`zabbix/template-prm-tagging.yaml` é o template para importar no Zabbix) |

`us-east-1` sempre: é a única região onde o EventBridge recebe eventos de
serviços globais (Route 53, CloudFront). A stack regional publica no tópico do
hub pelo ARN montado com `HubRegion` + `HubTopicName` (ou no tópico central,
com `TopicoCentralArn`).

Todas as Lambdas usam a layer `PrmSdkLayer` (boto3 fixado por hash em
`../requirements-lambda.txt`) e o `Makefile` da raiz para o build, que copia
só os `.py` e o CSV.

## Parâmetros principais

| Parâmetro | Stack | Padrão | Uso |
| --- | --- | --- | --- |
| `ExpectedTagValue` | hub, regional | `pc:825bh0xhwumf14tg27cthnoss` | Valor da tag |
| `NotificationEmail` | hub | vazio (sem e-mail) | Assinatura do tópico (exige confirmar o e-mail) |
| `ModoTagueamentoInicial` | hub | `dry-run` | `dry-run`, `live` ou `desligado` |
| `ReexecutarTagueamentoInicial` | hub | `1` | Mudar o valor roda o tagueamento inicial de novo (trocar só o modo não roda) |
| `ModoVarreduraRecorrente` / `AgendaVarredura` | hub | `dry-run` / `rate(1 day)` | Varredura; `live` reaplica a tag |
| `ContratoConfirmado` | hub | `false` | Sinaliza nos resumos conta que herdou o valor da OU |
| `RegioesVarredura` | hub | todas | Restringe tagueamento inicial e varredura |
| `DebounceSeconds` | regional | `1800` | Espera antes de agir em recurso novo (corrida com IaC) |
| `LambdaReservedConcurrency` | regional | `5` | Limita a Lambda da Etapa 3 em rajadas |
| `PublicarResultadoJaOk` | regional | `true` | `false` evita e-mail por task ECS que já nasce com a tag |
| `FiltroEscopoTeste` | regional | vazio | Só para conta compartilhada de teste |
| `TopicoCentralArn` / `TopicoCentralKmsArn` | hub, regional | vazio | Publicar no tópico central (StackSets) |
| `RetencaoLogsDias` / `Arquitetura` | todas SAM | `30` / `arm64` | Logs e arquitetura das Lambdas |

## Regras de EventBridge da Etapa 3

O pattern de todos os serviços mapeados passa da quota padrão de 2.048
caracteres, então há uma regra `Etapa3EventRule<N>` por arquivo
`event_pattern.<N>.generated.json` (hoje 6). Os arquivos e os blocos
`EventPattern` do template são gerados por `python3 -m aws_prm_tagging.event_mapping`,
nunca editados à mão; `test_infra_event_patterns*.py` falham se divergirem ou se
o número de regras precisar mudar. Cada alternativa exige `errorCode` ausente,
para chamadas que falharam não acionarem a automação.

Regras nativas (sem trail): instância EC2 `running` e task ECS Fargate
`RUNNING` (esta sem debounce). A regra de drift recebe remoções/substituições
de tag de qualquer serviço.

## Permissões

- Toda escrita de tag em `*` tem a condição `aws:TagKeys = [aws-apn-id]`.
- `tag:TagResources` exige também a ação nativa de cada serviço
  (`PrmEtapa3NativeTagWrite`); CodeBuild não precisa. API Gateway usa
  `apigateway:PUT` em `/tags/*`.
- A leitura do hub é a fonte do subcomando `politica-iam`; os testes travam
  a paridade entre hub e regional.

## Build e deploy (uma conta)

Use `deploy/implantar_conta.sh` (ou `python3 iniciar.py`). Manual, a partir da
pasta que contém o `Makefile`:

```bash
sam build --template-file aws_prm_tagging/infra/template-hub.yaml
sam deploy --stack-name prm-hub --region <regiao-hub> --resolve-s3 --capabilities CAPABILITY_IAM \
  --parameter-overrides NotificationEmail=<email> ModoTagueamentoInicial=dry-run
sam build --template-file aws_prm_tagging/infra/template-regional.yaml
sam deploy --stack-name prm-regional --region <cada-regiao> --resolve-s3 --capabilities CAPABILITY_IAM \
  --parameter-overrides HubRegion=<regiao-hub>
```

Várias contas: `deploy/stacksets/` (ver `docs/MVP-2.md`, seção StackSets).
