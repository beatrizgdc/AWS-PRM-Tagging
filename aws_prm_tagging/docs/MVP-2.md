# Automação de Tagueamento AWS PRM — MVP 2

13/10/2026 · Complementa o [MVP-1.md](MVP-1.md), que continua valendo para
tudo o que não está aqui (arquitetura base, Etapas 1 a 4, CLI, operação).

## O que mudou

O MVP2 atende os seis pedidos de 13/10/2026:

| # | Pedido | O que foi feito | Validação |
| --- | --- | --- | --- |
| 6 | Regra nova da AWS: pode sobrescrever outro parceiro; IaC não é tagueado e vai para lista do cliente | Decisão nova, detecção de Terraform/OpenTofu/CloudFormation/CDK/Pulumi (tags e CloudTrail), relatório do cliente com IaC e estado anterior | Local (moto). Sandbox: guia passos B, C, E |
| 3 | SDK atual nas Lambdas | boto3/botocore 1.43.113, fixados por hash, numa Lambda Layer | Local (`sam build`). Sandbox: passo D (`versao_boto3`) |
| 2 | Rollout por StackSets | Templates expandidos sem transform, bucket de artefatos por região na payer, CLI `stackset.py`, tópico central opcional | Local (moto + CloudFormation falso). Payer de teste: opcional O3 |
| 4 | Modo interativo | `python3 iniciar.py`: mapear, simular, taguear e implantar, com resumo e confirmação | Local (sessões simuladas) |
| 1 | Zabbix | Lambda que assina o tópico e envia por API (`history.push`) ou trapper; template de importação | Local (servidor trapper falso). Zabbix real: quando o acesso sair |
| 5 | Well-Architected + segurança | Revisão dos 6 pilares, bandit/checkov/pip-audit/cfn-lint, correções aplicadas | Local |

Achado no caminho: duas ações de IAM do MVP1 não existiam
(`apigateway:TagResource` e `cloudhsm:AddTagsToResource`). Tagueamento de
API Gateway e CloudHSM teria falhado com `AccessDenied` na conta real.
Corrigido para `apigateway:PUT` em `/tags/*` e `cloudhsm:TagResource`,
sempre com a condição `aws:TagKeys = [aws-apn-id]`.

Testes automatizados: **608** (eram 505), todos passando no pacote.

## 1. Regra de negócio nova

### Decisão por recurso (Etapa 2a)

Aplicada nesta ordem; a primeira que casar vale:

| # | Situação | Decisão | Escreve? | No relatório do cliente |
| --- | --- | --- | --- | --- |
| 1 | `aws-apn-id` já tem o nosso valor | `ja_ok` | não | — |
| 2 | Gerenciado por IaC (qualquer ferramenta), com ou sem tag | `pular_iac` | **nunca** | lista de IaC, com o valor atual se houver |
| 3 | `aws-apn-id` com outro valor (outro parceiro) | `sobrescrever` | sim | estado anterior (valor antigo) |
| 4 | Sem `aws-apn-id`, com chave parecida (`AWS-APN-ID`) | `taguear_com_tag_similar` | sim, ao lado | estado anterior (chave e valor parecidos, mantidos) |
| 5 | Sem tag | `taguear` | sim | — |

As decisões `conflito` e `revisar_tag_similar` do MVP1 deixaram de existir.
Um relatório de decisão antigo não é executado: cada recurso vira
`erro_classificacao`, pedindo para rodar `decide` de novo. Nada é
sobrescrito com base em regra velha.

A revalidação logo antes da escrita aplica a mesma regra. Um recurso que
passou a ser de IaC depois do mapeamento é pulado, e o "estado anterior"
registrado é o lido na hora da escrita.

### Como o IaC é detectado

| Sinal | Onde | Cobre |
| --- | --- | --- |
| Tag `aws:cloudformation:stack-name` | Etapas 1 a 4 | CloudFormation, CDK, SAM, Elastic Beanstalk, Control Tower, Service Catalog, eksctl |
| Tags de convenção (`managed-by=terraform/opentofu/pulumi/cdk`, `pulumi:*`, `terraform=true`, qualquer valor igual ao nome da ferramenta) | Etapas 1 a 4 | Terraform, OpenTofu, Pulumi, CDK, quando o time marca |
| `userAgent` do evento do CloudTrail | Etapa 3 (recurso novo) | Terraform/OpenTofu (`HashiCorp/... Terraform/...`), Pulumi, CloudFormation/CDK (`cloudformation.amazonaws.com`) |
| Histórico do CloudTrail (`LookupEvents`, 90 dias, grátis) | Etapa 1, e Etapa 3 para instância EC2 | Recursos existentes criados ou alterados por IaC nos últimos 90 dias; instâncias que chegam pelo evento nativo |

Sobre o histórico do CloudTrail:

- **Quais eventos contam:** só eventos de escrita. Um *data source* do Terraform só lê, e não marca o recurso.
- **Volume de chamadas:** a consulta é feita só para quem seria tagueado, a 2 chamadas por segundo por região. O `map` aceita `--orcamento-historico-cloudtrail` (segundos) e `--sem-historico-cloudtrail`; a Lambda usa 300 s por região.
- **O que não for verificado** fica `nao_verificado` em `iac_historico` e cai na heurística de tags.
- **Volumes de instância:** o volume EBS de uma instância criada por IaC herda a decisão da instância, porque nasceu do mesmo `RunInstances`.

Limite conhecido: um recurso de Terraform criado há mais de 90 dias, sem
alteração recente e sem tag de convenção não tem sinal nenhum. Ele será
tagueado, e o próximo `terraform apply` remove a tag; o alerta de drift
mostra isso como "alterado por IaC". Para reduzir esse caso, recomende ao
cliente o `default_tags` do provider (exemplo no relatório).

### Relatório para o cliente

O `apply` e o modo interativo gravam, ao lado do resultado,
`<saida>_cliente.md`, `.csv` e `.json`. O conteúdo:

1. **Recursos de IaC:**
   - por recurso: ferramenta, stack (CloudFormation), evidência da detecção e valor atual da `aws-apn-id`;
   - ação recomendada;
   - exemplo de como aplicar a tag em cada ferramenta encontrada (`default_tags` no Terraform/OpenTofu, tags da stack no CloudFormation, `Tags.of(app)` no CDK, `defaultTags` no Pulumi).
2. **Estado anterior:** cada recurso sobrescrito (valor antigo) ou tagueado ao lado de chave parecida (chave e valor parecidos).

Também dá para gerar a partir de um resultado antigo:
`python3 -m aws_prm_tagging.main relatorio-cliente --input resultado.json`.

A Lambda do tagueamento inicial e da varredura não grava arquivo. O resumo
publicado no SNS traz `cliente_resumo`, até 25 itens de `iac_amostra` e de
`estado_anterior_amostra` por região, e `por_acao`. A lista completa sai
pelo CLI ou pelo modo interativo.

### Disputa com outro parceiro

Com a sobrescrita liberada, duas automações podem trocar o valor uma da
outra. A nossa só reage a **criação** de recurso (Etapa 3) e à varredura
agendada; o alerta de drift **não** reaplica. Quando o valor muda para
outro, o alerta sai com `possivel_disputa_com_outro_parceiro: true` para
revisão humana antes de a varredura em modo live sobrescrever de novo.

## 2. Modo interativo

```bash
python3 iniciar.py                 # Linux/macOS/WSL
py iniciar.py                      # Windows
python3 -m aws_prm_tagging iniciar # equivalente
```

O assistente passa por estas etapas, com resumo e confirmação (s/n) em cada uma:

1. **Conta:** perfil e confirmação de que é a conta certa.
2. **Valor da tag:** `pc:...`.
3. **Regiões:** escolha das regiões, mais a checagem de CloudTrail.
4. **Mapeamento:** tabela com recursos, sem tag, outro parceiro, IaC e serviços.
5. **Decisão:** quantos seriam tagueados, sobrescritos (e quais valores), ao lado de chave parecida, e de IaC (por ferramenta).
6. **Simulação (dry-run):** mostra o mesmo resultado e grava a prévia do relatório do cliente.
7. **Tagueamento de verdade:**
   - grava antes a lista `a_taguear.tsv`;
   - aceita um arquivo de ARNs para restringir;
   - **exige digitar o ID da conta**.
8. **Implantação da automação contínua (opcional):**
   - mostra o que cria, o custo e como remover;
   - chama `deploy/implantar_conta.sh` com o tagueamento inicial desligado (acabou de rodar).

Nada é escrito antes do passo 7. Os arquivos ficam em
`saida-prm-<conta>-<data>/`, e os logs técnicos em `execucao.log`; na tela
fica só o resumo. Para CI existe `--nao-interativo`: lê tudo e só escreve
com `--aplicar` ou `--implantar` explícitos.

## 3. SDK atual nas Lambdas

O runtime python3.12 da Lambda traz um boto3 mais antigo, atualizado quando
a AWS quer. Agora cada stack tem uma Lambda Layer (`PrmSdkLayer`) com
**boto3/botocore 1.43.113**, montada pelo `Makefile` a partir de
`aws_prm_tagging/requirements-lambda.txt`. Lá as versões são fixas e cada
pacote tem hash (`pip --require-hashes`). A layer vem antes do SDK do
runtime no `sys.path`. O resumo do tagueamento inicial e da varredura traz
`versao_boto3` para conferir.

Para atualizar:

1. Baixe os wheels (`pip download ... boto3==<versão>`, comando no topo do arquivo).
2. Gere os sha256.
3. Troque as linhas do arquivo.
4. Rode os testes.

`pip-audit` não aponta vulnerabilidade nas versões atuais.

## 4. Rollout por StackSets

### Arquitetura

Na **conta payer do cliente** (gerenciamento da organização):

- um **bucket de artefatos por região** (`prm-tagging-<conta>-<região>`), porque a Lambda só lê código de um bucket da mesma região;
  - privado, SSE-S3, versionado, leitura só por contas da organização (`aws:PrincipalOrgID`) e só por TLS;
- os StackSets `prm-hub` (uma região por conta) e `prm-regional` (todas as regiões escolhidas), service-managed, com **auto-deployment**: conta que entra na OU recebe as stacks sozinha;
- opcional, mas recomendado com muitas contas: a stack **central** (`infra/template-central.yaml`) com um tópico SNS da organização.
  - Sem ela, cada conta manda um e-mail de confirmação de assinatura.
  - O tópico é criptografado com chave KMS própria, porque a gerenciada pela AWS não aceita publicação de outra conta.
  - Ele aceita publicação só da organização e cria uma fila para consumidores, como o Zabbix.

Os templates do StackSet são CloudFormation puro, gerados do SAM pelo
`samtranslator` (o mesmo motor do transform). Não dependem de transform
nem de `CAPABILITY_AUTO_EXPAND`, e cabem no limite de 51.200 bytes do
corpo do template.

**O valor do contrato é por OU:** cada `implantar` aplica o `pc:` como
override daquela OU. OUs com contratos diferentes são implantações
separadas. Ainda falta a lista OU → contrato de cada cliente (informação
de negócio).

### Passo a passo (conta payer)

| Passo | Comando | Cria | Custo | Remove |
| --- | --- | --- | --- | --- |
| 1. Empacotar (local) | `python3 deploy/stacksets/empacotar.py --versao 2.0.0` | `dist/stacksets/2.0.0/` no computador | — | apagar a pasta |
| 2. Tópico central (opcional) | `aws cloudformation deploy --template-file aws_prm_tagging/infra/template-central.yaml --stack-name prm-central --parameter-overrides OrgId=o-... NotificationEmail=...` | tópico, chave KMS, fila e DLQ | ~US$ 1/mês (chave KMS) + uso | `aws cloudformation delete-stack --stack-name prm-central` |
| 3. Publicar artefatos | `python3 deploy/stacksets/stackset.py publicar --profile payer --dist dist/stacksets/2.0.0 --regioes us-east-1,sa-east-1,...` | 1 bucket por região (~17 MB cada) | centavos/mês | `stackset.py remover --somente-artefatos ...` |
| 4. Implantar numa OU | `python3 deploy/stacksets/stackset.py implantar --profile payer --dist dist/stacksets/2.0.0 --ou ou-xxxx-yyyyyyyy --valor pc:XXXX --hub-regiao us-east-1 [--topico-central-arn ... --kms-central-arn ...]` | StackSets e uma stack por conta e região | por uso (ver Custos) | `stackset.py remover --ou ... --regioes ... --hub-regiao ...` |
| 5. Acompanhar | `python3 deploy/stacksets/stackset.py status --profile payer` | — | — | — |

Sobre cada comando:

- Todos mostram o plano (o que cria, o custo e como remover) e pedem `sim` antes de escrever. `--somente-mostrar` só mostra.
- O `implantar` começa em **dry-run** (tagueamento inicial e varredura). Para escrever nos recursos existentes, rode de novo com `--modo-inicial live`, depois do consentimento do cliente.
- O acesso confiável dos StackSets com o Organizations precisa estar ativo. O script confere e, com `--ativar-acesso-confiavel`, ativa.
- Região bloqueada por SCP numa conta falha só ali: a tolerância a falha é 100%, e o resumo lista as falhas.
- **Limitação da AWS:** StackSet service-managed não implanta na própria payer. Se ela tiver workloads, use `deploy/implantar_conta.sh` nela.

## 5. Zabbix

O SNS continua sendo o barramento. Uma Lambda própria
(`infra/template-zabbix.yaml`) assina o tópico, de preferência o central, e
envia cada mensagem como itens trapper. O núcleo não muda.

Como implantar, quando houver acesso:

1. No Zabbix, importe `aws_prm_tagging/infra/zabbix/template-prm-tagging.yaml` (formato 7.0) e vincule-o a um host, por exemplo `aws-prm`.
2. Escolha o modo:
   - **api** (recomendado, Zabbix 7.0+): HTTPS com `history.push`. Crie um token de API e guarde-o no SSM:
     `aws ssm put-parameter --name /prm/zabbix/token --type SecureString --value <token>` (Parameter Store padrão, grátis).
   - **trapper** (qualquer versão): TCP 10051 até o server ou proxy.
3. Implante:
   ```bash
   sam build --template-file aws_prm_tagging/infra/template-zabbix.yaml
   sam deploy --stack-name prm-zabbix --resolve-s3 --capabilities CAPABILITY_IAM \
     --parameter-overrides TopicoArn=<arn do tópico> ZabbixModo=api ZabbixUrl=https://zabbix... ZabbixHost=aws-prm
   ```
   Para um Zabbix interno, informe `SubnetIds`/`SecurityGroupIds`. A sub-rede precisa de saída para o SSM.
4. **Cria:** 1 Lambda (arm64, 128 MB), layer, fila DLQ, log group com 30 dias e 1 alarme CloudWatch.
   - **Custo:** centavos, mais US$ 0,10/mês do alarme.
   - **Remove:** `sam delete --stack-name prm-zabbix`.

Itens do template:

- `prm.evento`: texto, toda mensagem.
- Por conta (descoberta automática `{#CONTA}`): sucesso, falha, IaC, drift, pendentes, falhas da varredura, DLQ, região sem stack, falha de região e heartbeat.
- Cada um tem um gatilho (falha e drift em warning/average; DLQ e falha de região em high).
- O gatilho de ausência (`nodata`, 2 dias) vem desligado. Ligue-o junto com `PublicarVarreduraSemNovidade=true` no hub, para haver uma mensagem por dia mesmo sem novidade.

Se o Zabbix estiver fora do ar, a Lambda tenta de novo. Esgotadas as
tentativas, a mensagem vai para a DLQ (14 dias) e o alarme dispara.

**Pendente:** testar contra o Zabbix real (versão, URL e rede ainda não conhecidos).

## 6. Revisão Well-Architected e segurança

### Varredura automática

| Ferramenta | Alvo | Resultado |
| --- | --- | --- |
| `cfn-lint` | 4 templates + 2 templates expandidos do StackSet | 0 achados (achou as 2 ações IAM inexistentes do MVP1, corrigidas) |
| `checkov` | templates expandidos, central e Zabbix | só achados aceitos (tabela abaixo) |
| `bandit` | todo o código Python | 0 achados após correções (URL `https://` obrigatória no Custom Resource, `csv_check` e Zabbix; supressões só onde revisado) |
| `pip-audit` | `requirements-lambda.txt` | nenhuma vulnerabilidade conhecida |
| Revisão de IAM | todas as roles | toda escrita de tag em `*` exige `aws:TagKeys = [aws-apn-id]`; as demais escritas são restritas a recursos da própria stack (teste automático trava isso) |

### Por pilar

| Pilar | O que foi verificado | Mudanças no MVP2 |
| --- | --- | --- |
| **Segurança** | Menor privilégio, criptografia, segredos, entrada externa, supply chain | Tópico SNS do hub criptografado (`alias/aws/sns`, sem custo). Tópico central com KMS próprio, rotação e acesso só da organização + TLS. Buckets de artefatos privados, versionados, só organização + TLS. Token do Zabbix no SSM SecureString (nunca em variável de ambiente). SDK fixado por hash. URLs validadas. Ações IAM corrigidas. |
| **Confiabilidade** | Retentativas, DLQs, isolamento de falha, idempotência, timeouts | DLQ também para erro da função de drift (antes só falha de entrega). Timeout da Etapa 3 de 60 para 120 s (histórico do CloudTrail sob throttling, com tentativas curtas). StackSet com tolerância a falha por região/conta. Zabbix com DLQ e alarme. |
| **Excelência operacional** | Implantação, observabilidade, runbooks, remoção sem resíduo | Log groups dentro das stacks, com retenção (30 dias, parâmetro `RetencaoLogsDias`) e removidos junto (no MVP1 ficavam órfãos). Modo interativo. `versao_boto3` no resumo. CLI de StackSets com plano/confirmação/status. Guia de sandbox do MVP2. |
| **Eficiência de desempenho** | Memória, concorrência, chamadas de API | arm64 (Graviton) como padrão (`Arquitetura`). Histórico do CloudTrail só para candidatos, com orçamento de tempo. Zip de código único compartilhado entre funções e stacks. |
| **Otimização de custos** | Custo parado, custo por evento, alarmes | Nada cobra por hora. arm64 ~20% mais barato por GB-s. Sem alarme por região (a varredura diária já lê a DLQ de cada região; um alarme por região e conta custaria US$ 0,10 cada). Logs com retenção. |
| **Sustentabilidade** | Recursos ociosos, arquitetura | Tudo serverless e acionado por evento. Graviton. Varredura com agenda ajustável (diária por padrão; semanal reduz o consumo). |

### Achados aceitos (com justificativa)

| Achado (checkov) | Por que fica |
| --- | --- |
| Lambda fora de VPC (CKV_AWS_117) | Só chama APIs AWS públicas; VPC exigiria NAT (~US$ 32/mês por região). O Zabbix tem opção de VPC. |
| Variáveis de ambiente sem KMS próprio (CKV_AWS_173) | Não guardam segredo (valor da tag, ARNs). Já são criptografadas pela chave da AWS. |
| Log group sem KMS próprio (CKV_AWS_158) | Logs têm ARNs, sem dado sensível; criptografia padrão do CloudWatch. KMS próprio custaria US$ 1/mês por chave e região. |
| Sem limite de concorrência nas funções do hub (CKV_AWS_115) | Limitadas pelo `MaxConcurrency 4` do Map da state machine. A Etapa 3 tem limite por parâmetro. |
| Sem DLQ nas funções síncronas (CKV_AWS_116) | Invocadas pelo Step Functions, que trata o erro (publica no SNS ou manda para a DLQ). |
| Chave KMS com principal `*` (CKV_AWS_33, só no central) | Restrita por `aws:PrincipalOrgID` e `kms:ViaService` do SNS; necessária para as contas da organização publicarem. |
| DLQ com SSE-SQS e não KMS (CKV_AWS_27) | SSE-SQS é criptografia em repouso sem custo. |

## 7. Custos (estimativa, preços de tabela de us-east-1)

Por conta, em uso normal (conferir na calculadora da AWS para volumes reais):

| Item | Estimativa |
| --- | --- |
| Etapa 3, por 1.000 recursos novos/mês | ~US$ 0,15 (Step Functions ~5 transições por evento + Lambda) |
| Varredura diária em dry-run, 17 regiões | ~US$ 0,40/mês (Lambda arm64, 1 GB, ~1 min por região); semanal: ~US$ 0,06 |
| Regras do EventBridge para eventos da AWS, histórico do CloudTrail, Scheduler | grátis |
| E-mail SNS | grátis até 1.000/mês |
| Logs (30 dias) | centavos |
| Tópico central (uma vez, na payer) | ~US$ 1/mês (chave KMS) |
| Buckets de artefatos (na payer) | centavos/mês |
| Zabbix | centavos + US$ 0,10/mês (alarme) |

## 8. Migração do MVP1

- **Relatórios de decisão do MVP1** não são aceitos pelo `apply`; rode `map` e `decide` de novo.
- **Stacks já implantadas:** um novo `sam deploy`/`implantar_conta.sh` atualiza.
  - Os log groups mudam de nome (`/aws/lambda/<stack>/<função>`), e os antigos (`/aws/lambda/prm-hub-*`) ficam órfãos. Apague-os com `deploy/remover_conta.sh --apagar-logs`, que lista antes.
  - `NotificationEmail` passou a ser opcional (vazio = sem e-mail). Para manter o e-mail, passe `--email` de novo.
- **`implantar_conta.sh`** ganhou `--topico-central`/`--kms-central` e `--arquitetura`.

## 9. Validação: o que falta na AWS real

O guia `aws_prm_tagging/test/GUIA-SANDBOX-MVP2.md` traz cada passo com o
que cria, o custo e a remoção. Pendências que dependem de acesso:

| Pendência | Depende de |
| --- | --- |
| Regra nova, IaC por userAgent, layer, log groups, relatório do cliente | sandbox (guia, passos B a F) |
| Publicação entre regiões (pendente do MVP1) | sandbox (opcional O1) |
| Instância EC2 de IaC pelo histórico | sandbox (opcional O2, ~US$ 0,002) |
| StackSets de ponta a ponta | conta payer de uma organização de teste |
| Zabbix | acesso ao Zabbix |
| `auditar-org` e tagueamento em escala (live) | conta de cliente, com consentimento |
