# Testes unitários

Pytest, 100% offline — sem rede real, sem credencial, sem nenhuma conta AWS
real. Cobre `decision.py` (Etapa 2a, 100% sem boto3) e `tag_execution.py`
(Etapa 2b — usa boto3, mas os testes passam sessões/clients falsos em vez
de rede real; ver `fake_session_factory` em `conftest.py`). Ver
[test/localstack/README.md](../localstack/README.md) para o teste e2e da
Etapa 1, que precisa de boto3 + LocalStack de verdade.

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

Para `tag_execution.py` (Etapa 2b):

- `test_tag_execution_selecao.py` — garantia estrutural de `select_taggable`
  (só `decisao == "taguear"` passa) e roteamento de API por
  `(servico, tipo_recurso)`.
- `test_tag_execution_lotes.py` — agrupamento em lotes de até 20 ARNs no
  caminho genérico, sem misturar região, e confirmação de que os caminhos
  dedicados (EKS/Bedrock/ELB) nunca são agrupados.
- `test_tag_execution_execucao.py` — revalidação (pula recurso já
  tagueado), idempotência entre reexecuções, dry-run nunca chamando boto3
  de escrita, e formato do relatório de saída.

Ao adicionar um módulo novo (Etapa 2c em diante), crie um novo grupo de
arquivos `test_<módulo>_<área>.py` seguindo o mesmo padrão, em vez de
acrescentar num arquivo já existente de outro módulo.

Fixtures compartilhadas ficam em [`conftest.py`](conftest.py) —
`recurso_factory`/`relatorio_etapa1_factory` para dicts no formato da Etapa
1, `decisao_factory`/`relatorio_decisao_factory` para o formato da Etapa 2a
(entrada da Etapa 2b), e `fake_session_factory` para uma sessão boto3 falsa
(sem rede real) nos testes que exercitam a revalidação de `tag_execution.py`.
Reaproveite em vez de recriar esses dicts/stubs à mão em um novo arquivo.

Toda função de teste tem uma docstring curta explicando o *porquê* do
cenário (não só repetindo o nome) — mantenha esse padrão nos testes novos;
é o que torna a suíte legível como documentação das regras de negócio, não
só como verificação.
