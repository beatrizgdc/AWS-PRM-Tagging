# Validação na sandbox — Lote 4 (Etapa 4)

> **Histórico.** Este roteiro registra a validação de uma fase anterior do
> projeto. Nomes de arquivo podem ter mudado (ex.: `infra/template.yaml`
> hoje é `infra/template-regional.yaml` + `infra/template-hub.yaml`). O
> roteiro atual de validação é [`test/GUIA-SANDBOX.md`](../GUIA-SANDBOX.md).

Mesma regra dos lotes anteriores: quem executa é o responsável pelo
projeto; cada bloco diz **o que cria, custo e como remover**. Tudo aqui
foi validado antes **[simulado localmente]** (pytest + moto); nada foi
executado em conta AWS pelo assistente.

Pré-requisito: stacks `prm-hub` e `prm-regional` dos Lotes 2/3.

---

## D1 — `verificar-csv` (sem AWS, sem custo)

Só precisa de acesso à internet (lê a página pública do guia).

```bash
python3 -m aws_prm_tagging.main verificar-csv --output prm_verificacao_csv.json
```

Esperado hoje: código de saída `0` (CSV igual à página — 94 serviços).
**É o principal ponto a confirmar:** o parser foi testado contra HTML no
formato das tabelas da documentação AWS, mas não contra a página real
(meu ambiente não alcança `docs.aws.amazon.com`). Se sair `1` com "tabela
não encontrada", me mande o HTML salvo da página.

---

## D2 — Atualizar as stacks

| Stack | O que muda | Custo |
|---|---|---|
| `prm-regional` (`us-east-1`, `us-east-2`) | + Lambda e regra de drift; DLQ passa a se chamar `prm-etapa3-dlq` (**a fila é recriada** — se houver mensagem na DLQ antiga, ela se perde) | free tier |
| `prm-hub` (`us-east-1`) | + EventBridge Scheduler + role do Scheduler | free tier |

```bash
sam build --template-file aws_prm_tagging/infra/template-regional.yaml
for R in us-east-1 us-east-2; do
  sam deploy --stack-name prm-regional --region $R --profile <sandbox> --resolve-s3 --capabilities CAPABILITY_IAM \
    --parameter-overrides ExpectedTagValue=pc:testeprmsandbox DebounceSeconds=60 HubRegion=us-east-1
done

sam build --template-file aws_prm_tagging/infra/template-hub.yaml
sam deploy --stack-name prm-hub --region us-east-1 --profile <sandbox> --resolve-s3 --capabilities CAPABILITY_IAM \
  --parameter-overrides NotificationEmail=<seu-email> ExpectedTagValue=pc:testeprmsandbox \
    ModoTagueamentoInicial=desligado ModoVarreduraRecorrente=dry-run AgendaVarredura="rate(7 days)" \
    ContratoConfirmado=true
```

`ModoTagueamentoInicial=desligado` evita rodar o inicial de novo;
`rate(7 days)` evita e-mail diário na sandbox; `ContratoConfirmado=true`
para enxergar o filtro "só publica se houver novidade" (com `false`, toda
região publicaria — por design).

> Na sandbox compartilhada, **`ModoVarreduraRecorrente` sempre `dry-run`**:
> em `live` a varredura reaplica a tag em recursos de outras pessoas.

---

## D3 — Drift quase em tempo real

Cria 1 bucket S3 vazio (custo zero). Ordem:

```bash
B=prm-val-drift-$(date +%s)
aws s3api create-bucket --bucket $B --region us-east-1 --profile <sandbox>
# ~2 min: a Etapa 3 tagueia (e-mail tagueado_sucesso)
aws s3api get-bucket-tagging --bucket $B --profile <sandbox>
# remover TODAS as tags (simula alguém apagando a tag)
aws s3api delete-bucket-tagging --bucket $B --profile <sandbox>
```

Esperado em poucos minutos (latência do CloudTrail): e-mail `PRM Etapa
4-drift - drift_tag_removida` com o ARN do bucket e o `principal` (o seu
usuário/role). **Nenhuma** tag é reaplicada automaticamente.

Negativo (não deve alertar): `aws s3api put-bucket-tagging --bucket $B
--tagging 'TagSet=[{Key=aws-apn-id,Value=pc:testeprmsandbox}]'` (a
releitura encontra a tag correta).

Remover: `aws s3api delete-bucket --bucket $B --profile <sandbox>`

---

## D4 — Varredura recorrente, execução manual (dry-run)

Sem esperar o agendamento:

```bash
SM=$(aws cloudformation describe-stack-resource --stack-name prm-hub --logical-resource-id TagueamentoInicialStateMachine \
      --region us-east-1 --profile <sandbox> --query StackResourceDetail.PhysicalResourceId --output text)
aws stepfunctions start-execution --state-machine-arn $SM --region us-east-1 --profile <sandbox> \
  --input '{"modo":"dry-run","origem":"varredura_recorrente"}'
```

Só leitura em todas as regiões ativas; nenhuma escrita. Esperado:

- `us-east-1`/`us-east-2` (com stack regional): e-mail **só** se houver
  novidade (recurso a taguear, conflito, falha, mensagem na DLQ).
- Regiões ativas **sem** stack regional: e-mail com
  `stack_regional_detectada: false` — é o sinal de lacuna de cobertura
  (na sandbox, todas as regiões além das duas acima vão aparecer).

Nada a remover (a execução não cria recursos).

---

## D5 — `auditar-org` (só com credencial da conta de gerenciamento)

Somente leitura (`organizations:ListPolicies`, `DescribePolicy`,
`ListTargetsForPolicy`). **Depende da aprovação do cliente** para rodar na
payer dele; na Darede, pode rodar na payer da própria organização da
sandbox, se houver acesso.

```bash
python3 -m aws_prm_tagging.main auditar-org --profile <payer> \
  --expected-tag-value pc:testeprmsandbox --output prm_auditoria_org.json
```

Saída `2` = há algo para revisão humana (SCP negando ações de tag, tag
policy com capitalização/valor incompatível ou com `enforced_for`). Se a
sandbox tem a SCP de restrição de região vista nos testes de setembro, ela
deve aparecer como `restringe_regioes`.

---

## Remoção completa

Igual aos lotes anteriores (`sam delete` das stacks). O Scheduler e a
regra de drift saem junto.
