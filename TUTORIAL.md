# Tutorial rápido

A automação aplica a tag `aws-apn-id = pc:825bh0xhwumf14tg27cthnoss` (product
code da Darede, já é o padrão) e a mantém em recursos novos.

Há **dois caminhos, um ou outro**. Escolha pelo número de contas:

| Situação | Caminho | Roda onde |
| --- | --- | --- |
| Cliente com **várias contas** numa organização (o normal) | **A — StackSets** | Conta payer do cliente |
| **Uma conta só**: cliente sem organização, piloto, ou a própria payer (StackSet não implanta na payer) | **B — modo interativo** | A própria conta |

Os dois fazem a mesma coisa no fim: taguear o que já existe e implantar a
reação a recursos novos. Recursos de IaC (Terraform, CloudFormation, CDK,
Pulumi) nunca são tagueados; vão para o relatório do cliente.

> Antes de taguear recursos existentes de verdade, tenha o consentimento do cliente.

## Preparar (uma vez)

Requisitos: Python 3.10+, AWS CLI v2, SAM CLI e `make`. Na pasta do projeto:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r aws_prm_tagging/requirements-dev.txt
aws login                 # ou: aws sso login --profile <perfil>
```

## A — Várias contas (StackSets, na payer)

```bash
python3 deploy/stacksets/empacotar.py --versao 2.1.0
python3 deploy/stacksets/stackset.py publicar  --profile <payer> --dist dist/stacksets/2.1.0 --regioes us-east-1,sa-east-1
python3 deploy/stacksets/stackset.py implantar --profile <payer> --dist dist/stacksets/2.1.0 --ou <ou-id> --hub-regiao us-east-1
python3 deploy/stacksets/stackset.py status    --profile <payer>
```

1. **empacotar** gera os artefatos no seu computador.
2. **publicar** envia esses artefatos para buckets da payer, um por região.
3. **implantar** instala a automação em todas as contas da OU. Contas que
   entrarem nela depois recebem a automação sozinhas.
   - Recursos **novos** passam a ser tagueados (~30 min após a criação).
   - Os **existentes** começam só em simulação: cada conta manda um resumo
     do que seria feito.
4. Revisados os resumos e com consentimento, aplique nos existentes: rode
   o mesmo `implantar` com `--modo-inicial live`.

Remover: `stackset.py remover --profile <payer> --ou <ou-id>`.

## B — Uma conta (modo interativo)

```bash
python3 iniciar.py --profile <perfil>
```

O assistente faz tudo, com resumo e confirmação em cada passo:

1. Confirma a conta e escolhe as regiões.
2. Mapeia os recursos (só leitura).
3. Simula o tagueamento (só leitura).
4. Tagueia de verdade, só depois de você digitar o ID da conta.
5. Por último, pergunta se deve implantar a automação para recursos novos.
   Respondendo sim, ela fica instalada e não há mais nada a rodar.

Os resultados ficam em `saida-prm-<conta>-<data>/`. O `*_cliente.md` é o
relatório para o cliente: recursos de IaC, que precisam da tag no código
dele, e o estado anterior dos que tinham outro valor ou chave parecida.

Remover: `deploy/remover_conta.sh --profile <perfil>`. As tags já aplicadas
continuam nos recursos.

## Outro valor de tag?

Só para um contrato diferente: `--valor pc:<outro>`. Detalhes em
[aws_prm_tagging/docs/MVP-2.md](aws_prm_tagging/docs/MVP-2.md).
