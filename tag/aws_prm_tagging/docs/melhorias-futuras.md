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
  `decision.py`, `tag_execution.py` e `resource_discovery.py`. Um
  `constants.py` compartilhado resolveria, mas é uma mudança que toca os 3
  módulos de uma vez.
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
