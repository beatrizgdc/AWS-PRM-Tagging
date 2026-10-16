# Testes automatizados

Pytest, offline: sem rede, sem credencial e sem conta AWS. A AWS é simulada
com sessões falsas (`fake_session_factory` em `conftest.py`) ou com o
[moto](https://github.com/getmoto/moto). Templates são checados com
`cfn-lint` (pulado se não estiver instalado).

Rodar a partir da pasta que contém `aws_prm_tagging/`:

```bash
python3 -m pip install -r aws_prm_tagging/requirements-dev.txt
python3 -m pytest aws_prm_tagging/test/unit/
```

Convenção: um arquivo por área do módulo testado, `test_<módulo>_<área>.py`
(ex.: `test_decision_precedencia.py`, `test_tag_execution_lotes.py`). Os
arquivos `*_moto.py` usam AWS simulada; `test_infra_*` travam os templates
(event patterns gerados, state machines, permissões e lint).

Validação em conta real: `../sandbox/GUIA.md`.
