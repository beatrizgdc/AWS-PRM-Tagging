# Guia de teste na sandbox — escritas só em us-east-2 (Ohio)

Versão de 2026-10-02. Executado pelo responsável do projeto; o assistente
não tem acesso à conta.

**Regra desta rodada:** toda criação/alteração acontece em `us-east-2`.
As outras regiões só são **lidas** (tagueamento inicial e varredura, em
dry-run). Roles IAM são globais por natureza — as que existem aqui são as
das próprias stacks e saem com elas.

**Como rodar:** um bloco por vez. Os blocos marcados **ESPERE** pedem uma
pausa antes do próximo. **Pare no passo F e me mande os resultados antes de
limpar.**

---

## A — Preparação

```bash
cd <pasta que contém Makefile e aws_prm_tagging/>
export P=default
export EU=fulano@darede.com.br       # o seu e-mail SSO (trecho final do ARN do `aws sts get-caller-identity`)
export EMAIL=fulano@darede.com.br    # e-mail que recebe as notificações
export R=us-east-2          # região de todas as escritas
export HUB=us-east-2        # região da stack hub (ver variante no fim do guia)
export ACC=$(aws sts get-caller-identity --profile $P --query Account --output text)
echo "conta=$ACC"
```

## B — Etapa 1 + 2c em us-east-2 (refaz o passo 2, confirma o DynamoDB)

Cria recursos vazios (`prm-val-*`: 4 buckets S3, 2 filas SQS, 1 tópico
SNS, 1 tabela DynamoDB, 1 log group, 1 repositório ECR), tagueia **só
eles** e confere lendo de volta (agora com espera de propagação).

```bash
aws_prm_tagging/test/manual-live-lote2/validar_etapa2c.sh --profile $P --valor pc:testeprmsandbox --regiao $R
```

Anote a tabela final. Depois remova (bloco separado):

```bash
aws_prm_tagging/test/manual-live-lote2/limpar.sh saida-prm-val-*/manifest.tsv
rm -rf saida-prm-val-*
```

## C — Implantar as stacks

```bash
sam build --template-file aws_prm_tagging/infra/template-hub.yaml
```
```bash
sam deploy --stack-name prm-hub --region $HUB --profile $P --resolve-s3 --s3-prefix prm-teste \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides NotificationEmail=$EMAIL ExpectedTagValue=pc:testeprmsandbox \
    ModoTagueamentoInicial=dry-run ModoVarreduraRecorrente=desligado ContratoConfirmado=true
```
```bash
sam build --template-file aws_prm_tagging/infra/template-regional.yaml
```
```bash
sam deploy --stack-name prm-regional --region $R --profile $P --resolve-s3 --s3-prefix prm-teste \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides ExpectedTagValue=pc:testeprmsandbox DebounceSeconds=60 HubRegion=$HUB \
    FiltroEscopoTeste="$EU,prm-val"
```

**ESPERE:** confirme a assinatura no e-mail novo da AWS (o tópico é novo).

O tagueamento inicial roda sozinho ao criar o `prm-hub`, em dry-run.
Esperado: **um e-mail por região ativa (17)**, assunto `PRM Etapa
tagueamento_inicial - resumo <regiao> (dry-run)`. Os que chegarem antes da
confirmação se perdem — sem problema, a varredura do passo E repete.

## D — Testes da Etapa 3 e da Etapa 4

Cada teste: rode o bloco, **ESPERE ~3 min**, rode a conferência.

### D1 — Lambda (bug do nome versionado `CreateFunction20150331`)

```bash
ROLE=$(aws cloudformation describe-stack-resource --stack-name prm-regional --logical-resource-id Etapa3ExecutionRole \
       --region $R --profile $P --query StackResourceDetail.PhysicalResourceId --output text)
ROLE_ARN=$(aws iam get-role --role-name $ROLE --profile $P --query Role.Arn --output text)
python3 -c "import zipfile; zipfile.ZipFile('/tmp/fn.zip','w').writestr('index.py','def handler(e, c):\n    return 1\n')"
aws lambda create-function --function-name prm-val-fn --runtime python3.12 --handler index.handler \
  --role $ROLE_ARN --zip-file fileb:///tmp/fn.zip --region $R --profile $P --query FunctionArn --output text
```
(Usa a role da própria automação só porque ela já confia no Lambda — a
função de teste nunca é invocada.)

Conferência — esperado `aws-apn-id = pc:testeprmsandbox` e e-mail
`tagueado_sucesso`:
```bash
aws lambda list-tags --resource arn:aws:lambda:$R:$ACC:function:prm-val-fn --region $R --profile $P
```

### D2 — DynamoDB

```bash
aws dynamodb create-table --table-name prm-val-tab --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=id,AttributeType=S --key-schema AttributeName=id,KeyType=HASH \
  --region $R --profile $P --query TableDescription.TableArn --output text
```
Conferência:
```bash
aws dynamodb list-tags-of-resource --resource-arn arn:aws:dynamodb:$R:$ACC:table/prm-val-tab --region $R --profile $P
```

### D3 — IoT billing group (item 10)

```bash
aws iot create-billing-group --billing-group-name prm-val-bg --region $R --profile $P --query billingGroupArn --output text
```
Conferência:
```bash
aws iot list-tags-for-resource --resource-arn arn:aws:iot:$R:$ACC:billinggroup/prm-val-bg --region $R --profile $P
```

### D4 — Drift (remoção da tag)

```bash
export B=prm-val-drift-$(date +%s); echo $B
aws s3api create-bucket --bucket $B --region $R --create-bucket-configuration LocationConstraint=$R --profile $P
```
**ESPERE ~3 min** e confira que a Etapa 3 tagueou:
```bash
aws s3api get-bucket-tagging --bucket $B --region $R --profile $P
```
Agora remova a tag (simula alguém apagando):
```bash
aws s3api delete-bucket-tagging --bucket $B --region $R --profile $P
```
**ESPERE ~5 min.** Esperado: e-mail `PRM Etapa 4-drift - drift_tag_removida`
com o ARN do bucket e o seu usuário em `principal`, **e a tag NÃO volta**:
```bash
aws s3api get-bucket-tagging --bucket $B --region $R --profile $P   # esperado: NoSuchTagSet
```

### D5 — DLQ (falha de processamento)

```bash
SM3=$(aws cloudformation describe-stack-resource --stack-name prm-regional --logical-resource-id Etapa3StateMachine \
      --region $R --profile $P --query StackResourceDetail.PhysicalResourceId --output text)
EX=$(aws stepfunctions start-execution --state-machine-arn $SM3 --input '{"detail":"forcar-erro"}' \
     --region $R --profile $P --query executionArn --output text); echo $EX
```
**ESPERE ~2 min** (passa pelo debounce de 60 s). Esperado: `FAILED` e `1`:
```bash
aws stepfunctions describe-execution --execution-arn $EX --region $R --profile $P --query status --output text
aws sqs get-queue-attributes --region $R --profile $P --attribute-names ApproximateNumberOfMessages \
  --queue-url $(aws sqs get-queue-url --queue-name prm-etapa3-dlq --region $R --profile $P --query QueueUrl --output text)
```

## E — Varredura recorrente manual (dry-run, só leitura)

```bash
SMH=$(aws cloudformation describe-stack-resource --stack-name prm-hub --logical-resource-id TagueamentoInicialStateMachine \
      --region $HUB --profile $P --query StackResourceDetail.PhysicalResourceId --output text)
EXH=$(aws stepfunctions start-execution --state-machine-arn $SMH --region $HUB --profile $P \
      --input '{"modo":"dry-run","origem":"varredura_recorrente"}' --query executionArn --output text); echo $EXH
```
**ESPERE ~10 min.** Conferência — esperado `SUCCEEDED`:
```bash
aws stepfunctions describe-execution --execution-arn $EXH --region $HUB --profile $P --query status --output text
```
E-mails esperados (assunto `PRM Etapa varredura_recorrente - resumo ...`):

- `us-east-2`: **com** e-mail — há novidade (a mensagem do D5 na DLQ).
- As outras 16 regiões: **com** e-mail, `stack_regional_detectada: false`
  (não há stack regional nelas — é o sinal de lacuna de cobertura, esperado
  aqui).

## F — PARE AQUI e me mande

1. A tabela final do passo B.
2. A saída das conferências D1 a D5 e do passo E.
3. Um e-mail de cada tipo: `tagueamento_inicial`, `tagueado_sucesso`
   (Etapa 3), `drift_tag_removida`, `varredura_recorrente` de `us-east-2` e
   de uma região sem stack.

Só depois sigo com você para a limpeza.

---

## G — Limpeza (só depois do passo F)

**G1 — recursos de teste** (`us-east-2`):
```bash
aws lambda delete-function --function-name prm-val-fn --region $R --profile $P
aws dynamodb delete-table --table-name prm-val-tab --region $R --profile $P --query TableDescription.TableStatus --output text
aws iot delete-billing-group --billing-group-name prm-val-bg --region $R --profile $P
aws s3api delete-bucket --bucket $B --region $R --profile $P
```

**G2 — stacks** (as roles IAM, a DLQ, as regras e o tópico saem junto):
```bash
sam delete --stack-name prm-regional --region $R --profile $P --no-prompts
sam delete --stack-name prm-hub --region $HUB --profile $P --no-prompts
```

**G3 — log groups das Lambdas: LISTAR** (o `sam delete` não remove). Só
prefixos exatos das nossas stacks:
```bash
for PFX in /aws/lambda/prm-hub- /aws/lambda/prm-regional-; do
  aws logs describe-log-groups --log-group-name-prefix $PFX --region $R --profile $P \
    --query 'logGroups[].logGroupName' --output text
done
```
**G4 — conferido o G3, apagar** (bloco separado):
```bash
for PFX in /aws/lambda/prm-hub- /aws/lambda/prm-regional-; do
  for G in $(aws logs describe-log-groups --log-group-name-prefix $PFX --region $R --profile $P \
             --query 'logGroups[].logGroupName' --output text); do
    aws logs delete-log-group --log-group-name "$G" --region $R --profile $P && echo "apagado $G"
  done
done
```

**G5 — artefatos do SAM** (o bucket de `us-east-2` já existia; removemos só
o prefixo `prm-teste/`, com todas as versões):
```bash
BK=$(aws cloudformation describe-stack-resource --stack-name aws-sam-cli-managed-default --logical-resource-id SamCliSourceBucket \
     --region $R --profile $P --query StackResourceDetail.PhysicalResourceId --output text); echo $BK
aws s3api list-object-versions --bucket $BK --prefix prm-teste/ --profile $P --output text \
  --query '[Versions, DeleteMarkers][][].[Key, VersionId]' | while read KEY VID; do
    [ -n "$VID" ] && aws s3api delete-object --bucket $BK --key "$KEY" --version-id "$VID" --profile $P >/dev/null && echo "removido $KEY"
done
```

**G6 — verificação final** (somente leitura; esperado: só a stack
`aws-sam-cli-managed-default`, que já existia, e nenhuma linha com `prm-teste/`):
```bash
aws cloudformation list-stacks --region $R --profile $P \
  --query "StackSummaries[?StackStatus!='DELETE_COMPLETE' && (starts_with(StackName,'prm-') || starts_with(StackName,'aws-sam-cli'))].[StackName,StackStatus]" --output text
aws lambda list-functions --region $R --profile $P --query "Functions[?starts_with(FunctionName,'prm-')].FunctionName" --output text
aws stepfunctions list-state-machines --region $R --profile $P --query "stateMachines[?starts_with(name,'Etapa3') || starts_with(name,'TagueamentoInicial')].name" --output text
aws events list-rules --region $R --profile $P --name-prefix prm- --query "Rules[].Name" --output text
aws sqs list-queues --region $R --profile $P --queue-name-prefix prm- --output text
aws sns list-topics --region $R --profile $P --query "Topics[?contains(TopicArn,'prm-compliance')].TopicArn" --output text
aws logs describe-log-groups --region $R --profile $P --log-group-name-prefix /aws/lambda/prm- --query "logGroups[].logGroupName" --output text
aws iam list-roles --profile $P --query "Roles[?starts_with(RoleName,'prm-')].RoleName" --output text
aws s3api list-object-versions --bucket $BK --prefix prm-teste/ --profile $P --query '[Versions, DeleteMarkers][][].Key' --output text
```

---

## Inventário — tudo em us-east-2

| Recurso | Criado em | Removido em |
|---|---|---|
| Recursos `prm-val-*` do script (S3, SQS, SNS, DynamoDB, log group, ECR) | B | B (`limpar.sh`) |
| Stack `prm-hub`: tópico `prm-compliance` + assinatura, 3 Lambdas, 1 state machine, 3 roles IAM, Custom Resource | C | G2 |
| Stack `prm-regional`: 2 Lambdas, 1 state machine, 9 regras EventBridge, fila `prm-etapa3-dlq`, 3 roles IAM | C | G2 |
| Log groups `/aws/lambda/prm-hub-*` e `/aws/lambda/prm-regional-*` | AWS, na 1ª execução | G4 |
| Artefatos `prm-teste/*` no bucket do SAM que já existia | C | G5 |
| `prm-val-fn`, `prm-val-tab`, `prm-val-bg`, bucket `prm-val-drift-*` | D1–D4 | G1 |
| Outras 16 regiões | **nada** (só leitura) | — |

Nenhum recurso com custo por hora.

---

## Variante opcional — confirmar a publicação ENTRE regiões

Com hub e regional na mesma região, o código que aponta o client do SNS
para a região do tópico roda, mas não é provado entre regiões diferentes
(a topologia de produção: hub numa região, stacks regionais em todas). Para
cobrir isso, antes do passo C use:

```bash
export HUB=us-east-1
```

Isso cria em **us-east-1**: a stack `prm-hub` (tópico SNS, 3 Lambdas, 1
state machine, Custom Resource), os log groups `/aws/lambda/prm-hub-*` e,
se ainda não existir, a stack `aws-sam-cli-managed-default` com o bucket de
artefatos (foi apagada na rodada anterior, então será recriada). Na
limpeza: G2 já usa `$HUB`; repita G3/G4 com `--region us-east-1`; e no G5,
como esse bucket seria novo, apague a stack inteira
(`aws cloudformation delete-stack --stack-name aws-sam-cli-managed-default
--region us-east-1` depois de esvaziar o bucket, como na rodada anterior).

Sem a variante, essa é a **única** confirmação que fica de fora — e ela
está coberta por teste unitário.
