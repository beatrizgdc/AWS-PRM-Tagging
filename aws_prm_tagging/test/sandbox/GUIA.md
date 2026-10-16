# Roteiro de validação na sandbox (escritas só em us-east-1)

**[para rodar na sandbox]**. Antes, tudo foi **[simulado localmente]** (moto,
SAM CLI, cfn-lint) e os testes automatizados passam.

**Regras:**

- Toda criação/alteração acontece em `us-east-1` (na sandbox atual, uma SCP
  bloqueia us-east-2). As outras regiões não são tocadas.
- Recursos de teste começam com `prm-val-`. A Etapa 3 só reage a eles e ao seu
  usuário (`FiltroEscopoTeste`); nada de outras pessoas é alterado.
- Rode **um bloco por vez**. Onde está **ESPERE**, aguarde antes do próximo.
- **Pare no passo G** e me mande os resultados antes de limpar.

O que esta rodada prova:

| Item | Passo |
|---|---|
| Regra nova: sobrescrever outro parceiro, chave parecida, todo IaC pulado | C, E1, E2 |
| Terraform detectado pelo `userAgent` do CloudTrail (evento e histórico) | B, E3, E4 |
| Relatório para o cliente (IaC + estado anterior) | B, C, F |
| boto3 atual numa layer (`versao_boto3` no e-mail) | D |
| Modo interativo (`iniciar.py`) | B |
| Log groups apagados junto com a stack | H |
| Tópico SNS criptografado, arm64 | D |

---

## A — Preparação

Descompacte o pacote numa pasta **nova**.

```bash
cd <pasta do pacote>
python3 -m venv .venv && . .venv/bin/activate
pip install -r aws_prm_tagging/requirements-dev.txt
aws login            # ou o login SSO de sempre
export P=default
export EU=fulano@darede.com.br       # seu e-mail SSO (fim do ARN do get-caller-identity)
export EMAIL=fulano@darede.com.br    # quem recebe as notificações
export R=us-east-1
export HUB=us-east-1
export ACC=$(aws sts get-caller-identity --profile $P --query Account --output text); echo "conta=$ACC"
```

## B — Modo interativo, só leitura (nada é criado)

**Cria:** nada na AWS. **Custo:** zero (leituras e `LookupEvents`, que é grátis).
**Gera:** a pasta `saida-prm-<conta>-<data>/` no seu computador.

```bash
python3 iniciar.py --profile $P --valor pc:testeprmsandbox --regioes $R
```

Responda: conta **s** · mapear **s** · simular **s** · aplicar a tag **n** · implantar **n**.

O mapeamento consulta o histórico do CloudTrail (2 chamadas/s) para cada
candidato. Com uns 300 recursos, leva alguns minutos.

Depois, rode a contagem (só leitura do arquivo local):
```bash
python3 - <<'EOF'
import json, glob, collections
m = json.load(open(sorted(glob.glob("saida-prm-*/mapa.json"))[-1]))
print("historico:", collections.Counter(r.get("iac_historico", "-") for r in m["recursos"]))
print("iac:", collections.Counter((r["iac"].get("ferramenta") or r["iac"]["tipo"]) for r in m["recursos"]))
EOF
```

**Me mande:** a tela do assistente (as tabelas) e a saída da contagem.

O relatório `saida-prm-*/resultado_dry_run_cliente.md` lista recursos de
IaC **de outras pessoas** da sandbox. Ele é só para você conferir o formato;
não circule.

## C — Etapa 1 + 2c com a regra nova (cria e remove recursos de teste)

**Cria:** 5 buckets S3, 2 filas SQS, 1 tópico SNS, 1 tabela DynamoDB on-demand,
1 log group, 1 repositório ECR, todos vazios e com prefixo `prm-val-<data>`.
**Custo:** zero enquanto vazios. **Remove:** `limpar.sh` (bloco seguinte).

```bash
aws_prm_tagging/test/sandbox/validar_etapa2c.sh --profile $P --valor pc:testeprmsandbox --regiao $R
```

Esperado na tabela final:

- `...-outro-parceiro` → `sobrescrito`, com tag real `pc:testeprmsandbox`.
- `...-similar` → `adicionado_com_similar`, com tag real `pc:testeprmsandbox`
  (a `AWS-APN-ID` continua lá).
- `...-terraform` e `...-pulumi` → `pulado_iac`, intocados e listados em
  `resultado_live_cliente.md`.

Abra também `saida-prm-val-*/resultado_live_cliente.md`.

Remova (bloco separado):
```bash
aws_prm_tagging/test/sandbox/limpar.sh saida-prm-val-*/manifest.tsv
```

## D — Implantar as stacks (us-east-1)

**Cria:**

- **Stack `prm-hub`:**
  - tópico `prm-compliance` (criptografado) e assinatura de e-mail;
  - 3 Lambdas e 1 layer `PrmSdkLayer`;
  - 1 state machine, 3 roles IAM e 3 log groups `/aws/lambda/prm-hub/*`.
- **Stack `prm-regional`:**
  - 2 Lambdas e 1 layer;
  - 1 state machine e 9 regras do EventBridge;
  - fila `prm-etapa3-dlq`;
  - 3 roles e 2 log groups `/aws/lambda/prm-regional/*`.
- **Artefatos:** `prm-teste/*` no bucket do SAM. Se a stack
  `aws-sam-cli-managed-default` não existir na região, o `--resolve-s3` a cria
  (bucket versionado); anote isso para o H3.

**Custo:** por uso, centavos. Nada cobra por hora. **Remove:** passo H.

O build da layer baixa o boto3 do PyPI (precisa de internet e de `pip`).

```bash
sam build --template-file aws_prm_tagging/infra/template-hub.yaml
```
```bash
sam deploy --stack-name prm-hub --region $HUB --profile $P --resolve-s3 --s3-prefix prm-teste \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides NotificationEmail=$EMAIL ExpectedTagValue=pc:testeprmsandbox \
    ModoTagueamentoInicial=dry-run ModoVarreduraRecorrente=desligado ContratoConfirmado=true \
    RegioesVarredura=$R
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

**ESPERE:** confirme a assinatura no e-mail da AWS.

Como `RegioesVarredura=us-east-1`, o tagueamento inicial (dry-run) manda
**um** e-mail só. Confira nele:

- `"versao_boto3": "1.43.113"` (prova que a Lambda usa a layer, não o SDK do runtime);
- `cliente_resumo`, `iac_amostra` e `estado_anterior_amostra`.

Se o e-mail chegar antes da confirmação, ele se perde; a varredura do passo F repete.

## E — Etapa 3 em tempo real (cada teste: rode, **ESPERE ~3 min**, confira)

### E1 — sobrescrever o valor de outro parceiro

**Cria:** fila SQS `prm-val-e1` (grátis vazia). **Remove:** H1.
```bash
aws sqs create-queue --queue-name prm-val-e1 --tags aws-apn-id=pc:outroparceiro --region $R --profile $P
```
Conferência. Esperado `aws-apn-id = pc:testeprmsandbox` e e-mail `tagueado_sucesso` com
`"acao": "sobrescrita"` e `"estado_anterior": {"aws-apn-id": "pc:outroparceiro", ...}`:
```bash
aws sqs list-queue-tags --queue-url $(aws sqs get-queue-url --queue-name prm-val-e1 --region $R --profile $P --query QueueUrl --output text) --region $R --profile $P
```

### E2 — chave de grafia parecida

**Cria:** tópico SNS `prm-val-e2` (grátis). **Remove:** H1.
```bash
aws sns create-topic --name prm-val-e2 --tags Key=AWS-APN-ID,Value=digitado-errado --region $R --profile $P --query TopicArn --output text
```
Conferência. Esperado: as **duas** chaves (`AWS-APN-ID` antiga e `aws-apn-id` nossa) e e-mail com
`"acao": "adicionada_com_tag_similar"`:
```bash
aws sns list-tags-for-resource --resource-arn arn:aws:sns:$R:$ACC:prm-val-e2 --region $R --profile $P
```

### E3 — "Terraform" pelo userAgent (sem instalar o Terraform)

O `user_agent_extra` do boto3 acrescenta `terraform-prm-teste` ao
`userAgent` que o CloudTrail grava. É o mesmo sinal que o Terraform deixa
(ele se identifica como `HashiCorp/... Terraform/...`).

**Cria:** fila `prm-val-e3` (grátis). **Remove:** H1.
```bash
python3 -c "import boto3; from botocore.config import Config; boto3.Session(profile_name='$P').client('sqs', region_name='$R', config=Config(user_agent_extra='terraform-prm-teste')).create_queue(QueueName='prm-val-e3'); print('ok')"
```
Conferência. Esperado: **sem** `aws-apn-id`, e e-mail `pulado_iac` com
`"iac": {"ferramenta": "Terraform", "evidencia": "CloudTrail userAgent: ... terraform-prm-teste ..."}`:
```bash
aws sqs list-queue-tags --queue-url $(aws sqs get-queue-url --queue-name prm-val-e3 --region $R --profile $P --query QueueUrl --output text) --region $R --profile $P
```

### E4 — drift causado por "IaC"

Remove a tag da fila do E1 como se fosse o Terraform:
```bash
python3 -c "import boto3; from botocore.config import Config; s=boto3.Session(profile_name='$P').client('sqs', region_name='$R', config=Config(user_agent_extra='terraform-prm-teste')); s.untag_queue(QueueUrl=s.get_queue_url(QueueName='prm-val-e1')['QueueUrl'], TagKeys=['aws-apn-id']); print('ok')"
```
**ESPERE ~5 min.** Esperado: e-mail `PRM Etapa 4-drift - drift_tag_removida` com
`"alterado_por_iac": {"ferramenta": "Terraform", ...}`, e a `acao` dizendo que o
cliente deve pôr a tag no código. A tag **não** volta (o drift só alerta).

## F — Varredura manual (dry-run, só leitura)

```bash
SMH=$(aws cloudformation describe-stack-resource --stack-name prm-hub --logical-resource-id TagueamentoInicialStateMachine \
      --region $HUB --profile $P --query StackResourceDetail.PhysicalResourceId --output text)
EXH=$(aws stepfunctions start-execution --state-machine-arn $SMH --region $HUB --profile $P \
      --input '{"modo":"dry-run","origem":"varredura_recorrente"}' --query executionArn --output text); echo $EXH
```
**ESPERE ~10 min.** Esperado `SUCCEEDED` e um e-mail de `us-east-1`, que deve
mostrar `prm-val-e3` em `iac_amostra`:
```bash
aws stepfunctions describe-execution --execution-arn $EXH --region $HUB --profile $P --query status --output text
```

## G — PARE AQUI e me mande

1. Tela e contagem do passo B.
2. Tabela final do passo C e o `resultado_live_cliente.md`.
3. O e-mail do passo D (com `versao_boto3`).
4. As conferências e os e-mails de E1 a E4.
5. O status e o e-mail do passo F.

---

## H — Limpeza (só depois do passo G)

**H1 — recursos de teste:**
```bash
for Q in prm-val-e1 prm-val-e3; do
  aws sqs delete-queue --queue-url $(aws sqs get-queue-url --queue-name $Q --region $R --profile $P --query QueueUrl --output text) --region $R --profile $P && echo "fila $Q removida"
done
aws sns delete-topic --topic-arn arn:aws:sns:$R:$ACC:prm-val-e2 --region $R --profile $P && echo "tópico removido"
```

**H2 — stacks.** Saem junto: roles, DLQ, regras, tópico, layers e os **log groups**.
```bash
sam delete --stack-name prm-regional --region $R --profile $P --no-prompts
sam delete --stack-name prm-hub --region $HUB --profile $P --no-prompts
```

**H3 — artefatos do SAM** (só o prefixo `prm-teste/`, com todas as versões):
```bash
BK=$(aws cloudformation describe-stack-resource --stack-name aws-sam-cli-managed-default --logical-resource-id SamCliSourceBucket \
     --region $R --profile $P --query StackResourceDetail.PhysicalResourceId --output text); echo $BK
aws s3api list-object-versions --bucket $BK --prefix prm-teste/ --profile $P --output text \
  --query '[Versions, DeleteMarkers][][].[Key, VersionId]' | while read KEY VID; do
    [ -n "$VID" ] && aws s3api delete-object --bucket $BK --key "$KEY" --version-id "$VID" --profile $P >/dev/null && echo "removido $KEY"
done
```

Se a stack `aws-sam-cli-managed-default` foi **criada nesta rodada** (passo D),
apague-a também depois do loop acima (o bucket precisa estar vazio, com todas
as versões removidas):
```bash
aws s3api list-object-versions --bucket $BK --profile $P --output text \
  --query '[Versions, DeleteMarkers][][].[Key, VersionId]' | while read KEY VID; do
    [ -n "$VID" ] && aws s3api delete-object --bucket $BK --key "$KEY" --version-id "$VID" --profile $P >/dev/null
done
aws cloudformation delete-stack --stack-name aws-sam-cli-managed-default --region $R --profile $P
```

**H4 — verificação final** (somente leitura). Esperado: nenhuma linha (ou só
`aws-sam-cli-managed-default`, se ela já existia antes da rodada).
```bash
aws cloudformation list-stacks --region $R --profile $P \
  --query "StackSummaries[?StackStatus!='DELETE_COMPLETE' && (starts_with(StackName,'prm-') || starts_with(StackName,'aws-sam-cli'))].[StackName,StackStatus]" --output text
aws lambda list-functions --region $R --profile $P --query "Functions[?starts_with(FunctionName,'prm-')].FunctionName" --output text
aws lambda list-layers --region $R --profile $P --query "Layers[?contains(LayerName,'PrmSdkLayer')].LayerName" --output text
aws logs describe-log-groups --region $R --profile $P --log-group-name-prefix /aws/lambda/prm- --query "logGroups[].logGroupName" --output text
aws sqs list-queues --region $R --profile $P --queue-name-prefix prm- --output text
aws sns list-topics --region $R --profile $P --query "Topics[?contains(TopicArn,'prm-')].TopicArn" --output text
aws events list-rules --region $R --profile $P --name-prefix prm- --query "Rules[].Name" --output text
aws iam list-roles --profile $P --query "Roles[?starts_with(RoleName,'prm-')].RoleName" --output text
aws s3api list-object-versions --bucket $BK --prefix prm-teste/ --profile $P --query '[Versions, DeleteMarkers][][].Key' --output text
ls -d saida-prm-* 2>/dev/null   # pastas locais com ARNs da conta: apague quando não precisar mais
```

---

## Inventário — tudo em us-east-1

| Recurso | Criado em | Removido em |
|---|---|---|
| `prm-val-<data>-*` do script (S3, SQS, SNS, DynamoDB, log group, ECR) | C | C (`limpar.sh`) |
| Stack `prm-hub`: tópico + assinatura, 3 Lambdas, layer, state machine, 3 roles, 3 log groups, Custom Resource | D | H2 |
| Stack `prm-regional`: 2 Lambdas, layer, state machine, 9 regras, DLQ, 3 roles, 2 log groups | D | H2 |
| `prm-teste/*` no bucket do SAM (e a stack `aws-sam-cli-managed-default`, se criada) | D | H3 |
| `prm-val-e1`, `prm-val-e2`, `prm-val-e3` | E | H1 |
| Pasta local `saida-prm-*` (no seu computador) | B, C | você |

Nenhum recurso com custo por hora.

---

## Opcionais (cada um com o que cria e o custo)

### O1 — instância EC2 do "Terraform" pelo histórico (evento nativo)

O evento nativo de EC2 não traz `userAgent`, então a automação procura o
`RunInstances` no histórico do CloudTrail, que leva até ~15 min para mostrar
o evento. Na sandbox há um detalhe a mais: o evento nativo não traz
principal, e o ARN da instância (`instance/i-...`) não contém `prm-val`. Por
isso o ID da instância entra no `FiltroEscopoTeste` logo depois de criada,
dentro do debounce (o filtro é lido na hora do processamento).

**Cria:** uma `t4g.nano` na VPC padrão de us-east-1 por ~20 min. **Custo:**
~US$ 0,002. **Remove:** `terminate-instances` no fim.

1) Refaça o deploy da regional com `DebounceSeconds=900` (o comando do passo D, trocando o 60).

2) Crie a instância como "Terraform":
```bash
AMI=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 --region $R --profile $P --query Parameter.Value --output text)
IID=$(python3 -c "import boto3; from botocore.config import Config; e=boto3.Session(profile_name='$P').client('ec2', region_name='$R', config=Config(user_agent_extra='terraform-prm-teste')); print(e.run_instances(ImageId='$AMI', InstanceType='t4g.nano', MinCount=1, MaxCount=1, TagSpecifications=[{'ResourceType':'instance','Tags':[{'Key':'Name','Value':'prm-val-o2'}]}])['Instances'][0]['InstanceId'])"); echo $IID
```

3) **Em até 10 min**, inclua o ID no filtro:
```bash
sam deploy --stack-name prm-regional --region $R --profile $P --resolve-s3 --s3-prefix prm-teste \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides ExpectedTagValue=pc:testeprmsandbox DebounceSeconds=900 HubRegion=$HUB \
    FiltroEscopoTeste="$EU,prm-val,$IID"
```

4) **ESPERE ~20 min** desde a criação. Esperado: e-mail `pulado_iac` com
`"evidencia": "RunInstances em ...: CloudTrail userAgent: ... terraform-prm-teste ..."`
e a instância **sem** `aws-apn-id`. O root volume herda a decisão (também
`pulado_iac`, sem a tag), porque nasceu da mesma chamada.

5) Remova:
```bash
aws ec2 terminate-instances --instance-ids $IID --region $R --profile $P --query 'TerminatingInstances[0].CurrentState.Name' --output text
```

### O2 — StackSets (precisa da conta payer de uma organização de teste)

A sandbox é conta-membro de uma organização que não é nossa, então o
StackSet não roda daqui. **Sem AWS**, dá para conferir o empacotamento:
```bash
python3 deploy/stacksets/empacotar.py --versao 2.0.0      # gera dist/stacksets/2.0.0 (local)
```
Com uma payer de teste, `docs/MVP-2.md` (seção StackSets) traz os comandos
`publicar`/`implantar`/`remover`, cada um com o que cria, o custo e como remover.

### O4 — Zabbix (quando o acesso sair)

Ver `docs/MVP-2.md`, seção Zabbix: importar o template, criar o token, guardar
no SSM e implantar `template-zabbix.yaml` apontando para o tópico da sandbox.
