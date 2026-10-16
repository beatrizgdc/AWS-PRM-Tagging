# Automação de Tagueamento AWS PRM — MVP 2

Aplica e mantém a tag `aws-apn-id = pc:<product code>` nos recursos das
contas dos clientes, para o AWS Partner Revenue Measurement (PRM).

**Comece por [aws_prm_tagging/docs/MVP-2.md](aws_prm_tagging/docs/MVP-2.md)**,
que traz o que mudou no MVP2. A base continua em
[MVP-1.md](aws_prm_tagging/docs/MVP-1.md): arquitetura, Etapas, operação e remoção.

## Uso rápido

Pré-requisitos: Python 3.10+, AWS CLI v2, SAM CLI e make.

```bash
pip install -r aws_prm_tagging/requirements-dev.txt

# Uma conta, passo a passo (mapear -> simular -> taguear -> implantar):
python3 iniciar.py

# Uma conta, direto (dry-run por padrão):
deploy/implantar_conta.sh --profile <perfil> --valor pc:<product-code> --email <email> --somente-mostrar

# Várias contas, por OU (na conta payer):
python3 deploy/stacksets/empacotar.py --versao 2.0.0
python3 deploy/stacksets/stackset.py publicar  --profile <payer> --dist dist/stacksets/2.0.0 --regioes <r1,r2,...>
python3 deploy/stacksets/stackset.py implantar --profile <payer> --dist dist/stacksets/2.0.0 --ou <ou-id> --valor pc:<código> --hub-regiao us-east-1
```

Tudo começa sem escrever nos recursos existentes. Só a reação a recursos
**novos** escreve a tag, e recursos de IaC (Terraform, CloudFormation, CDK,
Pulumi) nunca são tagueados: vão para o relatório do cliente.

## Estrutura

| Caminho | Conteúdo |
| --- | --- |
| `iniciar.py` | Modo interativo |
| `deploy/` | `implantar_conta.sh`, `remover_conta.sh` (uma conta) |
| `deploy/stacksets/` | `empacotar.py` e `stackset.py` (várias contas, por OU) |
| `aws_prm_tagging/` | Código Python (CLI e Lambdas) |
| `aws_prm_tagging/infra/` | Templates: `template-hub.yaml`, `template-regional.yaml` (por conta), `template-central.yaml` (payer), `template-zabbix.yaml`, e `zabbix/` (template de importação no Zabbix) |
| `aws_prm_tagging/requirements-lambda.txt` | SDK fixado (com hash) que vai na layer das Lambdas |
| `aws_prm_tagging/docs/` | `MVP-2.md`, `MVP-1.md`, arquitetura, produção, histórico |
| `aws_prm_tagging/test/unit/` | 608 testes automatizados (`python3 -m pytest aws_prm_tagging/test/unit/`) |
| `aws_prm_tagging/test/GUIA-SANDBOX-MVP2.md` | Roteiro de validação numa conta de teste |
| `Makefile` | Build das Lambdas e da layer, usado pelo `sam build` |

Versão: ver `VERSION`.
