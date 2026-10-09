# AWS PRM Tagging

Automação que encontra, classifica e aplica a tag `aws-apn-id` em recursos
AWS, atendendo ao **AWS Partner Revenue Measurement (PRM)**.

<img width="1195" height="633" alt="Screenshot_20261009_154755" src="https://github.com/user-attachments/assets/03168d3f-8dce-4066-8330-5b7a1080f46a" />

## O que faz

- **Mapeia** recursos elegíveis em todas as regiões da conta (somente leitura)
- **Decide** o que taguear, pular (IaC) ou revisar
- **Aplica** a tag em modo dry-run ou real
- **Automatiza** o tagueamento de novos recursos em tempo real (~70 serviços)
- **Notifica** por e-mail via SNS

## Stack

`Python` · `boto3` · `AWS Lambda` · `EventBridge` · `Step Functions` ·
`SNS` · `CloudTrail` · `IAM` · `AWS SAM` · `CloudFormation` · `pytest` ·
`cfn-lint` · `LocalStack`

## Destaques

- Arquitetura event-driven, sem acesso cross-account
- Detecção de recursos gerenciados por IaC (CloudFormation / Terraform)
- Mais de 240 testes unitários, 100% offline
- Validado em conta sandbox com deploy real

## Saiba mais

- [Documentação do projeto](aws_prm_tagging/README.md)
- [Arquitetura](aws_prm_tagging/docs/arquitetura.md)
- [Detalhes técnicos do repositório](DETALHES-TECNICOS.md)
