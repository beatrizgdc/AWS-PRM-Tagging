# Testes unitários

Pytest, 100% offline — sem boto3, sem credencial, sem nenhuma conta AWS
real. Cobre os módulos puros do pacote (hoje só `decision.py`, Etapa 2a; ver
[test/localstack/README.md](../localstack/README.md) para o teste e2e da
Etapa 1, que precisa de boto3 + LocalStack).

Rodar (do diretório que contém `aws_prm_tagging/` — ver ["Onde rodar os
comandos"](../../README.md#onde-rodar-os-comandos) no README raiz):

```bash
python3 -m pip install -r aws_prm_tagging/requirements-dev.txt
python3 -m pytest aws_prm_tagging/test/unit/
```

## Convenção para novos arquivos de teste

Um arquivo por área de responsabilidade do módulo testado, não um arquivo
gigante por módulo — nomeado `test_<módulo>_<área>.py`. Para `decision.py`
(Etapa 2a):

- `test_decision_precedencia.py` — regra de precedência de
  `classify_resource` (tag ausente vs. presente, papel do IaC, tag com
  grafia parecida).
- `test_decision_escopo.py` — filtragem de escopo por `tipo_recurso` (os 5
  sub-tipos de EKS, exclusão de Fargate e de Bedrock fora de application
  inference profile).
- `test_decision_relatorio.py` — `build_decision_report` no nível de lote:
  robustez a entrada malformada, resumo agregado, múltiplos
  `expected_tag_value` sem vazamento de estado.

Ao adicionar um módulo novo (Etapa 2b/2c em diante), crie um novo grupo de
arquivos `test_<módulo>_<área>.py` seguindo o mesmo padrão, em vez de
acrescentar num arquivo já existente de outro módulo.

Fixtures compartilhadas (construção de recurso/relatório no formato da
Etapa 1) ficam em [`conftest.py`](conftest.py) — reaproveite `recurso_factory`
e `relatorio_etapa1_factory` em vez de recriar esses dicts à mão em um novo
arquivo.

Toda função de teste tem uma docstring curta explicando o *porquê* do
cenário (não só repetindo o nome) — mantenha esse padrão nos testes novos;
é o que torna a suíte legível como documentação das regras de negócio, não
só como verificação.
