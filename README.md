# Automação de Tagueamento AWS PRM — MVP 2 (v1)

Aplica e mantém a tag `aws-apn-id = pc:<product code>` nos recursos das
contas dos clientes, para o AWS Partner Revenue Measurement (PRM).

**Para usar, veja o [TUTORIAL.md](TUTORIAL.md).** Detalhes em
[aws_prm_tagging/docs/MVP-2.md](aws_prm_tagging/docs/MVP-2.md).
A base continua em [MVP-1.md](aws_prm_tagging/docs/MVP-1.md) (arquitetura,
Etapas, operação e remoção); pendências em
[pendencias.md](aws_prm_tagging/docs/pendencias.md). Referência técnica do
código: [aws_prm_tagging/README.md](aws_prm_tagging/README.md).

O valor da tag tem padrão `pc:825bh0xhwumf14tg27cthnoss` (product code da
Darede); `--valor` só é necessário para outro contrato.

## Uso rápido

Pré-requisitos: Python 3.10+, AWS CLI v2, SAM CLI e make.

```bash
pip install -r aws_prm_tagging/requirements-dev.txt

# Uma conta, passo a passo (mapear -> simular -> taguear -> implantar):
python3 iniciar.py

# Uma conta, direto (dry-run por padrão):
deploy/implantar_conta.sh --profile <perfil> --email <email> --somente-mostrar

# Várias contas, por OU (na conta payer):
python3 deploy/stacksets/empacotar.py --versao 2.0.0
python3 deploy/stacksets/stackset.py publicar  --profile <payer> --dist dist/stacksets/2.0.0 --regioes <r1,r2,...>
python3 deploy/stacksets/stackset.py implantar --profile <payer> --dist dist/stacksets/2.0.0 --ou <ou-id> --hub-regiao us-east-1
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
| `aws_prm_tagging/docs/` | `MVP-2.md`, `MVP-1.md`, `pendencias.md`, `referencia/` (guia oficial do PRM) |
| `aws_prm_tagging/test/unit/` | Testes automatizados (`python3 -m pytest aws_prm_tagging/test/unit/`) |
| `aws_prm_tagging/test/sandbox/` | Roteiro e scripts de validação numa conta de teste |
| `Makefile` | Build das Lambdas e da layer, usado pelo `sam build` |

Versão: ver `VERSION`.
