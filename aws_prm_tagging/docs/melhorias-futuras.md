# Melhorias futuras (pendências técnicas)

> **Atualização MVP2 (13/10/2026).** Novas pendências e decisões:
>
> - **Terraform antigo sem sinal:** recurso criado há mais de 90 dias, sem alteração recente nem tag de convenção, não é detectado (o histórico do CloudTrail só guarda 90 dias). Mitigação: recomendar `default_tags`; o drift mostra "alterado por IaC" se o `apply` remover a tag. Evolução possível: ler o state do Terraform (S3) com autorização do cliente.
> - **Disputa com outro parceiro:** o drift só alerta (`possivel_disputa_com_outro_parceiro`). Se virar rotina, avaliar regra de "não sobrescrever de novo em N dias".
> - **StackSets:** falta a lista OU → contrato de cada cliente (negócio) e o teste de ponta a ponta numa payer de teste.
> - **Zabbix:** falta o teste contra o servidor real (versão, URL, rede).
> - **Relatório do cliente na Lambda:** hoje só amostra no SNS (25 por região); se precisar da lista completa pela automação contínua, gravar num bucket da conta.

Registro das pendências técnicas identificadas no code review das Etapas
1-2c que **não** foram corrigidas junto — por exigirem uma decisão de
arquitetura/produto que não cabe a mim decidir sozinho, ou por terem custo
maior que uma correção pontual (nova chamada de API, mudança de escopo,
validação contra ambiente real). Cada item tem o porquê de não ter entrado
na correção imediata e o que destravaria fazer isso.

Isto é sobre **dívida técnica do código já implementado** (Etapas 1-2c).
Decisões de negócio/rollout multi-cliente (lista de clientes em escopo,
mapeamento contrato↔OU, etc.) ficam em
[arquitetura-multicliente.md](arquitetura-multicliente.md#pontos-em-aberto-resumo),
não aqui.

> **Atualização 2026-09-30 (handover):** o estado consolidado, as
> dependências e o que já foi resolvido estão em
> [plano-finalizacao.md](plano-finalizacao.md). Resolvidos no Lote 1 e
> mantidos abaixo só como histórico: "Volumes EBS criados junto com a
> instância", "Formas de criar recurso que não passam pelo evento esperado"
> (CreateFleet, restores de RDS/DynamoDB, snapshots, NAT, EIP), "Amazon
> DocumentDB e Amazon Neptune aparecem como Amazon RDS" (na Etapa 3). O
> mapeamento passou de 69 para 71 serviços (14 sem mapeamento).

## Pendências da Etapa 3 (automação contínua)

Decisões explicitamente adiadas durante a implementação inicial da Etapa 3
(EventBridge + Step Functions + Lambda), combinadas com o gestor do
projeto — cada uma com o porquê de não ter entrado agora.

Cada item abaixo tem uma **classificação**: `melhoria futura` significa que
a Etapa 3, hoje, já funciona corretamente sem esse item — a ausência dele
se degrada com segurança (falha reportada, nunca uma ação de tagueamento
errada) e não bloqueia nem é pré-requisito para nenhuma etapa seguinte;
entra quando houver decisão de produto ou dado de produção que justifique.
Nenhuma pendência aberta aqui é bloqueante hoje.

### Mapeamento evento→serviço — 69 de ~85 serviços cobertos, 16 conscientemente de fora

**Classificação: melhoria futura.** Não bloqueia a Etapa 3 (os 16 ficam
`None` explicitamente, sem tentativa de tagueamento incorreta) nem é
pré-requisito da Etapa 4 (que varre o estado final do recurso, não depende
de evento de criação).

**Atualizado.** Uma primeira versão deste item registrava cobertura de
~25/85 serviços, "de memória". Revisitado usando o `botocore` instalado
localmente como fonte de verdade (`session.get_service_model(...)` dá o
nome exato de cada operação de API e o `signingName`/`endpointPrefix` real
de cada serviço — a base do `eventSource` do CloudTrail) — cobertura hoje é
**69 dos ~85 `product_service_code`** (92 eventos de criação no total).
Esse processo já corrigiu **3 bugs reais** que a versão anterior tinha
(fonte errada para `AmazonKinesisAnalytics`, `AmazonTimestream` e
`CloudHSM`) e **confirmou** (não só supôs) que `AmazonDocDB`/`AmazonNeptune`
genuinamente não são distinguíveis por evento — os pacotes SDK
`docdb`/`neptune` têm `endpointPrefix`/`signingName` = `rds`, ou seja usam o
MESMO endpoint que RDS/Aurora. Três achados novos entraram na cobertura:
`AuroraDSQL`, `AWSM2` e `AmazonMCS` (Amazon Keyspaces) — este último tinha
sido descartado por engano numa versão anterior por supor, sem checar, que
só existia a API CQL (data-plane); na verdade o control-plane
(`CreateKeyspace`/`CreateTable`) é uma API REST normal.

**Os 16 que continuam `None`**, com contagem real de operações `Create*`
verificada via botocore (não estimativa): `AmazonSageMaker` (72!),
`AmazonQuickSight` (34), `AWSGlue` (31), `AmazonBedrockAgentCore` (29),
`AWSIoT` (32), `AmazonAppStream` (17), `AWSDataSync`/`AWSDeadlineCloud` (13
cada), `AmazonOmics` (12), `AWSSecurityHub` (11, nível de conta — não
"criação de recurso" no sentido usual), `AWSElasticDisasterRecovery` (6,
replicação contínua, nenhuma operação é uma criação de recurso "normal"),
`comprehend` (5, jobs/endpoints sem recurso persistente óbvio),
`AWSIoTSiteWise`, `AmazonDocDB`, `AmazonNeptune` (ambiguidade confirmada,
ver acima) e `AWSCodeStar` (descontinuado pela AWS, sem definição no
botocore instalado).

**Por que não foi resolvido 100%:** os 16 restantes são genuinamente
ambíguos (múltiplos tipos de recurso sem "o" recurso óbvio para PRM) ou têm
uma ambiguidade estrutural real (DocDB/Neptune) — mapear errado aqui tem
custo real (evento mapeado errado nunca dispara silenciosamente, ou o
recurso errado é tagueado como se fosse outro serviço). O CSV é atualizado
manualmente (decisão do gestor) — este mapeamento segue o mesmo modelo:
extensão incremental quando houver prioridade de negócio clara, não um
trabalho de "resolver tudo de uma vez".

**O que destrava:** priorizar, com o gestor/PDM, se algum dos 16 restantes
(SageMaker e QuickSight à parte — ambíguos demais para valer a pena sem uma
decisão de produto sobre qual sub-recurso importa) é usado o suficiente
pelos clientes da Darede para justificar escolher um recurso "principal"
por decisão de produto (não só técnica) e mapear só esse.

### Alerta ativo quando o CSV ganha um serviço sem mapeamento de evento

**RESOLVIDO em 2026-09-30 (Lote 4)** — subcomando `verificar-csv`
(`csv_check.py`): compara a tabela da página oficial com o CSV empacotado e
aponta códigos novos sem mapeamento. Roda centralmente (CI), não por conta.

**Classificação: melhoria futura.** Não bloqueia a Etapa 3 hoje (o teste
`validate_against_csv` já trava a divergência em desenvolvimento) e é
candidato natural para nascer junto da Etapa 4, não um pré-requisito dela.

**O quê:** `validate_against_csv()` já trava a divergência em tempo de
desenvolvimento (o teste `test_event_mapping.py` falha se o CSV tiver um
`product_service_code` sem nenhuma entrada na tabela). O que ainda não
existe é um alerta em PRODUÇÃO quando isso acontece — hoje o comportamento é
silencioso: o serviço simplesmente não é observado pela Etapa 3 até alguém
mapear.

**Por que não foi resolvido agora:** decisão do gestor — registrar como
melhoria em vez de implementar agora.

**O que destrava:** desenhar um mecanismo (ideia do gestor: um e-mail via
SNS) que rode periodicamente (ou no deploy) comparando o CSV vigente contra
`event_mapping.py` e alertando quando há um `product_service_code` novo sem
mapeamento — candidato natural para rodar junto da Etapa 4 quando ela for
implementada.

### Extractor do CloudFront (`CreateDistribution`) nunca testado contra evento real

**O quê:** de todos os serviços de protocolo `query`/`ec2`/`rest-xml`
(capitalização do CloudTrail incerta — ver docstring de `event_parser.py`),
só o CloudFront segue sem um evento real capturado em sandbox — os outros
8 já foram (`test/unit/test_event_parser_botocore.py` verifica
estruturalmente todos contra o botocore, sem custo de conta real; ver
[test/manual-live-etapa3/README.md](../test/manual-live-etapa3/README.md#parte-1--capturar-eventos-cloudtrail-reais-sem-implantar-a-stack)
para o resultado dos demais — 3 tinham bug real, já corrigidos).

**Por que não foi testado ainda:** custo de tempo, não de dinheiro — a
distribuição leva 15-20min pra propagar antes de poder ser desabilitada e
apagada.

**O que destrava:** capturar o evento `CreateDistribution` em sandbox
(mesmo roteiro da Parte 1) quando o tempo de espera for aceitável.
Continua com leitura tolerante a capitalização (`_direct_ci`/
`_constructed_ci`) como mitigação até lá, não substituto.

### Corrida com IaC — debounce de 30 minutos, sem validação de tempo real

**O quê:** `DebounceSeconds` (Step Functions `Wait`) tem default de 1800s
(30 minutos), combinado explicitamente com o gestor como ponto de partida.

**Por que não foi resolvido com mais precisão:** não há dado real de quanto
tempo as ferramentas de IaC usadas pelos clientes da Darede (Terraform,
CloudFormation, `eksctl`, etc.) levam entre criar um recurso e aplicar suas
próprias tags — 30 minutos é uma margem confortável, mas não validada.

**O que destrava:** observar, depois de alguns meses em produção, quantos
recursos resultam em `taguear` que na verdade eram gerenciados por IaC
(sinal indireto: o próprio IaC re-tagueando por cima na consolidação
seguinte causaria um falso "conflito" no relatório) — ajustar o debounce
para cima ou para baixo com esse dado.

### Buffer SQS entre EventBridge e o processamento — não implementado

**Classificação: melhoria futura.** Decisão consciente já tomada com o
gestor de manter o desenho simples por ora; reativa a dado de throttling em
produção que ainda não existe — não bloqueia a Etapa 3 nem é pré-requisito
de nenhuma etapa seguinte.

**O quê:** decisão explícita do gestor: manter invocação direta do Step
Functions pela regra do EventBridge, sem fila SQS no meio.

**Por que não foi resolvido agora:** decisão consciente de manter o desenho
mais simples por ora — SQS ajudaria a suavizar rajadas de criação de
recurso (ex.: Auto Scaling), mas adiciona um componente a mais e não há
dado real de que o volume atual de algum cliente justifique isso.

**O que destrava:** monitorar `Throttles`/erros de concorrência da função
Lambda (`Etapa3Function`) e do Step Functions em produção; se rajadas
causarem throttling real de API, inserir uma fila SQS entre a regra do
EventBridge e o Step Functions (ou entre o Step Functions e o Lambda).

### Load balancer/node/volume de EKS criados dinamicamente — deferido à Etapa 4

**O quê:** um Load Balancer criado por um Ingress Controller do EKS gera
sim um evento `elasticloadbalancing:CreateLoadBalancer` observável — mas,
no momento exato do evento, a tag de convenção que permitiria associá-lo a
um cluster específico (`elbv2.k8s.aws/cluster`) normalmente ainda não foi
aplicada (o controller faz isso numa chamada `AddTags` separada, logo
depois). Nodes/volumes EBS de node groups managed em geral JÁ vêm com essas
tags no próprio `RunInstances`/`CreateVolume` (via `TagSpecifications` do
launch template), mas isso não é garantido para todo node group.

**Por que não foi resolvido agora:** decisão explícita do gestor — esses 3
subtipos de recurso EKS ficam de fora da detecção por evento da Etapa 3;
continuam cobertos pelo scan periódico da Etapa 4 (fora do escopo deste
repositório), que já lê o estado final consolidado do recurso, sem essa
janela de corrida.

**O que destrava:** nada a fazer aqui — comportamento intencional. Só
revisitar se a Etapa 4 acabar não cobrindo esse gap na prática (ex.: se o
intervalo entre varreduras da Etapa 4 for longo demais para o SLA de
compliance desejado).

### Falha de extração de evento — decisão de alertar ainda em aberto

**Classificação: melhoria futura.** Hoje já se degrada com segurança (só
loga `WARNING`, nenhuma ação incorreta) — não bloqueia a Etapa 3; pode
reaproveitar o mesmo mecanismo do item "Alerta ativo quando o CSV ganha um
serviço sem mapeamento" acima quando esse for priorizado.

**O quê:** quando `event_parser.parse_creation_event` não consegue extrair
nenhum recurso (payload insuficiente, evento sem extractor confiável), o
handler hoje só loga um `WARNING` — não publica nada no SNS.

**Por que não foi resolvido agora:** decisão do gestor — registrar como
melhoria em vez de decidir agora entre "alertar via SNS" (mais visível, mas
gera ruído para casos esperados) e "só logar" (mais simples, mas depende de
alguém observar o CloudWatch Logs ativamente).

**O que destrava:** decisão de produto sobre o nível de alerta desejado
para esse caso, e possivelmente reaproveitar o mesmo mecanismo do item
"Alerta ativo quando o CSV ganha um serviço sem mapeamento" acima.

### DLQ para o alvo do EventBridge — RESOLVIDO em 2026-09-30 (Lote 2, uma DLQ por região; ver infra/README.md)

**Classificação: melhoria futura.** Mesma decisão do buffer SQS acima —
não bloqueia a Etapa 3 nem é pré-requisito de nenhuma etapa seguinte, só
vem natural junto se/quando uma fila for adicionada à arquitetura.

**O quê:** as regras do EventBridge têm `RetryPolicy` (2 tentativas,
`MaximumEventAgeInSeconds` configurável), mas nenhum
`DeadLetterConfig` — um evento que esgote as tentativas de entrega ao Step
Functions é simplesmente descartado, sem rastro.

**Por que não foi resolvido agora:** adicionar um DLQ de verdade exige uma
fila SQS dedicada (o EventBridge só suporta DLQ via SQS) — coerente com a
decisão de manter o desenho sem SQS por ora (ver item acima); ficou de fora
junto.

**O que destrava:** mesma decisão do buffer SQS acima — se/quando uma fila
for adicionada à arquitetura, o DLQ vem natural junto dela.

### A stack cobre só 1 região — qualquer recurso regional fora dela fica invisível (Route 53/CloudFront são só o caso mais extremo)

**RESOLVIDO em 2026-09-30 (Lote 2)** — `infra/template-hub.yaml` +
`infra/template-regional.yaml`, subcomando `preflight` para a lista de
regiões (sempre com `us-east-1`). Texto abaixo mantido como histórico da
decisão. Ver docs/plano-finalizacao.md.

**Classificação original: melhoria planejada — decisão de arquitetura já tomada
com o gestor (2026-09-25), implementação ainda não feita.**

**O quê:** uma regra de EventBridge só casa eventos da MESMA região onde
ela foi criada — isso vale para QUALQUER serviço regional (EC2, RDS, S3,
Lambda, todos os ~69 mapeados). Hoje `template.yaml` implanta a Etapa 3
numa única região por deploy — **todo recurso criado em qualquer outra
região da mesma conta fica invisível para a Etapa 3**, silenciosamente
(evento nunca chega, não é uma tentativa de escrita que falha com erro
visível). Contas de cliente que operam em mais de uma região (comum) têm
esse gap para a maioria dos recursos que criam, não só para Route
53/CloudFront.

**Route 53/CloudFront são um caso à parte, mais restritivo ainda**:
confirmado empiricamente em sandbox em 2026-09-24 (reproduzido: `aws
cloudtrail lookup-events` para `CreateHostedZone` devolveu 0 resultados
consultado em `us-east-2`, e 1 resultado — com `awsRegion: us-east-1`
gravado no próprio evento — na mesma consulta em `us-east-1`, mesmo com o
perfil AWS configurado para `us-east-2`) — a AWS entrega evento de
CloudTrail de serviço GLOBAL **só** ao barramento do EventBridge em
`us-east-1`, nunca ao de nenhuma outra região, mesmo que essa seja a
região "principal" da stack.

**Decisão registrada — opção (a) do leque anterior**: StackSet em todas
as regiões ativas da conta cliente, sempre incluindo `us-east-1` mesmo que
a conta não tenha recurso lá (única forma de cobrir Route
53/CloudFront). Descartadas: (b) aceitar o gap e delegar à Etapa 4 —
perde a reação em tempo real exatamente nas contas multi-região, que
tendem a ser as maiores/mais maduras; (c) fixar a região principal em
`us-east-1` — não resolve o caso regional geral.

**Desenho da implementação (ainda não feita):**

1. **Separar `template.yaml` em dois templates.** Hoje o tópico SNS
   (`PrmComplianceTopic`) vive na mesma stack que Lambda/Step
   Functions/EventBridge — duplicar tudo por região duplicaria o tópico
   também, quebrando a regra já registrada de que ele é peça
   compartilhada única por conta (ver docstring do template e a seção do
   e-mail de notificação acima). Precisa virar:
   - **Template "hub"** (implantado 1x por conta, região principal): só o
     tópico SNS + a assinatura de e-mail (`NotificationEmail`, ver acima).
   - **Template "regional"** (o que existe hoje, menos o tópico SNS):
     Lambda, Step Functions, EventBridge, IAM — recebe o ARN do tópico do
     hub como parâmetro simples (publicar num tópico SNS de outra região
     funciona apontando o client boto3 pra região do tópico — não precisa
     de `Fn::ImportValue` cross-region nem de nenhum truque).
2. **Descobrir as regiões ativas da conta cliente** para alimentar o
   StackSet — reaproveitar `regions.get_active_regions()` (já existe,
   mesmo código que a Etapa 1 usa), rodado uma vez por conta antes do
   deploy, **sempre forçando a inclusão de `us-east-1`** na lista mesmo
   quando a conta não reporta recurso lá.
3. **Pré-requisito de conta/organização**: StackSet de verdade (não só
   `sam deploy` manual por região) precisa de trusted access configurado
   entre a conta de gerenciamento e as contas de cliente (ou roles
   self-managed) — a confirmar se já está habilitado antes de implementar
   isso de fato.
4. **Custo**: praticamente nenhum de infra ociosa — Lambda/Step Functions
   só cobram por execução, EventBridge/IAM são gratuitos; o custo real é
   só mais peças pra gerenciar por conta.
5. **Validação**: precisa de um novo roteiro em sandbox (deploy em 2+
   regiões, confirmar que cada região só reage a evento local, e que as
   duas publicam no mesmo tópico do hub) antes de considerar resolvido —
   mesma disciplina já seguida para o resto da Etapa 3.

**O que destrava:** implementar o split de template (passo 1) e a
descoberta de regiões (passo 2) — a decisão de abordagem já está tomada,
falta só o trabalho.

### Volumes EBS criados junto com a instância (`RunInstances`) não são tagueados

**Classificação: gap de cobertura confirmado no código, correção de
escopo pequeno mas não trivial (precisa de evento real para confirmar o
shape).** `event_parser._ext_ec2_run_instances` (`event_parser.py`) só
extrai `instancesSet.items[].instanceId` do evento — nunca olha os volumes
EBS anexados na mesma chamada (`items[].blockDeviceMapping.items[].ebs.
volumeId` no shape esperado da API EC2, protocolo `ec2` com o mesmo
empacotamento `"xSet": {"items": [...]}` já documentado para esse
extractor). Toda instância criada com `RunInstances` tem pelo menos 1
volume root — hoje esse volume só é pego pela Etapa 1 (descoberta
completa), nunca pela Etapa 3 (reação em tempo real).

**Por que não foi corrigido agora:** o shape exato de
`blockDeviceMapping` num evento CloudTrail real de `RunInstances` não foi
capturado nesta sessão (não estava na lista priorizada de serviços
incertos, já que EC2 tem um shape de CloudTrail conhecido/estável para a
parte de instância — mas a parte de `blockDeviceMapping` especificamente
não foi verificada contra um evento real, só contra suposição de estrutura
análoga).

**O que destrava:** capturar um evento `RunInstances` real em sandbox
(mesmo padrão do roteiro em
[test/manual-live-etapa3/README.md](../test/manual-live-etapa3/README.md)),
confirmar o path exato de `blockDeviceMapping`/`ebs`/`volumeId`, estender
`_ext_ec2_run_instances` para devolver também os ARNs dos volumes, e
adicionar regressão em `test_event_parser_extracao.py`.

### Formas de criar recurso que não passam pelo evento esperado

**Classificação: melhoria futura de cobertura — decisão de priorização,
não bug de um evento mapeado errado.** Vários jeitos comuns de criar um
recurso não disparam o `eventName` que `event_mapping.py` espera para
aquele serviço, então passam batido pela Etapa 3 mesmo com o serviço
"mapeado":

- `ec2:CreateFleet` (usado por ferramentas de auto-scaling baseadas em
  Spot/Fleet, ex. Karpenter) não é `RunInstances` — instâncias criadas
  assim não geram o evento que o extractor de EC2 espera.
- `rds:RestoreDBInstanceFromDBSnapshot`/`RestoreDBInstanceToPointInTime`/
  `CreateDBInstanceReadReplica` são formas de criar um DB instance/cluster
  sem passar por `CreateDBInstance`/`CreateDBCluster`.
- `dynamodb:RestoreTableFromBackup`/`RestoreTableToPointInTime` — mesma
  lógica para DynamoDB.
- NAT Gateway (`ec2:CreateNatGateway`), Elastic IP
  (`ec2:AllocateAddress`) e snapshots (`ec2:CreateSnapshot`,
  `rds:CreateDBSnapshot` etc.) não têm evento mapeado — recursos comuns e
  de custo real que ficam fora da automação.

**Por que não foi corrigido agora:** cada um exigiria mapear um evento
novo (`event_mapping.py`) + um extractor novo (`event_parser.py`), o mesmo
processo já usado para os 69 mapeados — trabalho incremental de mesma
natureza do item "16 serviços sem mapeamento" acima, não algo a resolver
de uma vez. Karpenter/`CreateFleet` em particular merece prioridade alta
se algum cliente usa Karpenter para EKS (cenário comum), mas isso é
julgamento de produto sobre a base de clientes, não técnico.

**O que destrava:** priorizar com o gestor quais desses valem o esforço de
mapear agora vs. esperar dado real de uso pelos clientes da Darede —
`CreateFleet` (Karpenter) parece o candidato mais forte a entrar cedo,
dado quão comum é hoje em clusters EKS gerenciados com auto-scaling.

## Cobertura de recursos que nunca tiveram tag nenhuma

**RESOLVIDO em 2026-09-30 (Lote 2)** — `native_discovery.py`: listagem
pelas APIs nativas de cada serviço (somente leitura, sem criar índice do
Resource Explorer), unida ao resultado do `GetResources`. Texto abaixo
mantido como histórico.

**O quê:** `resourcegroupstaggingapi:GetResources` (usado por
`resource_discovery.discover_generic_resources`) não retorna recursos que
nunca receberam tag nenhuma, de nenhuma chave — documentação oficial da
AWS. Em clientes sem nenhuma governança de tags prévia, uma parte
significativa dos recursos pode nunca ter recebido tag nenhuma e fica
completamente invisível para a Etapa 1, em todas as etapas seguintes.
Limitação já documentada em
[arquitetura.md](arquitetura.md#resource_discoverypy) e no README raiz.

**Por que não foi resolvido agora:** a AWS recomenda o AWS Resource
Explorer (`tag:none`) para achar esses casos, mas isso exige um índice já
criado na conta — criar esse índice é uma escrita, o que quebraria a
premissa de "Etapa 1 100% somente-leitura".

**O que destrava:** decisão do gestor/PDM: vale abrir mão da garantia
"Etapa 1 nunca escreve" para fechar essa lacuna de cobertura (criando o
índice do Resource Explorer como parte da automação, ou como um passo de
setup separado e documentado à parte), ou a lacuna fica aceita como
limitação conhecida indefinidamente?

## Durabilidade da tag em recursos de EKS (nodes, load balancers, volumes EBS)

**O quê:** a Etapa 2c tagueia nodes/load balancers/volumes de EKS via API
diretamente nas instâncias/recursos em execução
(`tag_execution.py`/`ApiStrategy.GENERICO` e `ELB_LOAD_BALANCER`). Isso não
sobrevive a rotação de node pelo Auto Scaling Group: quando o ASG trocar os
nodes, os novos nascem sem a tag. O guia oficial AWS PRM (págs. 69-70)
recomenda caminhos diferentes, aplicados na origem, não via API pontual:

- Nodes: `TagSpecifications` no launch template.
- Load balancers: annotation no Ingress (ALB) ou no Service (NLB).
- Volumes EBS: `--extra-tags` no CSI driver.

Também não foi validado em sandbox se o AWS Load Balancer Controller
mantém uma tag aplicada por fora dele (via `elasticloadbalancing:AddTags`
direto) ou a remove na próxima reconciliação.

**Por que não foi resolvido agora:** não é um bugfix pontual — é trocar a
abordagem de tagueamento desses 3 sub-tipos de "via API, na Etapa
2b/2c" para "via configuração de infraestrutura" (launch template/
annotation/CSI driver), o que é um desenho diferente, fora do escopo de
`tag_execution.py` como está. A Etapa 4 (varredura recorrente, ainda não
implementada) mitiga parcialmente ao reaplicar a tag em drift detectado,
mas de forma reativa, não é o caminho recomendado pelo guia.

**O que destrava:** decisão sobre se vale a pena desenhar esse caminho
alternativo para os 3 sub-tipos de EKS (fora do fluxo genérico de
`tag_execution.py`), e teste em sandbox contra um cluster EKS real (managed
node group + AWS Load Balancer Controller) para confirmar o comportamento
de drift antes de desenhar a solução.

## Custom Resource do CloudFormation pode estourar o timeout do Lambda

**O quê:** o desenho atual (ver
[arquitetura-multicliente.md](arquitetura-multicliente.md#linha-de-execução))
prevê o Custom Resource da stack disparando mapeamento + tagueamento
inicial (Etapas 1 → 2a → 2c) na criação da stack. Em conta grande, isso
pode facilmente ultrapassar os 15 minutos máximos de uma execução Lambda,
travando a criação da stack e causando rollback.

**Por que não foi resolvido agora:** nenhum código de empacotamento Lambda
existe ainda neste repositório — as Etapas 1-2c hoje só rodam como CLI
local (ver tabela em
[arquitetura-multicliente.md](arquitetura-multicliente.md#linha-de-execução)).
Não há Custom Resource real para corrigir.

**O que destrava:** desenhar o Custom Resource para disparar o processo de
forma assíncrona (ex.: publicar um evento e responder `SUCCESS` de
imediato, com o mapeamento/tagueamento rodando à parte — Step Functions ou
uma segunda Lambda invocada de forma assíncrona) em vez de rodar tudo
dentro do próprio handler do Custom Resource. Fazer isso **antes** de
montar o template CloudFormation da stack, não depois.

## Amazon DocumentDB e Amazon Neptune aparecem como "Amazon RDS" no relatório

**O quê:** o namespace `rds` do ARN é compartilhado por Amazon RDS, Aurora,
DocumentDB e Neptune — todos usam `arn:aws:rds:...`. `services.py` já
documenta isso como limitação conhecida: sem uma chamada adicional à API de
cada engine (para inspecionar o atributo `Engine`), não há como diferenciar
com certeza pelo ARN. Hoje todos caem no rótulo "Amazon Relational Database
Service (RDS)".

**Por que não foi resolvido agora:** ao contrário do bug do VPC Lattice (já
corrigido), aqui a ambiguidade é real — resolver exige uma chamada de API
extra (`rds:DescribeDBInstances`/`DescribeDBClusters`) por recurso do
namespace `rds`, aumentando custo e complexidade da Etapa 1. Não afeta a
tag aplicada nem a atribuição de receita — só o rótulo `servico` no
relatório.

**O que destrava:** decidir se a distinção de engine no relatório importa o
suficiente para justificar chamadas de API extras na Etapa 1 (hoje 100%
via Resource Groups Tagging API, sem chamadas por-recurso).

## Escopo "taguear tudo" vs. o texto do guia PRM

**O quê:** o guia oficial recomenda taguear só recursos "directly used or
influenced by your partner solution" (pág. 17). Este projeto está
implementado para taguear todo recurso elegível do CSV, sem filtrar por uso
efetivo — decisão já confirmada para ambiente (prod vs. não-prod, ver
[arquitetura-multicliente.md](arquitetura-multicliente.md#escopo-de-ambiente)),
mas essa citação do guia é sobre outro eixo (quais recursos, não qual
ambiente).

**Por que não foi resolvido agora:** é decisão de negócio, não de código —
para um MSP como a Darede, taguear tudo é defensável, mas precisa de
confirmação explícita.

**O que destrava:** confirmação do gestor/PDM da AWS sobre se o escopo
"taguear tudo que é elegível pelo CSV" está alinhado com o texto do guia,
ou se precisa de um filtro adicional.

## Tag policies do AWS Organizations (Etapa 4)

**RESOLVIDO em 2026-09-30 (Lote 4)** — subcomando `auditar-org`
(`org_audit.py`), junto com a análise de SCP.

**O quê:** o guia oficial (Customer FAQ 6) cita tag policies do
Organizations como mecanismo relevante, além de SCP. A Etapa 4 (varredura
recorrente/auditoria) já prevê verificação de SCP relacionada à tag, mas
tag policies ainda não estavam no desenho.

**Por que não foi resolvido agora:** a Etapa 4 ainda não foi implementada
(nem desenhada em detalhe) — não há código para ajustar ainda.

**Status:** já incorporado ao desenho de alto nível em
[arquitetura-multicliente.md](arquitetura-multicliente.md#linha-de-execução)
(linha da Etapa 4 e visão "Compliance contínuo" do dashboard) — falta só
detalhar quando a Etapa 4 for de fato desenhada/implementada.

## CSV oficial e PDF do guia divergem nas notas do Bedrock

**O quê:** as notas de escopo do Amazon Bedrock no CSV oficial
(`data/resource-tagging-included-services.csv`) e no PDF do guia
(`reference/aws-prm-onboarding-guide.pdf`) não batem exatamente.

**Por que não foi resolvido agora:** não é uma decisão de código — precisa
de confirmação externa com a AWS/o guia sobre qual fonte é a atual/correta.

**O que destrava:** confirmar com a AWS (ou com quem mantém o
relacionamento do programa) qual versão é a vigente, e atualizar o CSV
(nunca reescrito à mão, sempre substituído pelo arquivo oficial) se
necessário.

## Códigos de erro "não encontrado" do ELBv2 sem confirmação em sandbox

**O quê:** `tag_execution._CODIGOS_RECURSO_NAO_ENCONTRADO` usa
`LoadBalancerNotFoundException`/`TargetGroupNotFoundException`/
`ListenerNotFoundException`/`RuleNotFoundException`/
`TrustStoreNotFoundException` (com sufixo `Exception`) — corrigido a partir
de uma versão anterior que tinha esses 5 códigos sem o sufixo, inconsistente
com os outros 3 códigos do mesmo conjunto (`ResourceNotFoundException`,
`ClusterNotFoundException`, `NodegroupNotFoundException`, todos com
sufixo) e com o padrão de API modelada do ELBv2 (shape name == Code).

**Por que não foi resolvido com confirmação total:** é uma aposta de alta
confiança baseada no padrão da API, não uma confirmação contra a AWS real —
não há acesso a uma conta com um load balancer/target group/listener/regra
apagado para testar contra o serviço de verdade.

**O que destrava:** testar em sandbox (apagar um load balancer referenciado
num relatório de decisão e rodar `apply --live`) e conferir o `Code` exato
que `elasticloadbalancing:DescribeTags`/`AddTags` devolve.

## Sinal mais preciso de "região sem Bedrock" (reduzir ruído de falhas_descoberta)

**O quê:** `discover_bedrock_resources` hoje trata QUALQUER `ClientError` ao
listar profiles (`AccessDenied` incluso) como falha de descoberta — corrigido
de uma versão anterior que assumia `AccessDeniedException`/
`UnrecognizedClientException` como "região sem Bedrock, esperado" e não
registrava nada (testado e comprovado errado: esconde falta de permissão
IAM real em todas as regiões, ver commit que introduziu esta correção).

**Por que não foi resolvido com mais precisão:** não há como confirmar sem
sandbox se existe um código de erro (ou outro sinal) que distinga com
segurança "região sem Bedrock habilitado" de "falta de permissão IAM" — por
ora, a resposta mais segura é reportar sempre e deixar a revisão humana
decidir, mesmo que isso gere entradas de `falhas_descoberta` para regiões
onde Bedrock de fato não está disponível (ruído aceitável perto do risco de
esconder uma falha de permissão real).

**Teste em sandbox (2026-09-23) — achado real, mas não conclusivo:** rodando
`map` contra uma conta AWS real (role `AdministratorAccess`, 17 regiões),
apareceu `AccessDeniedException` em 14 regiões ao mesmo tempo para
`bedrock:ListInferenceProfiles`, `tag:GetResources` E `eks:ListClusters` —
as 3 etapas de descoberta, todas com a mesma causa exata:

```
... with an explicit deny in a service control policy:
arn:aws:organizations::<org-id>:policy/<ou-id>/service_control_policy/<policy-id>
```

Ou seja, nesse caso específico o `AccessDenied` não era NEM "região sem
Bedrock" NEM "falta de permissão IAM na role" (a role tinha
`AdministratorAccess`) — era uma SCP da Organization restringindo região
(só liberando as 3 regiões onde a descoberta trouxe dados: `us-east-1`,
`us-east-2`, `us-west-2`), uma terceira causa que nem tinha sido cogitada
antes. Isso reforça que a decisão de sempre reportar (em vez de tentar
adivinhar a causa) foi a certa — uma exceção "esperta" baseada só nas 2
hipóteses originais teria classificado esse caso errado. Mas não resolve a
pergunta original: esse teste não isolou os 2 cenários hipotéticos (região
genuinamente sem Bedrock vs. permissão faltando sozinha, sem SCP no meio) —
segue em aberto.

**O que destrava:** testar em sandbox, isoladamente (sem uma SCP de região
no caminho): (1) uma região onde Bedrock realmente não está disponível e
(2) uma role sem `bedrock:ListInferenceProfiles` mas sem nenhuma SCP
bloqueando — comparando os erros exatos devolvidos. Se houver uma diferença
confiável (código, mensagem, ou outro sinal) entre os 2 e também frente ao
caso de SCP já observado, reintroduzir uma exceção mais criteriosa no
código.

## Falhas granulares dentro da descoberta de EKS ainda só ficam no log

**O quê:** `falhas_descoberta` agora cobre os pontos "largos" de
`discover_eks_resources` (listar clusters, descrever um cluster, listar ou
descrever um node group, listar load balancers da região). Falhas mais
granulares dentro do processamento de um cluster — um chunk de
`describe_instances`/`describe_volumes` (até 100 IDs por chamada), ou um
Auto Scaling Group isolado (`describe_auto_scaling_groups`) — continuam só
logadas, não aparecem em `falhas_descoberta`.

**Por que não foi resolvido agora:** o impacto é menor que os outros casos:
a mesma instância EC2/volume EBS de um node ainda aparece no relatório via
`discover_generic_resources` (namespace `ec2` não é excluído do passo
genérico) mesmo que o enriquecimento específico de EKS falhe — o recurso
não fica invisível, só perde o `tipo_recurso="node"`/`"ebs_volume"` mais
específico e a associação ao cluster. Instrumentar esses pontos também
exigiria encadear o acumulador de falhas por mais 4 funções auxiliares
(`_get_asg_instance_ids`, `_get_node_instance_ids_by_cluster_tags`,
`_instances_arns_and_tags`, `_volumes_arns_and_tags`), custo desproporcional
ao ganho nesse caso específico.

**O que destrava:** se no futuro o rótulo `tipo_recurso`/associação a
cluster desses casos passar a importar para alguma decisão automática (hoje
só é informativo no relatório), vale fechar essa lacuna também.

## Regex do valor esperado da tag aceita formato mais largo que o real

**O quê:** `main._EXPECTED_TAG_VALUE_PATTERN` (`^pc:[A-Za-z0-9]+$`) aceita
maiúsculas e qualquer tamanho depois do prefixo `pc:`. O único exemplo
confirmado do guia oficial (`pc:5ugbbrmu7ud3u5hsipfzug61p`) tem 25
caracteres minúsculos — se esse for o formato real de todo product code da
Darede, a regex deveria ser `^pc:[a-z0-9]{25}$`. O risco de deixar como
está: como a comparação de valor é case-sensitive e exata
(`tag_status.get_tag_status`), um erro de digitação em `--live` (ex.:
maiúscula trocada, um caractere a mais/a menos) tagueia a conta inteira com
um valor errado — e depois disso, toda tentativa de corrigir aparece como
`conflito` para o valor certo, nunca sobrescrito automaticamente.

**Por que não foi resolvido agora:** apertar a regex exige confirmar que
TODO product code de billing usado pela Darede segue exatamente esse
formato (25 caracteres, minúsculo) — um único exemplo do guia não é
confirmação suficiente para travar a validação de um jeito que rejeitaria
um formato válido não previsto.

**O que destrava:** confirmação do gestor/PDM sobre o formato exato (e
fixo) dos product codes usados pela Darede no PRM.

## Namespaces de serviços mais novos ausentes do mapeamento genérico

**O quê:** `services._NAMESPACE_TO_CODE` não tem entrada para
`emr-serverless` (EMR Serverless), `s3express` (S3 Express One Zone),
`mediapackagev2` (MediaPackage v2) nem `timestream-influxdb` (Timestream
for InfluxDB) — o CSV oficial também não tem uma linha própria para nenhum
dos 4, só para o serviço "pai" (Amazon EMR, Amazon S3, AWS Elemental
MediaPackage, Amazon Timestream). Hoje qualquer recurso desses 4 namespaces
é ignorado por completo pela descoberta genérica (`classify_arn` devolve
`None`).

**Por que não foi resolvido agora:** é plausível que esses 4 sejam
faturados sob o mesmo Product Service Code da linha-mãe do CSV, mas não é
garantido — a AWS pode faturar um serviço "v2"/"serverless" sob um código
de produto próprio, diferente do original. Mapear errado aqui não é
neutro: o recurso passaria a ser tagueado (então "descoberto" e contado)
sob um Product Service Code que pode não ser o real, distorcendo a
atribuição de receita em vez de só deixar o recurso de fora.

**O que destrava:** confirmar com a AWS (ou a documentação de billing
vigente) o Product Service Code real de cada um dos 4 antes de adicionar
qualquer entrada nova em `_NAMESPACE_TO_CODE`.

## Refatorações de código (sem risco de comportamento)

Dívida técnica pura — nenhuma delas muda o que o código faz, só como está
organizado. Baixa prioridade, sem prazo:

- **`tag_execution.py` está grande** (~450 linhas de código, boa parte do
  arquivo é docstring). Vale dividir em módulos menores (revalidação,
  executores, relatório) quando o arquivo crescer mais — hoje ainda é
  navegável.
- **Duplicação de constantes entre módulos** — `"Amazon EKS"`/`"Amazon
  Bedrock"` como string literal, o conjunto de tipos de IaC "detectado"
  (`_IAC_DETECTADO`), os `tipo_recurso` de EKS/Bedrock — repetidos em
  `decision.py`, `tag_execution.py`, `resource_discovery.py` e
  `single_resource.py` (Etapa 3). Um `constants.py` compartilhado
  resolveria, mas é uma mudança que toca vários módulos de uma vez. Além
  das constantes, `_chunk` (idêntica) e `_get_resources_page` (praticamente
  idêntica) existem hoje tanto em `resource_discovery.py` quanto em
  `tag_execution.py` — mesmo caso, um módulo utilitário compartilhado
  resolveria as duas coisas juntas.
- **`retry.py` é um retry próprio** — o botocore já oferece
  `Config(retries={"mode": "adaptive"})` nativamente. Trocar exigiria
  reavaliar se a diferenciação atual entre "erro retryable" (throttling) e
  "erro definitivo" (ex. `AccessDenied`, nunca re-tentado) tem paridade no
  modo adaptativo nativo antes de trocar.
- **`ou_tree._list_roots` reprocessa a paginação inteira em cada
  tentativa do retry** — ineficiência menor (não incorreção), específica
  desse ponto, não do decorator `with_backoff` em si.
- **Layout do repositório** — hoje o pacote Python é a própria raiz do
  repositório (`docs/`, `test/` dentro de `aws_prm_tagging/`), o que exige
  rodar o CLI/testes um nível acima dela (ver ["Onde rodar os
  comandos"](../README.md#onde-rodar-os-comandos) no README). Um layout com
  `pyproject.toml` + `src/aws_prm_tagging/` resolveria isso e ajudaria no
  empacotamento como Lambda Layer (ver Custom Resource acima).
- **`except Exception` genérico por região em `main.py`** — intencional
  (uma falha numa região/serviço não deve abortar a varredura inteira),
  mas é uma rede ampla que também engoliria um bug de programação real, não
  só falha de API. Menos arriscado agora que `falhas_descoberta` (ver
  `report.py`) torna essas falhas visíveis no relatório em vez de só no
  log — mas ainda vale considerar capturar exceções mais específicas
  (`ClientError`/`BotoCoreError`) e deixar bugs de verdade propagarem.

## Item avaliado e descartado — não rastreado

- **Checagem prévia do limite de 50 tags por recurso** (citado no guia):
  não vale a pena — se o limite for excedido, a chamada de escrita real já
  falha de forma limpa e cai no tratamento de erro genérico existente
  (`tag_execution.RESULTADO_ERRO`, com o código da AWS preservado em
  `detalhe_erro`). Uma checagem prévia só adiantaria a mesma informação por
  um caminho diferente, sem ganho real.
