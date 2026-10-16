# Tutorial rápido

A automação aplica a tag `aws-apn-id = pc:825bh0xhwumf14tg27cthnoss` (product
code da Darede, já é o padrão) e a mantém em recursos novos. Recursos de IaC
(Terraform, CloudFormation, CDK, Pulumi) nunca são tagueados; vão para o
relatório do cliente.

> Antes de taguear recursos existentes de verdade, tenha o consentimento do cliente.

## Preparar (uma vez)

Requisitos: Python 3.10+, AWS CLI v2, SAM CLI e `make`. Na pasta do projeto:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r aws_prm_tagging/requirements-dev.txt
aws login                 # ou: aws sso login --profile <perfil>
```

## Rodar

```bash
python3 iniciar.py
```

A primeira pergunta é o escopo:

| Opção | Quando | Perfil a usar |
| --- | --- | --- |
| **1 — uma conta** | Cliente sem organização, piloto, ou a própria payer | O da conta |
| **2 — várias contas** | Cliente com organização (o normal) | O da conta **payer** |

Num cliente com organização, use a opção 2 para as contas-membro e, se a payer
tiver recursos, a opção 1 nela (o StackSet não implanta na payer).

### Opção 1 — uma conta

O assistente faz tudo, com resumo e confirmação em cada passo:

1. Confirma a conta e escolhe as regiões.
2. Mapeia os recursos (só leitura).
3. Simula o tagueamento (só leitura).
4. Tagueia de verdade, só depois de você digitar o ID da conta.
5. Pergunta se deve implantar a automação para recursos novos. Respondendo
   sim, ela fica instalada e não há mais nada a rodar.

Os resultados ficam em `saida-prm-<conta>-<data>/`. O `*_cliente.md` é o
relatório para o cliente: recursos de IaC, que precisam da tag no código
dele, e o estado anterior dos que tinham outro valor ou chave parecida.

### Opção 2 — várias contas

O assistente confere que a conta é a payer, lista as OUs e pergunta o que fazer:

| Ação | O que acontece |
| --- | --- |
| **1 — implantar** | Empacota, publica o código na payer e instala a automação em todas as contas da OU (inclusive as que entrarem depois). Recursos novos passam a ser tagueados; os existentes ficam só em simulação, e cada conta manda um resumo. |
| **2 — aplicar** | Depois de revisar os resumos e com consentimento: as contas rodam de novo o tagueamento, agora escrevendo a tag nos recursos existentes. |
| **3 — status** | Situação das contas e regiões. |
| **4 — remover** | Tira a automação das contas da OU. |

Cada comando que escreve mostra o plano (o que cria, custo e como remover) e
pede que você digite `sim`.

## Remover uma conta avulsa

```bash
deploy/remover_conta.sh --profile <perfil>
```

As tags já aplicadas continuam nos recursos.

## Outro valor de tag?

Só para um contrato diferente: responda com o outro `pc:...` quando o
assistente perguntar o valor. Detalhes em
[aws_prm_tagging/docs/MVP-2.md](aws_prm_tagging/docs/MVP-2.md).
