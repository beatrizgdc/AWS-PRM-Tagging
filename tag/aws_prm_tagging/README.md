# Mapeamento, decisão e tagueamento para o AWS Partner Revenue Measurement (PRM)

Automação de tagging AWS para atender a exigência de Resource Tagging do
programa AWS Partner Revenue Measurement (PRM). Este repositório cobre as
três primeiras Etapas de uma automação de **quatro Etapas**, mais um
início de implementação da quarta:

1. **Mapeamento** (Etapa 1, `map`) — descoberta somente-leitura de recursos
   e status da tag `aws-apn-id` por recurso.
2. **Decisão** (Etapa 2a, `decide`) — classifica cada recurso do relatório
   da Etapa 1 em `taguear` / `pular_iac` / `revisar_tag_similar` / `ja_ok`
   / `conflito`. Sem escrita — só decide, não chama nenhuma API de
   tagueamento.
3. **Tagueamento** (`apply`) — dois modos da mesma lógica
   (`tag_execution.run_tagging_execution`):
   - **Etapa 2b, dry-run (default)** — simula, recurso a recurso, a chamada
     de API que taguearia cada recurso `taguear` da Etapa 2a. Só faz
     chamadas de **leitura** na conta (revalidação do estado atual de cada
     recurso; desligável com `--no-revalidate`) — nunca escreve.
   - **Etapa 2c, execução real (`--live`)** — a mesma lógica de
     roteamento/agrupamento/revalidação, chamando as APIs de escrita de
     verdade. **Ainda não validada contra a conta sandbox nem com a lista
     completa de permissões IAM nativas por serviço** — ver
     [docs/producao.md](docs/producao.md#permissões-iam-para-a-etapa-2c-apply---live-execução-real)
     antes de usar `--live` contra qualquer conta real.
4. **Automação contínua** (Etapa 3) — Lambda acionado por regra(s) de
   EventBridge a cada criação de recurso em escopo, reaproveitando
   `decision.py` (Etapa 2a) e `tag_execution.py` (Etapa 2c) sem nenhuma
   lógica de decisão/execução duplicada. Cobre **69 dos ~85** serviços do
   CSV — cada `eventSource`/`eventName`/campo de extração verificado contra
   o `botocore` instalado (não vem de memória), com um teste próprio
   (`test_event_parser_botocore.py`) que trava qualquer divergência futura
   contra o shape real da API; os 16 que ficam de fora têm o motivo
   documentado linha a linha (ver [event_mapping.py](event_mapping.py) e
   [docs/melhorias-futuras.md](docs/melhorias-futuras.md)) — não são uma
   lacuna silenciosa. Suíte de testes (`test/unit/` — número de testes
   não fixado aqui de propósito, cresce a cada correção; rodar
   `pytest aws_prm_tagging/test/unit/ --collect-only` para o total exato.
   *OBS: revisar ao final do projeto se vale fixar um número aqui.*) e
   infraestrutura como código (SAM) em [infra/](infra/README.md) —
   **validada em sandbox em 2026-09-23/24** (`sam deploy` real e eventos
   CloudTrail reais capturados para 8 serviços, 5 bugs reais encontrados e
   corrigidos — ver [docs/melhorias-futuras.md](docs/melhorias-futuras.md)
   e [test/manual-live-etapa3/README.md](test/manual-live-etapa3/README.md)
   para o roteiro e resultado completos). Uma revisão de código posterior a
   essa validação corrigiu mais 3 pontos (lote de revalidação genérica,
   filtro de `errorCode` nos event patterns, condição `aws:TagKeys`) —
   **esses 3 ainda só foram validados estaticamente** (cfn-lint, `sam
   build`, suíte de testes), sem reconfirmação em sandbox. Varredura
   recorrente/auditoria (Etapa 4) continua fora do escopo deste
   repositório.

A Etapa 3 reaproveita os módulos escritos aqui (`aws_prm_tagging/`, núcleo
compartilhado — ver [docs/arquitetura.md](docs/arquitetura.md)) e é
empacotada como Lambda dentro de uma stack CloudFormation (ver
[infra/README.md](infra/README.md) para o escopo exato do que já está
implantável e o que ainda falta mesclar), executando localmente em cada
conta cliente (arquitetura sem acesso cross-account: cada conta roda sua
própria automação). A Etapa 4 segue o mesmo modelo, ainda não implementada.

## O que este script faz

Três subcomandos — `map` (Etapa 1) é 100% somente-leitura; `decide` (Etapa
2a) não toca em AWS nenhuma, só reclassifica um relatório já em disco;
`apply` sem `--live` (Etapa 2b) só faz chamadas de **leitura** (revalidação)
— com `--live` (Etapa 2c), passa a escrever de verdade a tag nos recursos
`taguear`, e só nesses. Roda localmente contra uma única conta AWS por vez,
usando um perfil de credenciais já configurado.

Para cada região comercial ativa da conta, enumera os recursos dos serviços
elegíveis ao PRM (lista oficial em
[`data/resource-tagging-included-services.csv`](data/resource-tagging-included-services.csv))
e classifica cada recurso quanto ao status da tag `aws-apn-id`
(`sem_tag` / `ok` / `conflito`) e uma heurística de IaC
(`cloudformation` / `terraform_heuristico` / `desconhecido`). Se executado a
partir da conta de gerenciamento de uma AWS Organization, também descobre a
árvore de OUs. O resultado é um único arquivo JSON.

> **Limitação conhecida:** a descoberta genérica usa
> `resourcegroupstaggingapi:GetResources`, que **não retorna recursos que
> nunca receberam tag nenhuma** (documentação oficial da AWS). Um recurso
> sem absolutamente nenhuma tag, de nenhuma chave, fica invisível para este
> relatório — só recursos que têm (ou já tiveram) alguma tag aparecem.
> Detalhes e o porquê da decisão de não resolver isso agora em
> [docs/arquitetura.md](docs/arquitetura.md#resource_discoverypy).

Falhas de descoberta (ex.: `AccessDenied` numa região) entram no relatório
como `falhas_descoberta`, não só no log — sem isso, "0 recursos" e "a
descoberta falhou aqui" seriam indistinguíveis no único artefato que a
Etapa 4/dashboard consome.

Documentação completa:

- [docs/arquitetura.md](docs/arquitetura.md) — como o projeto está estruturado, módulo por módulo, decisões de design e limitações conhecidas.
- [docs/producao.md](docs/producao.md) — como configurar credenciais, permissões IAM e executar contra uma conta cliente real.
- [docs/arquitetura-multicliente.md](docs/arquitetura-multicliente.md) — rollout via CloudFormation StackSets para os clientes da EMPRESAX (camada acima da execução por conta).
- [docs/melhorias-futuras.md](docs/melhorias-futuras.md) — pendências técnicas conhecidas e registradas, não corrigidas ainda (exigem decisão de arquitetura/produto ou têm custo maior que uma correção pontual).
- [test/localstack/README.md](test/localstack/README.md) — cenário de teste local contra LocalStack, sem tocar em nenhuma conta AWS real.
- [test/manual-live/README.md](test/manual-live/README.md) — smoke test manual da Etapa 2c (`apply --live`) contra 1 único recurso descartável, numa conta AWS real de sandbox.
- [test/manual-live-etapa3/README.md](test/manual-live-etapa3/README.md) — roteiro de validação da Etapa 3 (captura de evento CloudTrail real, smoke test de ponta a ponta com deploy da stack, investigação de CodeBuild) numa conta AWS real de sandbox — elaborado, não executado.

## Onde rodar os comandos

Este arquivo, `requirements.txt`, `docs/` e `test/` vivem dentro da própria
pasta do pacote Python (`aws_prm_tagging/` — a raiz deste repositório). Isso
não afeta instalar dependências ou navegar a documentação (comandos abaixo
já assumem que você está dentro desta pasta), mas **executar o CLI exige
rodar de um nível acima**: `python3 -m aws_prm_tagging.main` só resolve o
módulo `aws_prm_tagging.main` se o diretório de trabalho for o **diretório
que contém `aws_prm_tagging/`** (o pai direto desta pasta, qualquer que seja
o nome dele em quem clonou o repositório).

## Requisitos

- Python 3.10+
- Dependências em [requirements.txt](requirements.txt) (`boto3`)
- Um perfil de credenciais AWS já configurado (`~/.aws/credentials` /
  `~/.aws/config`, ou variáveis de ambiente padrão do AWS CLI/boto3)

Rodando a partir desta pasta (`aws_prm_tagging/`):

```bash
python3 -m pip install -r requirements.txt
```

## Configurar credenciais AWS

O script nunca lê credenciais de um arquivo próprio do projeto — ele usa a
cadeia de credenciais padrão do boto3/AWS CLI (ver
["Onde rodar os comandos"](#onde-rodar-os-comandos) e
[docs/arquitetura.md](docs/arquitetura.md#por-que-não-há-arquivo-de-variáveis-de-ambiente)
para a justificativa). Isso significa configurar um perfil nomeado **uma
vez**, fora do repositório, por qualquer um dos métodos abaixo.

`<perfil>` abaixo é um placeholder — escolha um nome (ex.: `sandbox`,
`cliente-x-readonly`) e use **esse mesmo nome literal**, sem os sinais `<`
`>`, em todos os comandos desta seção e no `--profile` do comando em
["Uso"](#uso) logo depois. Rodar os exemplos com `<perfil>` digitado ao pé
da letra (ou copiando `meu-perfil` de uma versão antiga deste README) resulta
em `ProfileNotFound: The config profile (...) could not be found` — o perfil
precisa existir de fato em `~/.aws/config`/`~/.aws/credentials` antes do
primeiro `python3 -m aws_prm_tagging.main`.

**Opção A — AWS CLI v2 instalado (mais simples):**

```bash
aws configure --profile <perfil>
```

O comando pede, em ordem: `AWS Access Key ID`, `AWS Secret Access Key`,
`Default region name` (ex.: `us-east-1`) e `Default output format` (ex.:
`json`). Isso grava as credenciais em `~/.aws/credentials` e a configuração
em `~/.aws/config`, sob a seção `[<perfil>]`.

**Opção B — editar os arquivos manualmente** (sem precisar do AWS CLI
instalado):

`~/.aws/credentials`:

```ini
[<perfil>]
aws_access_key_id = <SUA_ACCESS_KEY_ID>
aws_secret_access_key = <SUA_SECRET_ACCESS_KEY>
```

`~/.aws/config`:

```ini
[profile <perfil>]
region = us-east-1
output = json
```

**Opção C — role assumida a partir de um perfil base** (padrão recomendado
para rodar contra uma conta de cliente — detalhado em
[docs/producao.md](docs/producao.md#credenciais)):

`~/.aws/config`:

```ini
[profile <perfil>]
role_arn = arn:aws:iam::<ACCOUNT_ID>:role/NomeDaRole
source_profile = default
region = us-east-1
```

O boto3 assume a role e renova as credenciais temporárias automaticamente —
nenhuma mudança de código é necessária.

**Verificar que o perfil funciona:**

```bash
aws sts get-caller-identity --profile <perfil>
```

Deve retornar o `Account`, `UserId` e `Arn` correspondentes às credenciais
configuradas. Se este comando falhar, `python3 -m aws_prm_tagging.main map`
com o mesmo `--profile` também vai falhar, no mesmo ponto (`sts:GetCallerIdentity`
é a primeira chamada que o subcomando `map` faz — `decide` não usa boto3
nenhum, e `apply` usa a mesma credencial só para as chamadas de leitura de
revalidação, sem chamar `sts:GetCallerIdentity`).

**Alternativa sem `--profile`**: se você já usa variáveis de ambiente
(`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`,
`AWS_PROFILE`) ou está rodando em um ambiente com IAM role anexada (EC2, ECS,
Lambda), pode omitir `--profile` — o boto3 resolve a credencial pela cadeia
padrão automaticamente.

Permissões IAM mínimas necessárias (somente leitura) em
[docs/producao.md](docs/producao.md#permissões-iam-necessárias-somente-leitura).

## Uso

Pré-requisito: o perfil `<perfil>` já criado e verificado na seção
["Configurar credenciais AWS"](#configurar-credenciais-aws) acima —
substitua pelo nome real do seu perfil no comando abaixo. Rodando a partir
do diretório pai desta pasta (ver "Onde rodar os comandos"):

**Etapa 1 — mapeamento:**

```bash
python3 -m aws_prm_tagging.main map \
  --expected-tag-value pc:xxxxxxxxxxxxxxxxxxxxxxxxx \
  --profile <perfil> \
  --output relatorio.json
```

| Parâmetro | Obrigatório | Descrição |
|---|---|---|
| `--expected-tag-value` | sim | Valor esperado da tag `aws-apn-id` (formato `pc:<product-code>`, validado — `ra-...` e qualquer outro formato são recusados). Nunca hardcoded — varia por cliente/conta/OU. |
| `--profile` | não | Perfil de credenciais AWS configurado localmente. Se omitido, usa a cadeia padrão do boto3 (variáveis de ambiente, perfil `default`, IAM role). |
| `--output` | não | Caminho do arquivo JSON de saída (default: `prm_mapping_report.json`). |

**Etapa 2a — decisão** (consome a saída da Etapa 1; não usa boto3 nem
credenciais):

```bash
python3 -m aws_prm_tagging.main decide \
  --input relatorio.json \
  --expected-tag-value pc:xxxxxxxxxxxxxxxxxxxxxxxxx \
  --output decisao.json
```

**Etapa 2b — tagueamento em dry-run** (consome a saída da Etapa 2a;
reaproveita o `valor_tag_esperado` de dentro do próprio arquivo — não tem
`--expected-tag-value` próprio, de propósito, para nunca divergir do valor
usado na decisão):

```bash
python3 -m aws_prm_tagging.main apply \
  --input decisao.json \
  --profile <perfil> \
  --output resultado_dry_run.json
```

**Etapa 2c — execução real** (mesmo comando, só com `--live`):

```bash
python3 -m aws_prm_tagging.main apply \
  --input decisao.json \
  --profile <perfil> \
  --live \
  --output resultado_execucao.json
```

> ⚠️ Antes de rodar `--live` contra uma conta real, ver as permissões IAM de
> escrita e as ressalvas em
> [docs/producao.md](docs/producao.md#permissões-iam-para-a-etapa-2c-apply---live-execução-real)
> — a lista de permissões nativas por serviço ainda não foi validada uma a
> uma, e não há substituto para testar primeiro na conta sandbox.

| Parâmetro | Obrigatório | Descrição |
|---|---|---|
| `--input` | sim | Relatório JSON da Etapa 2a (saída de `decide`). |
| `--profile` | não | Igual à Etapa 1 — usado para as chamadas de leitura de revalidação e (com `--live`) as chamadas de escrita. |
| `--output` | não | Caminho do arquivo JSON de saída (default: `prm_apply_report.json`). |
| `--no-revalidate` | não | Desliga a revalidação do estado atual de cada recurso antes de agir sobre ele (ver [docs/arquitetura.md](docs/arquitetura.md#tag_executionpy)). **Recusado junto de `--live`** — em execução real, a revalidação é a única proteção contra sobrescrever um conflito. |
| `--live` | não | Executa de verdade (Etapa 2c) em vez de dry-run (Etapa 2b, default) — escreve a tag nos recursos `taguear`. |
| `--max-decision-age-hours` | não | Recusa agir se a descoberta (Etapa 1) por trás do relatório de decisão for mais velha que este limite, em horas (default: sem checagem). |

Nenhuma credencial, código de produto, ID de conta ou nome de cliente é
hardcoded em nenhum módulo — tudo entra via `--expected-tag-value`/`--profile`
ou é descoberto em runtime pelas próprias chamadas de API (conta, regiões,
árvore de OUs).

## Testes

Dois níveis, sem sobreposição:

- **`test/unit/`** — pytest, 100% offline (sem AWS, sem credencial nenhuma;
  os testes de `tag_execution.py`/Etapas 2b/2c usam sessões/clients boto3
  falsos em vez de rede real). Cobre a Etapa 2a (`decision.py`), as Etapas
  2b/2c (`tag_execution.py`, incluindo `LiveExecutor`), `iac_detection.py`,
  `report.py`, a validação pura de `main.py`, `services.classify_arn` e as
  funções puras de `resource_discovery.py` — importando `aws_prm_tagging`
  como pacote, por isso, ao contrário do resto deste README, roda do
  diretório **pai** desta pasta (mesma exigência de
  ["Onde rodar os comandos"](#onde-rodar-os-comandos) para o CLI):

  ```bash
  python3 -m pip install -r aws_prm_tagging/requirements-dev.txt
  python3 -m pytest aws_prm_tagging/test/unit/
  ```

  Um arquivo por área de responsabilidade (não um arquivo único crescendo
  sem limite) e fixtures compartilhadas em `conftest.py` — convenção
  detalhada em [test/unit/README.md](test/unit/README.md), inclusive para
  quem for adicionar testes de um módulo novo nas próximas etapas.

- **`test/localstack/`** — ponta a ponta contra LocalStack (nenhuma conta AWS
  real), cobre a Etapa 1. Detalhes em
  [test/localstack/README.md](test/localstack/README.md).

## Estrutura

```text
aws_prm_tagging/                             raiz deste repositório (pacote Python — este README vive aqui)
  reference/                                 material de referência (leitura humana, não lido pelo código)
    aws-prm-onboarding-guide.pdf               guia oficial AWS PRM
    resource-tagging-included-services.csv     CSV oficial fornecido pela AWS (cópia de leitura)
  data/                                      cópia do CSV usada em runtime (importlib.resources — fonte de verdade para o código)
  services.py                                carrega o CSV e classifica ARNs por serviço
  regions.py                                 descoberta de regiões comerciais ativas
  resource_discovery.py                      Resource Groups Tagging API + casos especiais (Bedrock, EKS)
  tag_status.py                              classificação sem_tag / ok / conflito
  iac_detection.py                           heurística de IaC
  decision.py                                Etapa 2a — decisão de tagueamento (taguear/pular_iac/revisar_tag_similar/ja_ok/conflito)
  tag_execution.py                           Etapas 2b (dry-run) e 2c (execução real) — roteamento de API, batching, revalidação
  tag_reads.py                               wrappers de leitura de tags nativas (EKS/Bedrock/ELBv2), compartilhados entre tag_execution.py e single_resource.py
  ou_tree.py                                 árvore de OUs da Organization
  report.py                                  monta o relatório JSON da Etapa 1
  retry.py                                   backoff exponencial para throttling
  main.py                                    CLI (subcomandos map / decide / apply)
  event_mapping.py                           Etapa 3 — mapeamento serviço do CSV -> evento(s) de criação, gera o event pattern do EventBridge
  event_parser.py                            Etapa 3 — extrai o(s) recurso(s) recém-criado(s) a partir do payload do evento
  single_resource.py                         Etapa 3 — lê o estado atual de UM recurso (sem descoberta completa)
  publish.py                                 publicação de resultados no SNS central (núcleo compartilhado — Etapa 3 e futura Etapa 4)
  handler_continuous_tagging.py              Etapa 3 — handler Lambda (I/O only; reaproveita decision.py e tag_execution.py)
  infra/                                     SAM/CloudFormation da Etapa 3 (Lambda, Step Functions, EventBridge, SNS) — ver infra/README.md
  requirements.txt                           dependências de runtime (boto3)
  requirements-dev.txt                       dependências de desenvolvimento (pytest, pyyaml)
  docs/                                      documentação de arquitetura, produção e rollout multi-cliente
  test/
    unit/                                    testes pytest (offline, sem AWS) — Etapas 2a, 2b, 2c e 3
    localstack/                              teste de ponta a ponta contra LocalStack (sem AWS real) — Etapa 1
```

Detalhes de cada módulo em [docs/arquitetura.md](docs/arquitetura.md).

**Sobre `reference/`:** o código nunca lê esses dois arquivos — são só
material de leitura humana. A única cópia do CSV usada em runtime é
`data/resource-tagging-included-services.csv`, carregada via
`importlib.resources` (ver [docs/arquitetura.md](docs/arquitetura.md)).
