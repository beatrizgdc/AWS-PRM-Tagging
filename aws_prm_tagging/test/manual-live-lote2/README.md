# Validação na sandbox — Lote 2

> **Histórico.** As Partes 0 e B deste roteiro foram substituídas pelo guia
> consolidado; a Parte A (script `validar_etapa2c.sh`) continua valendo e é
> usada pelo guia. Nomes de arquivo podem ter mudado (ex.: `infra/template.yaml`
> hoje é `infra/template-regional.yaml` + `infra/template-hub.yaml`). O
> roteiro atual de validação é [`test/GUIA-SANDBOX-MVP2.md`](../GUIA-SANDBOX-MVP2.md). No MVP2 o script espera a regra nova: `sobrescrito`, `adicionado_com_similar` e dois buckets `pulado_iac` (Terraform e Pulumi).

Roteiro para rodar **na conta sandbox compartilhada**. Quem executa é o
responsável pelo projeto; cada bloco que cria ou altera algo diz **o que
cria, custo aproximado e como remover**. Tudo aqui foi ensaiado antes
contra AWS simulada (moto) — ver "O que já foi validado localmente".

Rodar a partir do diretório **pai** de `aws_prm_tagging/`, com um perfil
AWS que aponte para a sandbox. Nada aqui usa conta de cliente.

---

## Parte 0 — Preflight (somente leitura, sem custo)

```bash
python3 -m aws_prm_tagging.main preflight --profile <sandbox> --output prm_preflight.json
```

Lista as regiões para o StackSet (ativas + `us-east-1` sempre) e diz, por
região, se existe uma trail registrando eventos de escrita. Código de
saída `2` = há região sem trail (as regras de CloudTrail da Etapa 3 não
funcionariam lá; a regra nativa de EC2 funciona). **Anote o resultado**:
se não houver trail, a Parte B só valida a regra nativa de EC2 e a DLQ.

---

## Parte A — Etapa 1 (descoberta nativa) + Etapa 2c (apply --live)

Valida os itens 1 e 4: recurso **sem nenhuma tag** é descoberto e
tagueado; conflito, tag similar, IaC e "já ok" são respeitados; e a tag é
lida de volta na AWS para confirmar.

### O que cria

Prefixo único por execução: `prm-val-<timestamp>`. Região default
`us-east-1` (`--regiao` muda).

| Recurso | Cenário | Custo |
|---|---|---|
| 4 buckets S3 (vazios) | sem tag / conflito (`pc:outroparceiro`) / já ok / `managed-by=terraform` | zero (sem objetos) |
| 2 filas SQS | sem tag / tag parecida `AWS-APN-ID` | zero (sem mensagens) |
| 1 tópico SNS | sem tag | zero |
| 1 tabela DynamoDB on-demand | sem tag | zero (vazia, sem requisições) |
| 1 log group | sem tag | zero (sem ingestão) |
| 1 repositório ECR | sem tag | zero (sem imagens) |
| 1 função Lambda (**só** com `--lambda-role-arn`) | sem tag | zero (sem invocação) |

**Escritas feitas pela automação**: `aws-apn-id` só nos recursos acima
(`apply --live --somente-arns manifest.tsv`). A varredura (`map`) lê a
região inteira da conta, mas **só lê** — nenhum recurso de outra pessoa é
tocado. Isso foi testado localmente com um bucket "alheio" sem tag.

### Executar

```bash
aws_prm_tagging/test/manual-live-lote2/validar_etapa2c.sh \
  --profile <sandbox> --valor pc:testeprmsandbox [--regiao us-east-1] \
  [--lambda-role-arn arn:aws:iam::<conta>:role/<role-existente-com-trust-lambda>]
```

Use um valor de teste (ex.: `pc:testeprmsandbox`), não o product code real.

A saída termina com uma tabela `OK`/`ERRO` por recurso e grava tudo em
`saida-prm-val-<timestamp>/` (inclui ARNs da conta — já está no
`.gitignore`).

### Remover

```bash
aws_prm_tagging/test/manual-live-lote2/limpar.sh saida-prm-val-<timestamp>/manifest.tsv
```

O `manifest.tsv` tem, por recurso, o comando exato de remoção. Se a
execução parar no meio, a mensagem de erro já mostra o comando de limpeza
com o manifest parcial.

### O que observar

- Coluna `etapa1 = nativa` nos recursos sem tag → item 1 funcionando.
- Qualquer `falhou` com `AccessDenied` → falta permissão de escrita nativa
  do serviço na role usada (lista em `docs/producao.md`, seção da Etapa 2c).
- SQS e ECR aparecem como `falhou` no ensaio com moto (limitação do
  simulador: "Service not yet supported"); na AWS real devem ser `OK`.
  **É o principal ponto a confirmar nesta parte.**

---

## Parte B — Etapa 3 multi-região (hub + regional), DLQ e correções do Lote 1

### O que cria

| Stack | Região | Recursos | Custo |
|---|---|---|---|
| `prm-hub` | `us-east-1` | tópico SNS + assinatura de e-mail | zero |
| `prm-regional` | `us-east-1` | Lambda, Step Functions, 6 regras EventBridge, 2 roles IAM (+1 da Lambda), fila SQS (DLQ) | free tier (execuções pontuais) |
| `prm-regional` | `us-east-2` | idem | idem |
| bucket de artefatos do SAM | cada região | `sam deploy --resolve-s3` cria a stack `aws-sam-cli-managed-default` com 1 bucket, **se ainda não existir** | centavos (armazenamento do zip) |

Para testar em tempo razoável: `DebounceSeconds=60` (o default de
produção é 1800).

### Deploy

```bash
# do diretório pai de aws_prm_tagging/ (onde está o Makefile)
sam deploy --template-file aws_prm_tagging/infra/template-hub.yaml \
  --stack-name prm-hub --region us-east-1 --profile <sandbox> \
  --parameter-overrides NotificationEmail=<seu-email>

sam build --template-file aws_prm_tagging/infra/template-regional.yaml
for R in us-east-1 us-east-2; do
  sam deploy --stack-name prm-regional --region $R --profile <sandbox> \
    --resolve-s3 --capabilities CAPABILITY_IAM \
    --parameter-overrides ExpectedTagValue=pc:testeprmsandbox DebounceSeconds=60 HubRegion=us-east-1
done
```

Confirme a assinatura pelo e-mail que a AWS envia.

### Testes (cada um cria 1 recurso gratuito; remover no fim)

| # | O que valida | Criar (região) | Esperado em ~2 min |
|---|---|---|---|
| B1 | **A1** — Lambda (`CreateFunction20150331`) + **A2** (`functionArn` camelCase) | `aws lambda create-function` com role existente, em `us-east-2` | função com `aws-apn-id`; e-mail `tagueado_sucesso` |
| B2 | **A2** — DynamoDB (`tableDescription.tableArn`) | `aws dynamodb create-table ... --billing-mode PAY_PER_REQUEST`, em `us-east-2` | tabela tagueada |
| B3 | **Item 7** — região ≠ hub publica no tópico do hub | (B1/B2 já cobrem: rodaram em `us-east-2`) | e-mails chegam do tópico de `us-east-1` |
| B4 | **Item 9** — DLQ pega falha de processamento | `aws stepfunctions start-execution --state-machine-arn <arn da região> --input '{"detail":"forcar-erro"}'` | execução termina em `FalhaRegistradaNaDlq`; 1 mensagem na DLQ (`aws sqs receive-message --queue-url <Etapa3EventsDlqUrl>`) |
| B5 | Condição `aws:TagKeys` não bloqueia a escrita real | (B1/B2 já cobrem — se a tag chegou, a condição não bloqueou) | — |

Opcional, **com custo** (autorize antes): B6 — regra nativa de EC2 +
expansão de volume root: lançar 1 `t3.micro` (free tier se a conta for
elegível; senão ~US$0,01/h) e confirmar tag na instância **e** no volume
root; terminar a instância em seguida.

### Remover

```bash
# recursos de teste criados em B1/B2
aws lambda delete-function --function-name <nome> --region us-east-2 --profile <sandbox>
aws dynamodb delete-table --table-name <nome> --region us-east-2 --profile <sandbox>
# stacks (a DLQ é apagada junto)
for R in us-east-1 us-east-2; do sam delete --stack-name prm-regional --region $R --profile <sandbox> --no-prompts; done
sam delete --stack-name prm-hub --region us-east-1 --profile <sandbox> --no-prompts
```

A stack `aws-sam-cli-managed-default` (se foi criada agora) pode ficar —
é reaproveitada por qualquer `sam deploy` — ou ser removida esvaziando o
bucket e apagando a stack.

---

## O que já foi validado localmente (sem conta)

- `validar_etapa2c.sh` rodou de ponta a ponta contra moto server: criação,
  map (com descoberta nativa), decide, dry-run, live, verificação,
  limpeza. 8 de 10 recursos `OK`; SQS/ECR `falhou` só por limitação do
  simulador. Bucket "de outra pessoa" sem tag **não** foi tocado. O trap
  de erro e o `limpar.sh` também foram exercitados numa execução
  interrompida de propósito.
- Consistência de ARN da descoberta nativa × Tagging API para EC2, EBS,
  snapshot, S3, RDS, DynamoDB, Lambda, SNS, Logs, Secrets Manager, KMS,
  Step Functions, ECS e EFS (`test_native_discovery_moto.py`).
- Templates: `cfn-lint` sem erros nos dois; transform SAM do regional
  gerando a permissão `sqs:SendMessage` só para a DLQ da região.
