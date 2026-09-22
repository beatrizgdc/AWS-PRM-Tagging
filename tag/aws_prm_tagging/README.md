# Mapeamento e decisão de tagueamento para o AWS Partner Revenue Measurement (PRM)

Automação de tagging AWS para atender a exigência de Resource Tagging do
programa AWS Partner Revenue Measurement (PRM). Este repositório cobre os
dois primeiros passos de uma automação de **quatro estágios**:

1. **Mapeamento** (Etapa 1, neste repositório) — descoberta somente-leitura
   de recursos e status da tag `aws-apn-id` por recurso.
   - **Etapa 2a — decisão** (também neste repositório, `decision.py`):
     classifica cada recurso do relatório da Etapa 1 em `taguear` /
     `pular_iac` / `ja_ok` / `conflito`. Ainda 100% sem escrita — só decide,
     não chama nenhuma API de tagueamento.
2. Tagueamento inicial — Etapas 2b/2c (dry-run e execução real via API,
   ainda não implementadas): aplicam a tag nos recursos que a Etapa 2a
   classificou como `taguear`.
3. Automação contínua para novos recursos.
4. Varredura recorrente / auditoria.

As Etapas 2b a 4 reaproveitam os módulos escritos aqui (`aws_prm_tagging/`,
núcleo compartilhado — ver [docs/arquitetura.md](docs/arquitetura.md)) e
serão empacotadas como Lambda dentro de uma stack CloudFormation, executando
localmente em cada conta cliente (arquitetura sem acesso cross-account: cada
conta roda sua própria automação).

## O que este script faz

100% somente-leitura. Não cria, modifica ou remove nenhum recurso ou tag.
Roda localmente contra uma única conta AWS por vez, usando um perfil de
credenciais já configurado.

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

Documentação completa:

- [docs/arquitetura.md](docs/arquitetura.md) — como o projeto está estruturado, módulo por módulo, decisões de design e limitações conhecidas.
- [docs/producao.md](docs/producao.md) — como configurar credenciais, permissões IAM e executar contra uma conta cliente real.
- [docs/arquitetura-multicliente.md](docs/arquitetura-multicliente.md) — rollout via CloudFormation StackSets para os clientes da EMPRESAX (camada acima da execução por conta).
- [test/localstack/README.md](test/localstack/README.md) — cenário de teste local contra LocalStack, sem tocar em nenhuma conta AWS real.

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
configuradas. Se este comando falhar, `python3 -m aws_prm_tagging.main` com
o mesmo `--profile` também vai falhar, no mesmo ponto (`sts:GetCallerIdentity`
é a primeira chamada que o script faz).

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

```bash
python3 -m aws_prm_tagging.main \
  --expected-tag-value pc:xxxxxxxxxxxxxxxxxxxxxxxxx \
  --profile <perfil> \
  --output relatorio.json
```

| Parâmetro | Obrigatório | Descrição |
|---|---|---|
| `--expected-tag-value` | sim | Valor esperado da tag `aws-apn-id` (formato `pc:<product-code>`). Nunca hardcoded — varia por cliente/conta/OU. |
| `--profile` | não | Perfil de credenciais AWS configurado localmente. Se omitido, usa a cadeia padrão do boto3 (variáveis de ambiente, perfil `default`, IAM role). |
| `--output` | não | Caminho do arquivo JSON de saída (default: `prm_mapping_report.json`). |

Nenhuma credencial, código de produto, ID de conta ou nome de cliente é
hardcoded em nenhum módulo — tudo entra via `--expected-tag-value`/`--profile`
ou é descoberto em runtime pelas próprias chamadas de API (conta, regiões,
árvore de OUs).

## Testes

Dois níveis, sem sobreposição:

- **`test/unit/`** — pytest, 100% offline (sem AWS, sem credencial nenhuma).
  Cobre hoje a Etapa 2a (`decision.py`), importando `aws_prm_tagging.decision`
  como pacote — por isso, ao contrário do resto deste README, roda do
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

```
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
  decision.py                                Etapa 2a — decisão de tagueamento (taguear/pular_iac/ja_ok/conflito)
  ou_tree.py                                 árvore de OUs da Organization
  report.py                                  monta o relatório JSON da Etapa 1
  retry.py                                   backoff exponencial para throttling
  main.py                                    CLI (entrypoint da Etapa 1)
  requirements.txt                           dependências de runtime (boto3)
  requirements-dev.txt                       dependências de desenvolvimento (pytest)
  docs/                                      documentação de arquitetura, produção e rollout multi-cliente
  test/
    unit/                                    testes pytest (offline, sem AWS) — Etapa 2a
    localstack/                              teste de ponta a ponta contra LocalStack (sem AWS real) — Etapa 1
```

Detalhes de cada módulo em [docs/arquitetura.md](docs/arquitetura.md).

**Sobre `reference/`:** o código nunca lê esses dois arquivos — são só
material de leitura humana. A única cópia do CSV usada em runtime é
`data/resource-tagging-included-services.csv`, carregada via
`importlib.resources` (ver [docs/arquitetura.md](docs/arquitetura.md)).
