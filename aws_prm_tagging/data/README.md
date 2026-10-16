# CSV de serviços incluídos

`resource-tagging-included-services.csv` é a fonte de verdade dos serviços
elegíveis ao PRM (lido em runtime por `services.py`).

**Versão atual: 2026-09-30** (94 linhas, 92 códigos distintos; AmazonEC2 e
AmazonVPC aparecem em duas linhas cada).

O link de download do CSV na página oficial devolvia 404, então o arquivo
foi gerado a partir da tabela "Resource Tagging Included Services" da
própria página, mantendo cabeçalho, ordem e nota vazia onde a página diz
"None":
https://docs.aws.amazon.com/PRM/latest/aws-prm-onboarding-guide/resource-tagging-included-services.html

## Atualizar

1. `python3 -m aws_prm_tagging.main verificar-csv` aponta o que mudou na
   página (código de saída 3 = diferença).
2. Atualize o CSV e rode a suíte: `test_event_mapping.py` falha se um código
   novo não tiver entrada em `event_mapping.py`.
