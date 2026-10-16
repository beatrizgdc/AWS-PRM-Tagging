# Proveniência do CSV de serviços incluídos

`data/resource-tagging-included-services.csv` (usado em runtime) e a cópia em
`reference/` são idênticos.

**Versão atual — 2026-09-30 (94 linhas, 92 códigos distintos — AmazonEC2 e AmazonVPC aparecem em duas linhas cada).**
O link "download the Resource Tagging Included Services (CSV)" da página
oficial aponta para um zip que devolve 404 (confirmado em 2026-09-30, tanto
no caminho `pt_br` quanto no inglês). O CSV foi então gerado a partir da
tabela "Resource Tagging Included Services" publicada na própria página:
https://docs.aws.amazon.com/PRM/latest/aws-prm-onboarding-guide/resource-tagging-included-services.html

Convenções mantidas da versão anterior: mesmo cabeçalho, mesma ordem da
página, e nota vazia onde a página diz "None".

Diferenças para a versão anterior (86 linhas):

- 8 serviços novos: Amazon Connect Decisions (AWSSupplyChain), Amazon
  DynamoDB Accelerator (AmazonDAX), Amazon EVS (AmazonEC2), AWS End User
  Messaging (AmazonPinpoint), Amazon GameLift Streams
  (AmazonGameLiftStreams), AWS RTB Fabric (AWSRTBFabric), Amazon SES
  (AmazonSES), Amazon WorkSpaces Core Managed Instances
  (AmazonWorkSpacesInstances). Quatro deles (Connect Decisions, GameLift
  Streams, RTB Fabric, SES) trazem "Revenue attribution available from
  September 1, 2026".
- Notas alteradas: Amazon Bedrock (agora "Includes only Amazon models and
  Open Source models"), Amazon DynamoDB (deixou de dizer "Excludes DAX"),
  Amazon EC2 (inclui "Savings Plan for ML instance types").
- Nenhum serviço removido, nenhum código alterado.

Observação: em 2026-09-30 a versão em português da página (e a cópia que o
fetch automatizado enxergava da versão em inglês) ainda mostrava 90 linhas,
sem os 4 serviços de "September 1, 2026". A lista de 94 linhas veio da
página vista diretamente no navegador — tratada como a mais recente.

**Quando o zip oficial voltar a funcionar**, substituir os dois arquivos
pelo CSV oficial e rodar a suíte: os testes de `test_event_mapping.py`
apontam qualquer código novo sem entrada em `event_mapping.py`.
