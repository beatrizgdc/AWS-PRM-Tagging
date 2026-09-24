# Validação manual da Etapa 3 contra conta AWS real (sandbox)

> **Atenção: isto roda contra uma conta AWS REAL, cria recursos reais (alguns
> com custo por hora), e implanta uma stack CloudFormation de verdade.**
> **Nunca rode contra conta de cliente — só sandbox.** Cada seção tem um
> passo de reversão explícito; não pule esse passo. É um roteiro para você
> rodar (ou autorizar) quando tiver acesso à conta sandbox, não uma
> automação. **As 3 partes já foram executadas** (autorizadas
> explicitamente, conta sandbox XXXXXXXXXXXX, 2026-09-23/24) — CloudFront
> foi a única pulada (custo de tempo de propagação, não de dinheiro; segue
> não testada). Ver resultado em cada seção — **4 bugs reais foram
> encontrados e corrigidos** nessa execução.

## Por que este roteiro existe

Três lacunas que só uma conta AWS real resolve (ver
[docs/melhorias-futuras.md](../../docs/melhorias-futuras.md)) — **as 3
foram executadas, ver resultado em cada parte abaixo**:

1. **Capitalização exata do CloudTrail** — a verificação automatizada
   (`test_event_parser_botocore.py`) prova que os campos existem na API,
   não a forma exata que o CloudTrail grava para os serviços de protocolo
   `query`/`ec2`/`rest-xml` (RDS, ElastiCache, Redshift, Elastic
   Beanstalk, SNS, S3, Route 53, CloudFront, ELB) — só um evento real
   captura isso. **Executado — ver Parte 1.**
2. **O pipeline de ponta a ponta nunca rodou** — EventBridge → Step
   Functions → Lambda → revalidação → escrita real → SNS. `sam build`/
   `sam validate`/`cfn-lint`/`sam local invoke` (rodados nesta sessão, ver
   [infra/README.md](../../infra/README.md)) provam que o pacote é válido
   e importa corretamente num runtime Lambda real — não provam que a regra
   do EventBridge de fato casa um evento real, nem que o Step Functions
   invoca a Lambda corretamente, nem que a tag chega no recurso. **Executado
   — ver Parte 2.**
3. **CodeBuild sem ação de tag nativa identificada** — **resolvido**, ver
   Parte 3 abaixo e [infra/README.md](../../infra/README.md#permissões-iam--cobertura-real).

## Achado à parte: Route 53/CloudFront só disparam EventBridge em us-east-1

Confirmado empiricamente durante a Parte 1 (não é só documentação da AWS —
foi reproduzido): eventos de CloudTrail de serviços **globais** (Route 53
aqui; CloudFront pelo mesmo motivo, IAM/STS também mas não mapeados) só
aparecem no Event History quando consultado em `us-east-1`, mesmo com o
perfil AWS configurado para `us-east-2` — `aws cloudtrail lookup-events`
devolveu 0 eventos em `us-east-2` e 1 evento (com `awsRegion: us-east-1`
gravado nele) assim que a mesma consulta rodou em `us-east-1`.

**Implicação arquitetural real, não só um detalhe de teste**: a AWS entrega
eventos de serviço global ao EventBridge **só no barramento padrão de
`us-east-1`**, não no da região onde a stack foi implantada. Se a stack da
Etapa 3 for implantada numa conta cliente em qualquer região que não seja
`us-east-1`, as regras do EventBridge lá **nunca vão ver** criação de
Route 53/CloudFront — os únicos 2 serviços "globais" mapeados hoje
(`event_mapping.py`). Isso não foi corrigido nesta sessão (mudança de
arquitetura — precisaria de uma regra extra implantada especificamente em
`us-east-1` nessas contas, ou aceitar o gap) — registrado como pendência
nova em [docs/melhorias-futuras.md](../../docs/melhorias-futuras.md).

## Pré-requisitos

- Perfil AWS configurado apontando para a conta **sandbox** (nunca cliente)
  — ver [README.md#configurar-credenciais-aws](../../README.md#configurar-credenciais-aws).
- `sam` CLI e `cfn-lint` instalados (`pip install aws-sam-cli cfn-lint` —
  já usados nesta sessão para validar o template estaticamente).
- Docker, se for repetir a validação local (`sam local invoke`) antes de ir
  para a conta real — opcional, já validado nesta sessão sem custo.
- Consciência de custo: RDS/ElastiCache/Redshift/ELB têm custo por hora
  enquanto o recurso existir. Os passos abaixo usam o menor tipo de
  instância elegível a free tier onde existe, e pedem para deletar
  imediatamente depois de capturar o evento — mas o custo não é zero
  enquanto o passo de reversão não roda.

---

## Parte 1 — Capturar eventos CloudTrail reais (sem implantar a stack)

**Resultado (executado em 2026-09-23/24, conta sandbox XXXXXXXXXXXX):**
todos os 8 serviços da tabela prioritária foram testados com um evento
real, exceto CloudFront (pulado por custo de tempo — 15-20min de
propagação para poder apagar). Redshift usou `ra3.large` em vez de
`dc2.large` (DC2 não está mais disponível para clusters novos nesta
conta/região — RA3 é a família atual; custo por hora um pouco maior que
o estimado no roteiro, mas o cluster ficou no ar só ~3 minutos).

| Serviço | Extractor estava | Resultado |
|---|---|---|
| S3 (`CreateBucket`) | correto | confirmado — lê `requestParameters.bucketName` |
| SNS (`CreateTopic`) | correto | confirmado — `_direct_ci` casa `topicArn`/`TopicArn` |
| Route 53 (`CreateHostedZone`) | correto | confirmado — ver também o achado sobre região global acima |
| ELB (`CreateLoadBalancer`) | correto | confirmado — já tratava as duas capitalizações de `loadBalancerArn` |
| Redshift (`CreateCluster`) | correto | confirmado — já construía o ARN do request, nunca dependeu de `responseElements` |
| **RDS (`CreateDBInstance`)** | **bug real** | `responseElements` vem ACHATADO (`dBInstanceArn` na raiz), não `{"DBInstance": {...}}` como o shape do botocore documenta — corrigido em `event_parser.py` |
| **ElastiCache (`CreateCacheCluster`)** | **bug real** | mesmo achatamento — `aRN` na raiz, não `{"CacheCluster": {...}}` — corrigido |
| **Elastic Beanstalk (`CreateApplication`)** | **bug real** | `responseElements` vem `null` — o ARN precisa ser construído a partir de `requestParameters.ApplicationName`, igual ao S3 — corrigido |

`RDS CreateDBCluster` (Aurora) não foi testado diretamente (mesmo padrão de
serviço/protocolo do `CreateDBInstance`, mas outra operação) — corrigido
por analogia, documentado como inferência em `event_parser.py`, não como
confirmação independente.

Todos os 3 bugs viraram regressão permanente em
`test/unit/test_event_parser_extracao.py` — suíte completa (235 testes)
continua passando. Todos os 8 recursos de teste foram apagados
imediatamente após a captura; nada ficou na conta.

Não precisa de Trail nenhuma configurada — `aws cloudtrail lookup-events`
lê o **Event History** padrão (90 dias, sempre ativo em toda conta,
gratuito) via `ReadOnly`/`LookupEvents`, sem custo e sem setup.

**Padrão geral, repetido por serviço abaixo:**

```bash
# 1. Criar o recurso (comando específico por serviço, ver tabela)
# 2. Esperar ~30-60s para o CloudTrail indexar o evento
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=<EventName> \
  --max-results 5 \
  --profile <perfil-sandbox> \
  --output json > /tmp/evento_<servico>.json

# 3. Inspecionar responseElements/requestParameters reais
python3 -c "
import json
d = json.load(open('/tmp/evento_<servico>.json'))
for e in d['Events']:
    ct = json.loads(e['CloudTrailEvent'])
    print(json.dumps(ct.get('responseElements'), indent=2))
"

# 4. Comparar contra o extractor correspondente em event_parser.py
#    (_REGISTRY[("<eventSource>", "<EventName>")]) — path e tolerant_casing.
# 5. Deletar o recurso (comando de reversão, ver tabela).
```

### Serviços de protocolo `query`/`ec2`/`rest-xml` (prioridade — capitalização incerta)

| Serviço | Criar | EventName | Reverter |
|---|---|---|---|
| S3 | `aws s3api create-bucket --bucket <nome-unico-descartavel> --profile <perfil>` | `CreateBucket` | `aws s3api delete-bucket --bucket <nome> --profile <perfil>` |
| SNS | `aws sns create-topic --name teste-prm-etapa3 --profile <perfil>` | `CreateTopic` | `aws sns delete-topic --topic-arn <arn> --profile <perfil>` |
| Route 53 | `aws route53 create-hosted-zone --name teste-prm-etapa3.exemplo.com --caller-reference $(date +%s) --profile <perfil>` (custo pequeno, ~US$0,50/mês pró-rata) | `CreateHostedZone` | `aws route53 delete-hosted-zone --id <id> --profile <perfil>` |
| RDS | `aws rds create-db-instance --db-instance-identifier teste-prm-etapa3 --db-instance-class db.t3.micro --engine mysql --master-username admin --master-user-password <senha-temporaria> --allocated-storage 20 --profile <perfil>` (elegível a free tier; leva alguns minutos para ficar `available`, mas o evento `CreateDBInstance` já é gravado na hora da chamada) | `CreateDBInstance` | `aws rds delete-db-instance --db-instance-identifier teste-prm-etapa3 --skip-final-snapshot --profile <perfil>` |
| ElastiCache | `aws elasticache create-cache-cluster --cache-cluster-id teste-prm-etapa3 --cache-node-type cache.t3.micro --engine redis --num-cache-nodes 1 --profile <perfil>` (elegível a free tier) | `CreateCacheCluster` | `aws elasticache delete-cache-cluster --cache-cluster-id teste-prm-etapa3 --profile <perfil>` |
| Elastic Beanstalk | `aws elasticbeanstalk create-application --application-name teste-prm-etapa3 --profile <perfil>` (a aplicação em si não tem custo; não crie um Environment, caro e desnecessário só para capturar o evento `CreateApplication`) | `CreateApplication` | `aws elasticbeanstalk delete-application --application-name teste-prm-etapa3 --profile <perfil>` |
| **ELB (elbv2)** | ⚠️ Custo por hora (~US$0,02/h) e exige VPC/subnets já existentes — `aws elbv2 create-load-balancer --name teste-prm-etapa3 --subnets <subnet1> <subnet2> --profile <perfil>` | `CreateLoadBalancer` | `aws elbv2 delete-load-balancer --load-balancer-arn <arn> --profile <perfil>` |
| **Redshift** | ⚠️ Sem free tier, custo real (~US$0,25/h no menor nó) — `aws redshift create-cluster --cluster-identifier teste-prm-etapa3 --node-type dc2.large --number-of-nodes 1 --master-username admin --master-user-password <senha-temporaria> --profile <perfil>` — considere pular este e confiar na leitura tolerante a capitalização (`_direct_ci`/`_constructed_ci`) até haver justificativa de custo | `CreateCluster` | `aws redshift delete-cluster --cluster-identifier teste-prm-etapa3 --skip-final-cluster-snapshot --profile <perfil>` |
| **CloudFront** | Sem custo de criação, mas a distribuição leva 15-20min para propagar (o evento `CreateDistribution` é gravado na hora, não precisa esperar propagar para capturar) — `aws cloudfront create-distribution --distribution-config file:///tmp/cf-config-minimo.json --profile <perfil>` (precisa de um JSON de config mínimo à parte) | `CreateDistribution` | `aws cloudfront delete-distribution --id <id> --if-match <etag> --profile <perfil>` (só depois de desabilitada e com status `Deployed`) |

### Serviços de protocolo `json`/`rest-json` (confiança já alta — opcional, só se sobrar tempo)

Qualquer um dos outros ~60 serviços mapeados serve como checagem adicional
de baixo risco (ex.: `aws dynamodb create-table`, `aws sqs create-queue`,
`aws logs create-log-group` — todos gratuitos ou com custo desprezível e
reversão imediata). Priorize isso só depois da lista acima.

### Se algum evento real divergir do extractor

1. Anotar o campo real (`responseElements`/`requestParameters`, path e
   valor) contra o que `event_parser._REGISTRY[(event_source, event_name)]`
   espera.
2. Corrigir o `path`/`tolerant_casing` do `_DirectPath`/`_ConstructedPath`
   correspondente em `event_parser.py`.
3. Adicionar (ou ajustar) o caso em `test_event_parser_extracao.py` com o
   payload real capturado (anonimizado — nunca commitar ARN/conta real),
   para virar regressão permanente.
4. Rodar `python3 -m pytest aws_prm_tagging/test/unit/` para confirmar que
   nada mais quebrou.

---

## Parte 2 — Smoke test de ponta a ponta (implanta a stack)

**Resultado (executado em 2026-09-23/24, conta sandbox XXXXXXXXXXXX, stack
`prm-etapa3-smoke-test` em `us-east-2`):** o pipeline completo — EventBridge
→ Step Functions (debounce 30s) → Lambda → decisão → escrita real → tag
no recurso — **funciona de ponta a ponta**, confirmado com um bucket S3
criado de propósito. A verificação por e-mail (passo 4 do roteiro) foi
pulada (sem como confirmar inscrição sem acesso a caixa de entrada) —
verificado direto via execução do Step Functions, `CloudTrail
lookup-events` e a tag no bucket, que é a mesma informação.

**2 bugs reais de permissão IAM encontrados e corrigidos** — a primeira
tentativa (antes da correção) rodou sem erro nenhum no CloudWatch Logs da
Lambda, mas o bucket NÃO ficou tagueado — só o `TagResources` bruto no
CloudTrail revelou o motivo (`FailedResourcesMap`, HTTP 200 na chamada mas
falha por recurso dentro dela — exatamente o caso que `tag_execution.py`
já documentava, só que na prática, não hipoteticamente):

1. **S3 precisa de `s3:GetBucketTagging` ALÉM de `s3:PutBucketTagging`** —
   só o Put estava na política; `tag:TagResources` lê o TagSet atual antes
   de escrever. Sem o Get, todo bucket falharia silenciosamente (sem
   exceção, só reportado como `erro_permissao` no SNS) até este teste.
2. **CloudWatch Logs precisa de `logs:TagResource`, não `logs:TagLogGroup`**
   — as duas ações existem no botocore (por isso a verificação estrutural
   automatizada não pegou isso — as duas são "válidas", só uma é a que
   `tag:TagResources` de fato invoca internamente), mas só `TagResource` é
   chamada de verdade. Achado porque a PRÓPRIA Lambda, ao rodar pela
   primeira vez, cria seu log group — um evento `logs.amazonaws.com/
   CreateLogGroup` orgânico (não disparado de propósito) que a Etapa 3
   processou e tentou taguear, revelando o mesmo tipo de bug num segundo
   serviço.

Depois de corrigir as duas ações em `infra/template.yaml` e reimplantar, um
segundo bucket de teste confirmou a tag aplicada corretamente
(`aws-apn-id=pc:testesmoketestesomente`).

**Achado à parte, sem ação necessária**: a Lambda criando seu próprio log
group é um evento de criação de recurso genuíno (CloudWatch Logs está
mapeado) — a Etapa 3 processá-lo e tentar tagueá-lo é o comportamento
correto, não um bug; só serviu de segundo caso de teste orgânico para o
mesmo tipo de problema de permissão.

Stack, os 2 buckets de teste e tudo mais foram apagados ao final
(`sam delete` + `s3api delete-bucket`) — confirmado via
`describe-stacks`/`list-buckets`/`list-rules` que nada ficou na conta.

**Reduz o debounce para o teste** (não espere 30 minutos de verdade):
`--parameter-overrides DebounceSeconds=30` no deploy abaixo.

```bash
cd tag  # pai de aws_prm_tagging/ (diretório que contém o Makefile)

# 1. Build (usa o Makefile — já validado nesta sessão, sem custo)
sam build --template-file aws_prm_tagging/infra/template.yaml

# 2. Deploy guiado — nome de stack claramente de teste, região sandbox
sam deploy --guided \
  --stack-name prm-etapa3-smoke-test \
  --parameter-overrides ExpectedTagValue=pc:testesmoketestesomente DebounceSeconds=30 \
  --capabilities CAPABILITY_IAM \
  --profile <perfil-sandbox>

# 3. Confirmar que as 3 regras de EventBridge e a Step Machine foram criadas
aws events list-rules --name-prefix prm-etapa3-smoke-test --profile <perfil-sandbox>
aws stepfunctions list-state-machines --profile <perfil-sandbox>

# 4. Assinar o tópico SNS num e-mail de teste, para ver a mensagem final
#    (pegar o ARN do Output "PrmComplianceTopicArn" do deploy)
aws sns subscribe --topic-arn <arn-do-topico> --protocol email --notification-endpoint <seu-email> --profile <perfil-sandbox>
# confirmar a inscrição no e-mail recebido antes de continuar

# 5. Disparar o evento de verdade — criar um bucket S3 (mais rápido e
#    gratuito) NA MESMA CONTA/REGIÃO onde a stack foi implantada
aws s3api create-bucket --bucket <nome-unico-smoke-test> --profile <perfil-sandbox>

# 6. Esperar ~40s (30s de debounce + margem) e conferir a execução do
#    Step Functions
aws stepfunctions list-executions --state-machine-arn <arn-da-state-machine> --profile <perfil-sandbox>
aws stepfunctions describe-execution --execution-arn <arn-da-execucao> --profile <perfil-sandbox>

# 7. Conferir os logs da Lambda
aws logs tail /aws/lambda/<nome-da-funcao> --profile <perfil-sandbox>

# 8. Conferir que a tag chegou de verdade no bucket
aws s3api get-bucket-tagging --bucket <nome-unico-smoke-test> --profile <perfil-sandbox>

# 9. Conferir a mensagem recebida no e-mail assinado no passo 4 (categoria_final
#    deve ser "tagueado_sucesso")
```

**Reversão (sempre, mesmo se algo falhar no meio):**

```bash
# Esvaziar e deletar o bucket de teste
aws s3api delete-bucket --bucket <nome-unico-smoke-test> --profile <perfil-sandbox>

# Cancelar a inscrição do e-mail no SNS
aws sns unsubscribe --subscription-arn <arn-da-inscricao> --profile <perfil-sandbox>

# Deletar a stack inteira (remove EventBridge, Step Functions, Lambda, SNS, IAM)
sam delete --stack-name prm-etapa3-smoke-test --profile <perfil-sandbox>
```

### Se a execução do Step Functions nunca aparecer

Sinal de que a regra do EventBridge não casou o evento — confirmar:
- O bucket foi criado na MESMA região onde a stack (e as regras) foram
  implantadas.
- `aws events test-event-pattern` com o pattern da regra e um evento de
  amostra (`aws cloudtrail lookup-events` do passo 5) — confirma se o
  pattern realmente casaria o evento real.

---

## Parte 3 — Investigar a ação de tag do CodeBuild

**Resultado (executado em 2026-09-23, conta sandbox XXXXXXXXXXXX,
`codebuild-teste-service-role` como service role):** o passo 2
(`tag:TagResources`, caminho genérico) funcionou sozinho —
`FailedResourcesMap` veio vazio e `batch-get-projects` confirmou a tag
aplicada no projeto. **CodeBuild não precisa de nenhuma ação nativa na
política `PrmEtapa3NativeTagWrite`** — a permissão genérica já existente
(`PrmEtapa3TagWrite`) é suficiente. Passo 3 (`UpdateProject`) não foi
necessário. Projeto de teste (`teste-prm-etapa3`) apagado logo em seguida,
nada ficou na conta. Comandos abaixo mantidos como documentação do
processo, para o mesmo tipo de investigação em outro serviço no futuro.

```bash
# 1. Criar um projeto mínimo descartável
aws codebuild create-project \
  --name teste-prm-etapa3 \
  --source type=NO_SOURCE \
  --artifacts type=NO_ARTIFACTS \
  --environment type=LINUX_CONTAINER,image=aws/codebuild/standard:7.0,computeType=BUILD_GENERAL1_SMALL \
  --service-role <arn-de-uma-role-generica-de-teste> \
  --profile <perfil-sandbox>

# 2. Tentar tag:TagResources (caminho genérico que a Etapa 2c/3 usariam)
aws resourcegroupstaggingapi tag-resources \
  --resource-arn-list <arn-do-projeto> \
  --tags aws-apn-id=pc:testesmoketestesomente \
  --profile <perfil-sandbox>
# Se isso funcionar sozinho (sem nenhuma permissão nativa adicional),
# CodeBuild não precisa de entrada na política PrmEtapa3NativeTagWrite —
# atualizar infra/template.yaml e infra/README.md removendo a nota de gap.

# 3. Se o passo 2 falhar, tentar via UpdateProject (concede mais que só
#    tagging — só para descobrir SE é esse o mecanismo, não para adotar
#    como está)
aws codebuild update-project --name teste-prm-etapa3 --tags key=aws-apn-id,value=pc:testesmoketestesomente --profile <perfil-sandbox>

# 4. Confirmar
aws codebuild batch-get-projects --names teste-prm-etapa3 --profile <perfil-sandbox> --query "projects[0].tags"
```

**Reversão:**

```bash
aws codebuild delete-project --name teste-prm-etapa3 --profile <perfil-sandbox>
```

**Depois de confirmar o mecanismo real**: atualizar
`infra/template.yaml` (`PrmEtapa3NativeTagWrite`, adicionar a ação certa se
`tag:TagResources` sozinho não bastar) e remover a nota de gap em
[infra/README.md](../../infra/README.md) e
[docs/melhorias-futuras.md](../../docs/melhorias-futuras.md).

---

## O que este roteiro NÃO cobre

- Os 16 serviços ainda sem mapeamento (`event_mapping.py`) — decisão de
  produto antes de mapear, não um teste.
- Durabilidade da tag em recursos de EKS (nodes/load balancers/volumes) —
  já documentada como limitação separada em
  [docs/melhorias-futuras.md](../../docs/melhorias-futuras.md).
- Volume real de eventos em conta de cliente grande (rajadas de Auto
  Scaling etc.) — só observável em produção, não reproduzível de forma
  barata num smoke test.
- Qualquer coisa das Etapas 1/2 (Custom Resource) ou Etapa 4 — fora do
  escopo da Etapa 3.
