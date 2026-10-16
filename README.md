# Automação de Tagueamento AWS PRM — MVP 1

Aplica e mantém a tag `aws-apn-id = pc:<product code>` nos recursos das
contas dos clientes, para o AWS Partner Revenue Measurement (PRM).

**Comece por [aws_prm_tagging/docs/MVP-1.md](aws_prm_tagging/docs/MVP-1.md)**
— o que é, como funciona, como implantar, operar e remover.

## Implantar numa conta (resumo)

```bash
# a partir desta pasta; pré-requisitos: Python 3.10+, AWS CLI v2, SAM CLI, make
pip install -r aws_prm_tagging/requirements-dev.txt
deploy/implantar_conta.sh --profile <perfil> --valor pc:<product-code> --email <email> --somente-mostrar
deploy/implantar_conta.sh --profile <perfil> --valor pc:<product-code> --email <email>
```

Tudo começa em dry-run: só a reação a recursos **novos** escreve a tag. Para
aplicar nos recursos existentes (com consentimento do cliente), ver a seção
"Como implantar" do documento.

## Estrutura

| Caminho | Conteúdo |
| --- | --- |
| `deploy/` | `implantar_conta.sh` e `remover_conta.sh` |
| `aws_prm_tagging/` | Código Python (CLI e Lambdas) |
| `aws_prm_tagging/infra/` | Templates `template-hub.yaml` e `template-regional.yaml` (SAM) |
| `aws_prm_tagging/docs/` | `MVP-1.md` (documento principal), arquitetura, produção, plano e histórico |
| `aws_prm_tagging/test/unit/` | 505 testes automatizados (`python3 -m pytest aws_prm_tagging/test/unit/`) |
| `aws_prm_tagging/test/GUIA-SANDBOX.md` | Roteiro de validação numa conta de teste |
| `Makefile` | Build das Lambdas, usado pelo `sam build` |

Versão: ver `VERSION`.
