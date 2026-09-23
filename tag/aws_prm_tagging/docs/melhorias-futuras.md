# Melhorias futuras (pendências técnicas)

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

## Cobertura de recursos que nunca tiveram tag nenhuma

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
para um MSP como a EMPRESAX, taguear tudo é defensável, mas precisa de
confirmação explícita.

**O que destrava:** confirmação do gestor/PDM da AWS sobre se o escopo
"taguear tudo que é elegível pelo CSV" está alinhado com o texto do guia,
ou se precisa de um filtro adicional.

## Tag policies do AWS Organizations (Etapa 4)

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
confirmado do guia oficial (`pc:xxxxxxxxxxxxxxxxxxxxxxxxx`) tem 25
caracteres minúsculos — se esse for o formato real de todo product code da
EMPRESAX, a regex deveria ser `^pc:[a-z0-9]{25}$`. O risco de deixar como
está: como a comparação de valor é case-sensitive e exata
(`tag_status.get_tag_status`), um erro de digitação em `--live` (ex.:
maiúscula trocada, um caractere a mais/a menos) tagueia a conta inteira com
um valor errado — e depois disso, toda tentativa de corrigir aparece como
`conflito` para o valor certo, nunca sobrescrito automaticamente.

**Por que não foi resolvido agora:** apertar a regex exige confirmar que
TODO product code de billing usado pela EMPRESAX segue exatamente esse
formato (25 caracteres, minúsculo) — um único exemplo do guia não é
confirmação suficiente para travar a validação de um jeito que rejeitaria
um formato válido não previsto.

**O que destrava:** confirmação do gestor/PDM sobre o formato exato (e
fixo) dos product codes usados pela EMPRESAX no PRM.

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
- **Duplicação de constantes E de funções entre módulos** — `"Amazon
  EKS"`/`"Amazon Bedrock"` como string literal, o conjunto de tipos de IaC
  "detectado" (`_IAC_DETECTADO`), os `tipo_recurso` de EKS/Bedrock —
  repetidos em `decision.py`, `tag_execution.py` e `resource_discovery.py`.
  Um `constants.py` compartilhado resolveria, mas é uma mudança que toca os
  3 módulos de uma vez. Além das constantes, `_chunk` (idêntica) e
  `_get_resources_page` (praticamente idêntica) existem hoje tanto em
  `resource_discovery.py` quanto em `tag_execution.py` — mesmo caso, um
  módulo utilitário compartilhado resolveria as duas coisas juntas.
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
