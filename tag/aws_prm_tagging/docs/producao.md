# Execução em produção (contas cliente)

Este documento cobre como executar o estágio 1 (mapeamento) contra uma conta
AWS de cliente real, e o que muda quando os estágios seguintes forem
empacotados como Lambda.

## Modelo de acesso

Cada conta cliente roda sua própria execução da automação — não há acesso
cross-account nesta arquitetura. Isso vale tanto para este script (execução
local/CLI) quanto para os estágios futuros (Lambda dentro de uma stack
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
   ECS/Fargate, ou — nos próximos estágios — execution role de Lambda. Nesse
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

## Passo a passo

1. Confirmar o valor esperado da tag para o cliente/produto/OU em questão
   (nunca reaproveitar um valor de outra conta).
2. Configurar o perfil de credenciais (seção acima).
3. Rodar:

   ```bash
   python3 -m aws_prm_tagging.main \
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
  um candidato natural de melhoria para os próximos estágios, não implementado
  aqui para manter o escopo do estágio 1 fiel ao pedido original.

## Caminho para os próximos estágios (Lambda + CloudFormation StackSets)

A distribuição para os clientes da EMPRESAX foi decidida via **CloudFormation
StackSets com service-managed permissions**, integrada ao AWS Organizations
de cada cliente — uma stack autocontida por conta, sem acesso cross-account
de leitura. Este documento cobre só a execução do estágio 1 contra uma
conta; a camada de orquestração multi-cliente (trusted access, targeting por
sub-OU, modelo de reporte por push, dashboard) está em
[arquitetura-multicliente.md](arquitetura-multicliente.md).

Os módulos em `aws_prm_tagging/` (exceto `main.py`, que é só o CLI) são
puros o suficiente para serem importados diretamente por um handler Lambda:

```python
from aws_prm_tagging import regions, resource_discovery, services, tag_status, iac_detection, report

def handler(event, context):
    session = boto3.Session()  # usa a execution role da própria Lambda
    ...
```

Pontos a considerar ao empacotar como Lambda:

- **Timeout**: a varredura completa de todas as regiões pode facilmente
  ultrapassar os 15 minutos máximos de uma execução Lambda em contas
  grandes. Os estágios de automação contínua e varredura recorrente devem
  paralelizar por região (ex.: uma invocação de Lambda por região, orquestrada
  por Step Functions ou EventBridge, reaproveitando `discover_generic_resources`,
  `discover_bedrock_resources` e `discover_eks_resources` como estão) em vez
  de repetir o loop sequencial de `main.py`.
- **Permissões**: a execution role da Lambda substitui o `--profile` local —
  aplicar a mesma política de somente-leitura acima (mais as permissões de
  escrita necessárias nos estágios 2+, que devem ficar em uma política
  separada, nunca na mesma role usada para descoberta).
- **CloudFormation StackSets**: o template implantado por StackSet em cada
  conta deve provisionar a execution role local, a própria função Lambda com
  o pacote `aws_prm_tagging` (ou uma Lambda Layer compartilhada entre as
  functions dos 4 estágios, já que o código de descoberta é o mesmo), e o
  gatilho apropriado por estágio (EventBridge Scheduler para a varredura
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
