# Pendências e melhorias

Itens em aberto na v1 do MVP2. Cada um diz o que falta e o que destrava.

## Dependem de acesso ou de informação externa

| Pendência | O que destrava |
| --- | --- |
| **Zabbix:** testar contra o servidor real | Versão, URL/host, rota de rede e token de API |
| **StackSets de ponta a ponta** | Conta payer de uma organização de teste |
| **Lista OU → contrato** de cada cliente | Negócio (o `pc:` padrão é o da Darede; OU com outro contrato usa `--valor`) |
| **`auditar-org` e modo `live` em escala** | Conta de cliente, com consentimento |
| **Extractor do CloudFront** (`CreateDistribution`): único de protocolo XML sem evento real capturado. A leitura já é tolerante a capitalização | Capturar o evento numa conta de teste (a distribuição leva ~20 min para propagar e ser removida) |
| **Códigos "não encontrado" do ELBv2** (`LoadBalancerNotFoundException` etc.) seguem o padrão da API, sem confirmação real | Rodar `apply --live` sobre um LB apagado e conferir o `Code` |
| **Notas do Bedrock divergem entre CSV e PDF do guia** | Confirmar com a AWS qual é a vigente |
| **Namespaces sem linha própria no CSV** (`emr-serverless`, `s3express`, `mediapackagev2`, `timestream-influxdb`) hoje são ignorados | Confirmar o Product Service Code de faturamento de cada um |
| **Escopo "taguear tudo que é elegível"** vs. o guia ("directly used or influenced by your partner solution") | Confirmação com o PDM da AWS |

## Comportamento a acompanhar em produção

- **Terraform antigo sem sinal:** recurso criado há mais de 90 dias, sem alteração recente nem tag de convenção, não é detectado como IaC (o histórico do CloudTrail guarda 90 dias). Mitigação: recomendar `default_tags`; o drift mostra "alterado por IaC" se o `apply` remover a tag. Evolução possível: ler o state do Terraform (S3), com autorização do cliente.
- **Velocidade do histórico do CloudTrail:** `LookupEvents` aceita 2 chamadas/s por região (~11 min para 470 recursos). A Lambda usa 300 s por região; o excedente fica `nao_verificado` e cai na heurística de tags. Se for comum, paralelizar por região ou consultar por `EventName` em vez de por recurso.
- **Disputa com outro parceiro:** o drift só alerta (`possivel_disputa_com_outro_parceiro`). Se virar rotina, avaliar "não sobrescrever de novo em N dias".
- **Debounce de 30 min** contra corrida com IaC: margem confortável, não medida. Ajustar com dados reais.
- **Buffer SQS** entre EventBridge e Step Functions: não implementado. Inserir se aparecer throttling em rajadas (Auto Scaling).
- **Falha de extração de evento** só vai para o log; a varredura é o backstop. Decidir se vale alerta por SNS.
- **Relatório do cliente na Lambda:** o SNS leva só uma amostra (25 por região). Se precisar da lista completa pela automação contínua, gravar num bucket da conta.
- **EKS:** LB/nodes/volumes criados dinamicamente e a durabilidade da tag dependem da configuração na origem (`recomendar`). Confirmar num cluster real se o Load Balancer Controller remove tag aplicada por fora.
- **Região sem Bedrock vs. falta de permissão:** os dois aparecem como falha de descoberta (seguro, com algum ruído). Não há sinal confiável que os distinga.
- **Falhas granulares de EKS** (um ASG, um lote de instâncias) só vão para o log; os EC2/EBS ainda vêm do passo genérico.
- **Formato do valor:** a validação aceita `pc:[A-Za-z0-9]+`. Com o product code confirmado (25 caracteres minúsculos), dá para apertar para `^pc:[a-z0-9]{25}$` se todos os contratos seguirem esse formato.

## Refatorações (sem mudança de comportamento)

- Dividir `tag_execution.py` (revalidação, executores, relatório).
- Constantes repetidas (`"Amazon EKS"`, tipos de IaC, `tipo_recurso`) e `_chunk`/`_get_resources_page` duplicados num módulo comum.
- Avaliar o retry adaptativo do botocore no lugar de `retry.py`.
- Layout com `pyproject.toml` + `src/`, para não depender de rodar da pasta acima do pacote.
- Trocar o `except Exception` por região em `main.py` por exceções de API específicas.
