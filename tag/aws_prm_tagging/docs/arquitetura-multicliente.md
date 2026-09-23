# Arquitetura de rollout multi-cliente (StackSets)

Este documento cobre a camada de orquestração acima da execução por conta
descrita em [producao.md](producao.md): como a automação PRM é distribuída
para os ~80-90 clientes da EMPRESAX, cada um com sua própria AWS Organization
isolada. `producao.md` responde "como rodar contra uma conta"; este
documento responde "como entregar isso em todas as contas de todos os
clientes, e como manter isso funcionando quando a estrutura de contas
muda".

Nada aqui altera o código de `aws_prm_tagging/` na Etapa 1. O motor de
descoberta já é executado por conta isolada — que é exatamente o modelo de
execução do StackSet descrito abaixo. Esta decisão informa como as Etapas
2-4 serão empacotadas, não como a Etapa 1 funciona hoje.

## Contexto de negócio

A EMPRESAX é uma parceira de consultoria AWS com múltiplos produtos de suporte
(MSP, FinOps, CaaS etc.) e aproximadamente 80-90 clientes, cada um com sua
própria AWS Organization separada. A EMPRESAX tem acesso administrativo a
todas as contas payer desses clientes.

Escopo confirmado: este projeto trata exclusivamente da tag exigida pelo
PRM (`aws-apn-id` = `pc:<product-code>`). Não é uma iniciativa de governança
de tags mais ampla da EMPRESAX.

## Modelo de execução

**Motor genérico único (config-driven)**, implantado via CloudFormation
através de **StackSets com service-managed permissions**, integrado ao AWS
Organizations de cada cliente.

- **Execução local por conta**: cada conta de cliente recebe, via StackSet,
  sua própria stack autocontida (IAM role local, Lambda(s), regras de
  EventBridge, tópico SNS). Não há Lambda central lendo cross-account — cada
  conta roda sua própria automação, reaproveitando os módulos de
  `aws_prm_tagging/` (ver [arquitetura.md](arquitetura.md)).
- **Reporte por push**: cada conta envia métricas/eventos para um destino
  central (Zabbix — ver seção "Dashboard central" abaixo), sem necessidade
  de acesso cross-account de leitura. Isso é o que permite ao modelo escalar
  para ~80-90 Organizations sem a EMPRESAX precisar assumir role em cada uma
  para ler resultados.
- **Pré-requisito único por cliente**: habilitar trusted access do
  StackSets com Organizations na conta payer daquele cliente. Isso é
  scriptável — feito uma vez por cliente, usando o acesso administrativo que
  a EMPRESAX já tem à payer, antes do primeiro deploy do StackSet. (Validar o
  comando exato/service principal contra a documentação AWS vigente no
  momento da implementação — o mecanismo é `organizations:EnableAWSServiceAccess`
  com o principal do serviço StackSets, via console CloudFormation > StackSets
  > "Enable trusted access" ou `aws organizations enable-aws-service-access`.)
- **Auto-deployment para novas contas**: contas que entram numa OU já
  mapeada herdam automaticamente o parâmetro (contrato) padrão daquela OU,
  via "automatic deployment" do StackSet. Isso é sinalizado no relatório
  recorrente (Etapa 4) para confirmação humana posterior — uma conta nova
  herdando um contrato por default não é considerada validada até alguém
  confirmar que o contrato herdado é o correto para aquela conta específica.

## Estrutura de clientes/contratos

- Sub-OUs geralmente separam **unidades de negócio/agências** dentro de um
  cliente (ex.: CLIENTE-X -> MARCA-A, MARCA-B), **não ambientes** — muitos clientes
  mantêm produção e desenvolvimento na mesma conta. Isso é consistente com a
  decisão de escopo de ambiente abaixo (tagueia-se tudo, sem distinção
  prod/dev).
- Pode haver **contratos diferentes por sub-OU** dentro do mesmo payer. O
  deploy do StackSet mira a sub-OU específica (nunca a OU-pai), cada uma com
  seu próprio parâmetro via **StackSet parameter override** — sem lógica
  condicional no código da automação. O parâmetro (valor esperado da tag,
  `pc:<product-code>`) é dado de configuração de infraestrutura, não de
  código, seguindo o mesmo princípio já aplicado em `main.py`
  (`--expected-tag-value` nunca hardcoded).
- **Escopo de participação**: decidido rodar por payer — a Organization do
  cliente é a unidade de decisão sobre se ele está em escopo — com o deploy
  do StackSet mirando as sub-OUs específicas dentro dela conforme o
  mapeamento de contrato.
- A hierarquia de OUs é descoberta automaticamente via API do Organizations
  — já implementado em `ou_tree.py` na Etapa 1, rodando a partir da conta de
  gerenciamento de cada cliente. Essa árvore é o insumo usado para decidir em
  qual sub-OU mirar cada instância do StackSet.
- **Em aberto**: a fonte do mapeamento "qual nó da árvore de OUs pertence a
  qual contrato/produto EMPRESAX" é informação de negócio, ainda a confirmar
  com o gestor. `ou_tree.py` entrega a estrutura; a ligação estrutura →
  contrato não está automatizada e não deve ser assumida — é dado externo a
  este projeto até essa fonte ser definida.
- **Em aberto**: quais dos ~80-90 clientes estão de fato em escopo para esta
  automação.

## Escopo de ambiente

**Decidido**: taguear tudo — produção e não-produção, sem distinção. Não há
penalidade formal documentada pela AWS por taguear recursos fora de
produção; essa era a única razão para considerar excluir ambientes
não-produtivos, e a decisão final elimina essa distinção. Nenhum filtro de
ambiente é necessário em nenhuma etapa da automação.

## Dashboard central

**Zabbix**. Duas visões complementares:

1. **Compliance contínuo**: cobertura de tag por cliente/conta
   (`ok`/`sem_tag`/`conflito`/`pular_iac`/`revisar_tag_similar`), contas
   usando valor padrão de OU ainda não confirmado (ver "auto-deployment"
   acima), feed de eventos de drift/remoção de tag (Etapa 4), proporção IaC
   vs. não-IaC, status de SCP e de tag policies do Organizations
   relacionadas à tag quando disponível (pendente aprovação do cliente),
   com filtros por cliente/conta/serviço/status. Alimentado pelo modelo de
   push descrito acima — cada conta envia seus próprios dados, sem leitura
   cross-account.

2. **Registro de cobertura/rollout**: uma linha por cliente com status de
   onboarding:

   `não iniciado -> bootstrap feito (trusted access habilitado) -> mapeamento de contrato definido -> stack implantada -> ativo e reportando -> erro/atenção`

   Diferente da visão de compliance contínuo: uma conta sem automação
   implantada não tem como se auto-reportar via push. Esta visão depende de
   duas fontes:
   - uma **lista mestra de todos os clientes** (fonte interna, ainda a
     definir);
   - uma **checagem ativa periódica**, usando credenciais de admin da EMPRESAX
     contra cada payer da lista mestra, verificando se o trusted access do
     StackSets está habilitado e se o StackSet tem instância implantada. É a
     única parte de toda a arquitetura que usa acesso administrativo
     cross-account de leitura, e é deliberadamente limitada a checar
     status de onboarding — não a ler recursos/tags do cliente (isso
     continua sendo feito localmente por conta, via push).

## Linha de execução

Detalhe funcional de cada etapa em [arquitetura.md](arquitetura.md) (módulo
por módulo) e no README raiz (uso do CLI). Nesta camada de rollout, o que
muda por etapa é a forma de empacotamento/distribuição — não a lógica em
si, que já está implementada e testada para 1/2a/2b/2c:

| Etapa | O que faz | Empacotamento |
|---|---|---|
| 1 — Mapeamento (`map`) | Descoberta somente-leitura, JSON de saída | CLI local/manual (estado atual deste repositório) |
| 2a — Decisão (`decide`) | Classifica cada recurso em `taguear`/`pular_iac`/`revisar_tag_similar`/`ja_ok`/`conflito` | CLI local/manual (estado atual deste repositório) |
| 2b — Tagueamento, dry-run (`apply`) | Simula a chamada de API por recurso `taguear`, sem escrever | CLI local/manual (estado atual deste repositório) |
| 2c — Tagueamento, execução real (`apply --live`) | Chama as APIs de escrita de verdade | CLI local/manual (estado atual deste repositório); ainda não validada contra sandbox nem com a lista completa de permissões IAM nativas por serviço — ver [producao.md](producao.md#permissões-iam-para-a-etapa-2c-apply---live-execução-real) |
| 3 — Automação contínua | EventBridge + Lambda reagindo a criação de recursos, validando tag existente antes de agir | Ainda não implementada — Lambda + regra EventBridge dentro da stack do StackSet |
| 4 — Varredura recorrente e auditoria | Repete a lógica da Etapa 1 periodicamente; alerta de drift/remoção via CloudTrail + EventBridge + SNS (near real-time) com a varredura periódica como backstop; verificação de SCP e de tag policies do Organizations (pendente aprovação do cliente); alimenta o dashboard | Ainda não implementada — Lambda agendada (EventBridge Scheduler) dentro da stack do StackSet |

As Etapas 1-2c hoje só rodam como CLI local, encadeadas manualmente por
quem executa (a saída em arquivo de uma alimenta a entrada da próxima) —
nenhuma delas está empacotada como Lambda ainda. As Etapas 3-4 (a
implementar) reaproveitam os módulos de `aws_prm_tagging/` (em particular
`resource_discovery.py`, `tag_status.py`, `iac_detection.py`, `ou_tree.py`,
`decision.py`, `tag_execution.py`) como dependência empacotada (ex.: Lambda
Layer) das funções Lambda implantadas por StackSet, evitando duplicar a
lógica de descoberta/decisão/execução entre etapas — e a Etapa 8 do
levantamento de code review (Custom Resource do CloudFormation disparando
o tagueamento inicial de forma assíncrona, para não esbarrar no limite de
15 minutos do Lambda em contas grandes) precisa ser resolvida junto do
desenho do empacotamento das Etapas 2b/2c como Lambda — ver
[melhorias-futuras.md](melhorias-futuras.md).

## Pontos em aberto (resumo)

Decisões de negócio/rollout, específicas desta camada multi-cliente:

- Fonte do mapeamento cliente/OU -> contrato/produto EMPRESAX.
- Lista mestra de clientes para o registro de rollout.
- Quais dos ~80-90 clientes estão de fato em escopo.
- Verificação de SCP e de tag policies do Organizations relacionadas à
  tag — pendente aprovação do cliente.
- Comando/service principal exato para habilitar trusted access do StackSets
  por cliente — validar contra a documentação AWS vigente na implementação.

Pendências técnicas do código já implementado (Etapas 1-2c) — não são
decisão de rollout, são registradas separadamente em
[melhorias-futuras.md](melhorias-futuras.md).
