# Execução em produção (contas cliente)

Este documento cobre como executar as Etapas 1, 2a, 2b e 2c (`map`,
`decide`, `apply` sem/com `--live`) contra uma conta AWS de cliente real —
hoje via CLI local —, e o que muda quando essas etapas (e as próximas,
Etapa 3/4) forem empacotadas como Lambda.

## Modelo de acesso

Cada conta cliente roda sua própria execução da automação — não há acesso
cross-account nesta arquitetura. Isso vale tanto para este script (execução
local/CLI) quanto para as Etapas futuras (Lambda dentro de uma stack
CloudFormation implantada na própria conta do cliente).

## Credenciais

Sem arquivo `.env` — ver justificativa em
[arquitetura.md](arquitetura.md#por-que-não-há-arquivo-de-variáveis-de-ambiente).
Formas suportadas nativamente pelo boto3, em ordem de preferência para
produção:

1. **Perfil nomeado com role assumption** (recomendado quando um operador
   humano executa o script pontualmente). Em `~/.aws/config` do cliente ou do
   operador autorizado:

   ```ini
   [profile cliente-x-prm-readonly]
   role_arn = arn:aws:iam::<ACCOUNT_ID_CLIENTE>:role/PRMResourceMappingReadOnly
   source_profile = default
   region = us-east-1
   ```

   Execução: `--profile cliente-x-prm-readonly`. O boto3 assume a role e
   renova as credenciais temporárias automaticamente, sem nenhuma alteração
   de código.

2. **IAM role anexada ao ambiente de execução** (recomendado para execução
   agendada/automatizada): instance profile de EC2, task role de
   ECS/Fargate, ou — nas próximas Etapas — execution role de Lambda. Nesse
   caso `--profile` é omitido; o boto3 resolve a role automaticamente.

3. **Variáveis de ambiente temporárias** (`AWS_ACCESS_KEY_ID`,
   `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`) geradas por
   `aws sts assume-role`, para uso em pipelines de CI que não suportam
   instance profile.

Nunca commitar credenciais de longa duração no repositório ou em arquivos de
configuração versionados.

## Permissões IAM necessárias (somente leitura)

A role/usuário usado para executar o script precisa apenas de ações
`Describe*`/`List*`/`Get*` — nenhuma ação de escrita. Política mínima:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PRMMappingReadOnly",
      "Effect": "Allow",
      "Action": [
        "sts:GetCallerIdentity",
        "ec2:DescribeRegions",
        "ec2:DescribeInstances",
        "ec2:DescribeVolumes",
        "tag:GetResources",
        "bedrock:ListInferenceProfiles",
        "bedrock:ListTagsForResource",
        "eks:ListClusters",
        "eks:DescribeCluster",
        "eks:ListNodegroups",
        "eks:DescribeNodegroup",
        "autoscaling:DescribeAutoScalingGroups",
        "elasticloadbalancing:DescribeLoadBalancers",
        "elasticloadbalancing:DescribeTags"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PRMMappingOrgTreeReadOnly",
      "Effect": "Allow",
      "Action": [
        "organizations:DescribeOrganization",
        "organizations:ListRoots",
        "organizations:ListOrganizationalUnitsForParent",
        "organizations:ListAccountsForParent"
      ],
      "Resource": "*"
    }
  ]
}
```

O segundo bloco (`organizations:*`) só é necessário quando a execução roda a
partir da conta de gerenciamento de uma Organization e a árvore de OUs é
desejada. Em uma conta-membro, essas chamadas falham com
`AccessDeniedException` ou `AWSOrganizationsNotInUseException` e o script
prossegue normalmente sem a árvore (ver `ou_tree.py`) — a permissão pode ser
omitida em contas-membro sem quebrar a execução.

## Permissões IAM para a Etapa 2a (`decide`)

Nenhuma. `decision.py` é uma função pura — não instancia sessão boto3, não
faz nenhuma chamada de API. O subcomando `decide` só lê o JSON da Etapa 1 do
disco e escreve o relatório de decisão, também no disco.

## Permissões IAM para a Etapa 2b (`apply`, dry-run — sem `--live`)

O subcomando `apply` sem `--live` (`tag_execution.run_tagging_execution`
com `dry_run=True`, o default) nunca chama uma API de escrita — o
`DryRunExecutor` só loga a ação que seria tomada. As únicas chamadas reais
que a Etapa 2b faz são de **leitura**, para a revalidação do estado atual
de cada recurso (tag E status de IaC) imediatamente antes de decidir se
simula ou pula cada um (ver [arquitetura.md](arquitetura.md#tag_executionpy);
pode ser desligada com `--no-revalidate`, mas então o relatório de dry-run
deixa de refletir mudanças feitas na conta depois da Etapa 1/2a).

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PRMStage2bRevalidationReadOnly",
      "Effect": "Allow",
      "Action": [
        "tag:GetResources",
        "eks:ListTagsForResource",
        "bedrock:ListTagsForResource",
        "elasticloadbalancing:DescribeTags"
      ],
      "Resource": "*"
    }
  ]
}
```

Com `--no-revalidate`, nem essa política é necessária — o subcomando `apply`
não faz nenhuma chamada AWS.

## Permissões IAM para a Etapa 2c (`apply --live`, execução real)

`LiveExecutor` está implementado, mas **a lista abaixo ainda não deve ser
anexada a uma role de cliente real** até o item "isso não é suficiente
sozinho" logo abaixo ser resolvido (validação das ~80 permissões nativas
por serviço) e até a Etapa 2c ter sido exercitada contra a conta sandbox
(ver "Estratégia de testes" no README/docs de arquitetura). Deve viver numa
política **separada** da política de leitura da Etapa 2b acima, nunca
anexada à mesma role usada para descoberta/dry-run — escrita é uma
superfície de risco diferente de leitura.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PRMStage2cGenericTagWrite",
      "Effect": "Allow",
      "Action": ["tag:TagResources"],
      "Resource": "*"
    },
    {
      "Sid": "PRMStage2cDedicatedTagWrite",
      "Effect": "Allow",
      "Action": [
        "eks:TagResource",
        "bedrock:TagResource",
        "elasticloadbalancing:AddTags"
      ],
      "Resource": "*"
    }
  ]
}
```

**Isso não é suficiente sozinho.** A AWS documenta explicitamente que
`tag:TagResources` exige, além de si mesma, a permissão de tagging nativa
de cada serviço dono do recurso — ex., para taguear uma instância EC2 via
`tag:TagResources`, é preciso `tag:TagResources` **e** `ec2:CreateTags`
([referência](https://docs.aws.amazon.com/resourcegroupstagging/latest/APIReference/API_TagResources.html)).
Como o escopo cobre ~80 serviços do CSV oficial, a política real da Etapa 2c
precisa de uma ação de tagging nativa por serviço (`ec2:CreateTags`,
`lambda:TagResource`, `s3:PutBucketTagging`, `rds:AddTagsToResource` etc.).
Essa lista já foi levantada e confirmada em sandbox para a Etapa 3
(`PrmEtapa3NativeTagWrite` em
[infra/template.yaml](../infra/template.yaml), documentada em
[infra/README.md](../infra/README.md#permissões-iam--cobertura-real) e
[melhorias-futuras.md](melhorias-futuras.md#permissões-iam-nativas--resolvido-69-de-69-serviços-mapeados-cobertos))
e serve de ponto de partida direto para a Etapa 2c, já que ambas cobrem o
mesmo conjunto de 69 serviços mapeados e a mesma ação nativa por serviço —
ainda assim, monte/revise essa política especificamente para a Etapa 2c (e
teste contra a conta sandbox) antes de usá-la em produção, em vez de
reaproveitar `infra/template.yaml` diretamente.

## Passo a passo

1. Confirmar o valor esperado da tag para o cliente/produto/OU em questão
   (nunca reaproveitar um valor de outra conta).
2. Configurar o perfil de credenciais (seção acima).
3. Rodar:

   ```bash
   python3 -m aws_prm_tagging.main map \
     --expected-tag-value pc:<PRODUCT_CODE_DO_CLIENTE> \
     --profile cliente-x-prm-readonly \
     --output relatorio-<cliente>-<data>.json
   ```

4. Revisar o log de execução (nível INFO por padrão) — regiões processadas,
   contagem de recursos por região, e qualquer `WARNING`/`ERROR` de
   permissão ou throttling persistente.
5. Tratar o JSON de saída como dado interno sensível: contém ARNs, estrutura
   de contas/OUs e status de tagging da conta do cliente. Não publicar nem
   anexar a tickets externos sem necessidade.
6. Antes de reportar o número de `sem_tag` como definitivo para o cliente,
   lembrar da limitação conhecida: `GetResources` não enxerga recursos que
   nunca receberam tag nenhuma (nem `aws-apn-id`, nem qualquer outra) — o
   `total_recursos`/`sem_tag` do relatório é um piso, não necessariamente o
   universo completo. Detalhe em
   [arquitetura.md](arquitetura.md#resource_discoverypy).
7. Rodar a Etapa 2a (decisão) sobre o relatório da Etapa 1 — não usa boto3,
   sem permissão IAM adicional:

   ```bash
   python3 -m aws_prm_tagging.main decide \
     --input relatorio-<cliente>-<data>.json \
     --expected-tag-value pc:<PRODUCT_CODE_DO_CLIENTE> \
     --output decisao-<cliente>-<data>.json
   ```

8. Rodar a Etapa 2b (dry-run) sobre o relatório de decisão — revisar o
   `resumo.por_categoria_final` antes de sequer considerar `--live` (ver
   ["Permissões IAM para a Etapa 2b"](#permissões-iam-para-a-etapa-2b-apply-dry-run--sem---live)
   acima):

   ```bash
   python3 -m aws_prm_tagging.main apply \
     --input decisao-<cliente>-<data>.json \
     --profile cliente-x-prm-readonly \
     --output dry-run-<cliente>-<data>.json
   ```

9. Etapa 2c (`apply --live`, execução real): **não rodar contra conta de
   cliente ainda** — ver as ressalvas em
   ["Permissões IAM para a Etapa 2c"](#permissões-iam-para-a-etapa-2c-apply---live-execução-real)
   acima (lista de permissões nativas por serviço não validada, nenhum
   teste contra sandbox realizado até agora).

## Escala e tempo de execução

O tempo de execução escala com o número de regiões ativas e o volume de
recursos taggeáveis na conta (a Resource Groups Tagging API pagina 100
recursos por chamada). Em contas grandes:

- O `retry.py` já absorve throttling pontual com backoff exponencial: não é
  necessário reduzir a paralelização manualmente porque a execução é
  sequencial por região (sem paralelismo entre regiões, de propósito, para
  não somar limites de taxa entre serviços diferentes na mesma conta).
- Se uma conta tiver regiões que claramente não interessam (ex.: nunca usada
  fora de `us-east-1`/`sa-east-1`), considerar restringir a lista de regiões
  processadas antes de rodar em larga escala — hoje o script sempre varre
  todas as regiões comerciais ativas da conta; um filtro de região por CLI é
  um candidato natural de melhoria para as próximas Etapas, não implementado
  aqui para manter o escopo da Etapa 1 fiel ao pedido original.

## Caminho para as próximas Etapas (Lambda + CloudFormation StackSets)

A distribuição para os clientes da EMPRESAX foi decidida via **CloudFormation
StackSets com service-managed permissions**, integrada ao AWS Organizations
de cada cliente — uma stack autocontida por conta, sem acesso cross-account
de leitura. Este documento cobre só a execução da Etapa 1 contra uma
conta; a camada de orquestração multi-cliente (trusted access, targeting por
sub-OU, modelo de reporte por push, dashboard) está em
[arquitetura-multicliente.md](arquitetura-multicliente.md).

Os módulos em `aws_prm_tagging/` (exceto `main.py`, que é só o CLI) são
puros o suficiente para serem importados diretamente por um handler Lambda
— incluindo `decision.py` (Etapa 2a) e `tag_execution.py` (Etapas 2b/2c,
que já faz chamadas reais de leitura/escrita, mas sem I/O de arquivo):

```python
from aws_prm_tagging import (
    regions, resource_discovery, services, tag_status, iac_detection,
    report, decision, tag_execution,
)

def handler(event, context):
    session = boto3.Session()  # usa a execution role da própria Lambda
    ...
    # tag_execution.run_tagging_execution(decision_report, session=session,
    #     expected_tag_value=..., dry_run=False)  # Etapa 2c
```

Pontos a considerar ao empacotar como Lambda:

- **Timeout**: a varredura completa de todas as regiões pode facilmente
  ultrapassar os 15 minutos máximos de uma execução Lambda em contas
  grandes. As Etapas de automação contínua e varredura recorrente devem
  paralelizar por região (ex.: uma invocação de Lambda por região, orquestrada
  por Step Functions ou EventBridge, reaproveitando `discover_generic_resources`,
  `discover_bedrock_resources` e `discover_eks_resources` como estão) em vez
  de repetir o loop sequencial de `main.py`.
- **Permissões**: a execution role da Lambda substitui o `--profile` local —
  aplicar a mesma política de somente-leitura acima (mais as permissões de
  escrita necessárias nas Etapas 2+, que devem ficar em uma política
  separada, nunca na mesma role usada para descoberta).
- **CloudFormation StackSets**: o template implantado por StackSet em cada
  conta deve provisionar a execution role local, a própria função Lambda com
  o pacote `aws_prm_tagging` (ou uma Lambda Layer compartilhada entre as
  functions das 4 Etapas, já que o código de descoberta é o mesmo), e o
  gatilho apropriado por Etapa (EventBridge Scheduler para a varredura
  recorrente; EventBridge rule de `CreateTags`/`RunInstances` etc., ou AWS
  Config, para a automação contínua de novos recursos). O parâmetro
  `--expected-tag-value` do CLI vira um **StackSet parameter** por
  instância (por sub-OU/contrato) — sem lógica condicional no código, só
  configuração de infraestrutura. Detalhe completo do modelo de deploy em
  [arquitetura-multicliente.md](arquitetura-multicliente.md).
- **Saída**: em vez de um arquivo JSON local, o handler deve enviar o
  relatório (ou um resumo/eventos derivados dele) para o destino central de
  monitoramento por **push** — sem leitura cross-account da conta do
  cliente pela EMPRESAX. Ver "Dashboard central" em
  [arquitetura-multicliente.md](arquitetura-multicliente.md).
