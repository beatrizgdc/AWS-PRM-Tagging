# Teste de ponta a ponta contra LocalStack

Teste manual de integração do estágio 1 (mapeamento) executado durante o
desenvolvimento, documentado aqui para reprodução e para servir de
regressão ao alterar `aws_prm_tagging/`. Roda inteiramente contra o
[LocalStack](https://www.localstack.cloud/) — nenhuma chamada toca uma conta
AWS real, nenhuma credencial real é necessária.

## Por que este teste existe

`aws_prm_tagging/main.py` só pode ser validado de fato chamando APIs AWS
reais (Resource Groups Tagging API, EKS, Organizations etc.). Testar contra
uma conta AWS real exigiria uma conta descartável e ainda assim teria custo e
risco de tocar dados reais. O LocalStack emula essas APIs localmente em
Docker, permitindo validar o fluxo completo — descoberta de regiões,
paginação da Resource Groups Tagging API, classificação de tag, heurística de
IaC, descoberta de EKS e árvore de OUs — sem nenhum dos dois problemas.

## Cenário coberto

O script [`setup_test_resources.sh`](setup_test_resources.sh) cria, na conta
de teste do LocalStack (`000000000000`), região `us-east-1`:

| Recurso | Tag `aws-apn-id` | Resultado esperado |
|---|---|---|
| Instância EC2 "inst-ok" | `pc:test123` (igual ao esperado) | `status_tag = ok` |
| Instância EC2 "inst-sem-tag" | ausente | `status_tag = sem_tag` |
| Instância EC2 "inst-conflito" | `pc:outrovalor` + `aws:cloudformation:stack-name=minha-stack` | `status_tag = conflito`, `iac.tipo = cloudformation` |
| Instância EC2 "inst-terraform" | ausente + `Provisioner=Terraform` | `status_tag = sem_tag`, `iac.tipo = terraform_heuristico` (exercita o catch-all por valor, chave arbitrária) |
| Instância EC2 "inst-tag-similar" | `AWS-APN-ID` (case diferente) = `pc:test123` | `status_tag = sem_tag` (chave exata ausente), `tag_similar_encontrada = true`, `tag_similar_chaves = ["AWS-APN-ID"]` |
| Bucket S3 `prm-test-bucket-ok` | `pc:test123` | `status_tag = ok` |
| Cluster EKS `prm-test-cluster` | `pc:test123` | `status_tag = ok`, `servico = Amazon EKS` |
| Node group EKS `prm-test-ng` | `pc:test123` | `status_tag = ok`, `servico = Amazon EKS` |
| Organization com OU "Producao" e conta filha "Cliente Teste" | — | `arvore_ou` populada com a OU e a conta aninhadas |

A conta que roda o teste é automaticamente a conta de gerenciamento da
Organization (criada pelo próprio `setup_test_resources.sh`), então a
descoberta de árvore de OUs é exercitada no caminho feliz (não no caminho de
"pular com aviso").

O teste também exercita a resiliência a falha pontual: o LocalStack
community não implementa `bedrock:ListInferenceProfiles`
(retorna `InternalFailure`) — a execução deve logar o erro por região e
**continuar normalmente**, sem abortar. Isso é o comportamento esperado e faz
parte do critério de aceite, não uma falha do teste.

## Pré-requisitos

- Docker instalado e rodando.
- [LocalStack CLI](https://docs.localstack.cloud/getting-started/installation/):
  `python3 -m pip install localstack`.
- AWS CLI v2 instalado (usado pelo script de setup).
- Dependências do projeto instaladas (`python3 -m pip install -r ../../requirements.txt`,
  rodando a partir desta pasta, ou `-r aws_prm_tagging/requirements.txt` a
  partir da raiz do repositório).

## Como rodar

`run_test.sh` precisa ser executado de forma que `python3 -m aws_prm_tagging.main`
resolva o pacote — o próprio script já cuida disso internamente (`cd`
automático para a raiz do repositório antes de invocar o CLI), então ele
pode ser chamado de qualquer diretório, com o caminho completo:

```bash
bash aws_prm_tagging/test/localstack/run_test.sh
```

(a partir da raiz do repositório) ou:

```bash
bash run_test.sh
```

(a partir desta pasta, `aws_prm_tagging/test/localstack/`).

O script:

1. Verifica se `localstack` e `docker` estão disponíveis.
2. Cria o perfil AWS `localstack` em `~/.aws/config`/`~/.aws/credentials`
   automaticamente, caso ainda não exista (credenciais fake — o LocalStack
   não valida credenciais).
3. Sobe o LocalStack (`localstack start -d`) e aguarda o healthcheck.
4. Roda `setup_test_resources.sh` para criar o cenário acima.
5. Executa `python3 -m aws_prm_tagging.main map --expected-tag-value pc:test123
   --profile localstack` contra o LocalStack.
6. Imprime o resumo (`resumo`) do relatório gerado.

Para rodar as etapas manualmente (útil ao depurar), a partir da raiz do
repositório (`python3 -m aws_prm_tagging.main` exige isso — ver
[README.md](../../README.md#onde-rodar-os-comandos)):

```bash
localstack start -d
bash aws_prm_tagging/test/localstack/setup_test_resources.sh
python3 -m aws_prm_tagging.main map \
  --expected-tag-value pc:test123 \
  --profile localstack \
  --output aws_prm_tagging/test/localstack/relatorio_teste.json
```

## Resultado esperado

O bloco `resumo` do relatório gerado deve conter:

```json
{
  "total_recursos": 12,
  "por_status_tag": {
    "sem_tag": 7,
    "ok": 4,
    "conflito": 1
  },
  "por_status_iac": {
    "cloudformation": 1,
    "terraform_heuristico": 1,
    "desconhecido": 10
  },
  "por_servico": {
    "Amazon EC2": 9,
    "Amazon EKS": 2,
    "Amazon S3": 1
  },
  "total_tag_similar_encontrada": 1
}
```

Notas sobre os números:

- `total_recursos = 12` inclui os 5 recursos listados na tabela de cenário
  mais recursos "de fundo" que o próprio LocalStack expõe (ex.: AMIs padrão
  visíveis via `ec2:DescribeInstances`/Resource Groups Tagging API, e o
  security group padrão criado junto com a VPC do cluster EKS) — variações
  pequenas nesse número entre execuções não indicam regressão, desde que os
  3 status de tag (`ok`/`sem_tag`/`conflito`), o `terraform_heuristico = 1`,
  o `total_tag_similar_encontrada = 1` e os 2 recursos EKS (`cluster` +
  `nodegroup`) apareçam.
- `arvore_ou` não deve ser `null`: deve conter um root com uma OU "Producao"
  contendo a conta "Cliente Teste".
- O log da execução deve conter uma linha `ERROR` por região mencionando
  `ListInferenceProfiles` (Bedrock não suportado pelo LocalStack community) e,
  logo depois, a execução deve prosseguir e terminar com
  `Concluído. N recursos mapeados em <N regiões>` — se a execução abortar
  nesse ponto, é uma regressão no tratamento de erro de
  `discover_bedrock_resources`.

## Limpeza

```bash
bash aws_prm_tagging/test/localstack/cleanup.sh
```

Para o container do LocalStack (`localstack stop`) e remove o relatório de
teste gerado. O LocalStack community roda sem persistência por padrão, então
parar o container já descarta todos os recursos de teste — não é necessário
excluir instâncias/buckets/cluster manualmente.

## Limitações deste teste

- Valida o *código* do estágio 1 (paginação, classificação, tratamento de
  erro, formato do relatório), não valida atribuição de receita real — isso é
  calculado pelo backend da AWS a partir de dados de billing reais, fora do
  escopo deste repositório.
- O LocalStack community não implementa Bedrock `ListInferenceProfiles` nem
  simula o lançamento real de instâncias EC2 por um node group EKS managed
  (por isso o node group de teste não tem instâncias/volumes/load balancer
  associados) — a lógica de `_get_asg_instance_ids`,
  `_instances_arns_and_tags`, `_volumes_arns_and_tags` e
  `_load_balancers_for_cluster` em `resource_discovery.py` fica coberta pela
  leitura de código e por este teste apenas no caminho "zero resultados",
  não no caminho "com instâncias reais". Validação desses caminhos
  específicos, se necessária, requer um cluster EKS managed real (fora do
  escopo deste teste local).
