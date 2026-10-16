# Validação na sandbox — Lote 3

> **Histórico.** Este roteiro registra a validação de uma fase anterior do
> projeto. Nomes de arquivo podem ter mudado (ex.: `infra/template.yaml`
> hoje é `infra/template-regional.yaml` + `infra/template-hub.yaml`). O
> roteiro atual de validação é [`test/GUIA-SANDBOX.md`](../GUIA-SANDBOX.md).

Mesma regra dos lotes anteriores: quem executa é o responsável pelo
projeto, e cada bloco diz **o que cria, custo e como remover**. Tudo aqui
foi validado antes **[simulado localmente]** (pytest + moto); nada foi
executado em conta AWS pelo assistente.

Pré-requisito: Parte B do Lote 2 (stacks `prm-hub` e `prm-regional`) —
este lote atualiza o `prm-hub`.

---

## C1 — Recomendações de tag na origem (somente leitura, sem custo)

```bash
python3 -m aws_prm_tagging.main recomendar --profile <sandbox> \
  --expected-tag-value pc:testeprmsandbox --regioes us-east-1 \
  --output prm_recomendacoes.json --markdown prm_recomendacoes.md
```

Não cria nada. Lê EKS (node groups/launch templates, nodes do Karpenter,
LBs do Load Balancer Controller, volumes do EBS CSI), services ECS
(`propagateTags`), templates de replicação do DRS e billing groups do IoT.
O `.md` é o formato para enviar ao cliente. Se a sandbox não tiver nada
disso, o resultado é "nenhuma recomendação" — esperado.

---

## C2 — Tagueamento inicial (item 8), **somente em dry-run**

> **Atenção:** em modo `live`, o tagueamento inicial aplica a tag em
> **todos** os recursos sem tag da conta, em todas as regiões. Numa conta
> compartilhada isso alcança recursos de outras pessoas. Na sandbox use
> **apenas `dry-run`**. O modo `live` foi validado [simulado localmente] e
> deve ser usado só em conta dedicada ou de cliente, com consentimento.

### O que cria / altera

Update da stack `prm-hub` (`us-east-1`): 3 funções Lambda, 1 state machine,
roles IAM e o Custom Resource. Custo: free tier (1 execução, uma Lambda por
região ativa). Leitura em todas as regiões ativas; **nenhuma escrita** de
tag em dry-run.

```bash
sam build --template-file aws_prm_tagging/infra/template-hub.yaml
sam deploy --stack-name prm-hub --region us-east-1 --profile <sandbox> \
  --resolve-s3 --capabilities CAPABILITY_IAM \
  --parameter-overrides NotificationEmail=<seu-email> \
    ExpectedTagValue=pc:testeprmsandbox ModoTagueamentoInicial=dry-run
```

### O que observar

- Um e-mail por região ativa, assunto `PRM Etapa tagueamento_inicial -
  resumo <regiao> (dry-run)`, com `por_categoria` (o que seria feito) e
  `total_descobertos_so_via_api_nativa`.
- Output `TagueamentoInicialExecutionArn` da stack → abrir no console do
  Step Functions; todas as iterações do Map em verde.
- Região que estourar os 15 min da Lambda → e-mail `FALHA em uma regiao`
  (a execução segue para as outras). Anotar se acontecer.
- Confirmar que **nenhuma** tag foi escrita: o CloudTrail não deve ter
  `TagResources`/`TagResource` da role `...TagueamentoInicialRole...`.

### Rodar de novo

Mudar `ReexecutarTagueamentoInicial` (ex.: `=2`) no próximo `sam deploy`.
Trocar só o modo **não** dispara nova execução — de propósito.

### Remover

Volta a ser removido com o `sam delete --stack-name prm-hub` do Lote 2.

---

## C3 — Item 10: serviços novos na Etapa 3 (gratuitos)

| # | Criar (região com a stack regional) | Esperado em ~2 min | Remover |
|---|---|---|---|
| C3a | `aws iot create-billing-group --billing-group-name prm-val-bg` | billing group com `aws-apn-id` | `aws iot delete-billing-group --billing-group-name prm-val-bg` |
| C3b | `aws iotsitewise create-asset-model --asset-model-name prm-val-model` | asset model com `aws-apn-id` | `aws iotsitewise delete-asset-model --asset-model-id <id>` |

Custo: zero (sem dados ingeridos). Valida o `eventSource`/campos reais de
IoT e SiteWise, que até aqui só foram conferidos contra o modelo do SDK.

---

## C4 — Item 14: task ECS Fargate (**com custo — autorizar antes**)

Custo: 1 task Fargate de 0,25 vCPU / 0,5 GB ≈ US$ 0,01 por hora; parar em
seguida. Cria: 1 cluster, 1 task definition, 1 task.

```bash
aws ecs create-cluster --cluster-name prm-val
aws ecs register-task-definition --family prm-val --requires-compatibilities FARGATE \
  --network-mode awsvpc --cpu 256 --memory 512 \
  --container-definitions '[{"name":"c","image":"public.ecr.aws/docker/library/busybox:latest","command":["sleep","600"]}]'
aws ecs run-task --cluster prm-val --task-definition prm-val --launch-type FARGATE \
  --network-configuration 'awsvpcConfiguration={subnets=[<subnet-publica>],assignPublicIp=ENABLED}'
```

Esperado **sem esperar o debounce** (segundos, não 30 min): a task recebe
`aws-apn-id` e chega um e-mail `tagueado_sucesso`.

Remover:

```bash
aws ecs stop-task --cluster prm-val --task <task-arn>
aws ecs deregister-task-definition --task-definition prm-val:1
aws ecs delete-cluster --cluster prm-val
```
