# Build customizado para o SAM CLI (AWS::Serverless::Function com
# Metadata.BuildMethod: makefile, ver infra/template.yaml) — necessário
# porque `.samignore` NÃO é um mecanismo suportado pelo SAM CLI (confirmado
# lendo o código-fonte instalado: nenhuma referência a "samignore" em
# nenhum lugar do pacote `samcli`) — uma versão anterior deste repositório
# tinha um `.samignore` que nunca teve efeito nenhum, copiando test/docs/
# infra/reference/dados soltos de smoke test para dentro do pacote da
# Lambda silenciosamente. Este Makefile é o mecanismo real e documentado
# pela AWS para controlar exatamente o que entra no artefato de build:
# https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/building-custom-runtimes.html
#
# `$(ARTIFACTS_DIR)` é injetado pelo `sam build` — só copiamos o que a
# Lambda da Etapa 3 realmente importa em runtime (módulos .py do pacote +
# o CSV oficial em data/), nunca test/, docs/, infra/, reference/ nem
# qualquer arquivo solto de smoke test manual que exista neste diretório.
build-Etapa3Function:
	mkdir -p "$(ARTIFACTS_DIR)/aws_prm_tagging/data"
	cp aws_prm_tagging/*.py "$(ARTIFACTS_DIR)/aws_prm_tagging/"
	cp aws_prm_tagging/data/*.py aws_prm_tagging/data/*.csv "$(ARTIFACTS_DIR)/aws_prm_tagging/data/"

# Hub (item 8) — mesmo conteúdo de artefato da Etapa 3.
build-ListarRegioesFunction:
	mkdir -p "$(ARTIFACTS_DIR)/aws_prm_tagging/data"
	cp aws_prm_tagging/*.py "$(ARTIFACTS_DIR)/aws_prm_tagging/"
	cp aws_prm_tagging/data/*.py aws_prm_tagging/data/*.csv "$(ARTIFACTS_DIR)/aws_prm_tagging/data/"

# Hub (item 8) — mesmo conteúdo de artefato da Etapa 3.
build-ProcessarRegiaoFunction:
	mkdir -p "$(ARTIFACTS_DIR)/aws_prm_tagging/data"
	cp aws_prm_tagging/*.py "$(ARTIFACTS_DIR)/aws_prm_tagging/"
	cp aws_prm_tagging/data/*.py aws_prm_tagging/data/*.csv "$(ARTIFACTS_DIR)/aws_prm_tagging/data/"

# Hub (item 8) — mesmo conteúdo de artefato da Etapa 3.
build-TagueamentoInicialCRFunction:
	mkdir -p "$(ARTIFACTS_DIR)/aws_prm_tagging/data"
	cp aws_prm_tagging/*.py "$(ARTIFACTS_DIR)/aws_prm_tagging/"
	cp aws_prm_tagging/data/*.py aws_prm_tagging/data/*.csv "$(ARTIFACTS_DIR)/aws_prm_tagging/data/"

# Etapa 4 (drift, stack regional) — mesmo conteúdo de artefato.
build-Etapa4DriftFunction:
	mkdir -p "$(ARTIFACTS_DIR)/aws_prm_tagging/data"
	cp aws_prm_tagging/*.py "$(ARTIFACTS_DIR)/aws_prm_tagging/"
	cp aws_prm_tagging/data/*.py aws_prm_tagging/data/*.csv "$(ARTIFACTS_DIR)/aws_prm_tagging/data/"

# MVP2 (item 3) — Lambda Layer com boto3/botocore atuais e fixados por hash
# (aws_prm_tagging/requirements-lambda.txt). Uma layer por stack, usada por
# todas as funções da stack (Globals.Function.Layers nos templates). Só
# wheels puros (sem compilação), para a plataforma do runtime python3.12.
build-PrmSdkLayer:
	python3 -m pip install --quiet --no-compile --disable-pip-version-check \
	  --require-hashes --only-binary=:all: \
	  --platform manylinux2014_aarch64 --platform manylinux2014_x86_64 \
	  --python-version 3.12 --implementation cp \
	  -r aws_prm_tagging/requirements-lambda.txt --target "$(ARTIFACTS_DIR)/python"
	rm -rf "$(ARTIFACTS_DIR)/python/bin"

# MVP2 (item 1) — encaminhador para o Zabbix (template-zabbix.yaml).
build-ZabbixFunction:
	mkdir -p "$(ARTIFACTS_DIR)/aws_prm_tagging/data"
	cp aws_prm_tagging/*.py "$(ARTIFACTS_DIR)/aws_prm_tagging/"
	cp aws_prm_tagging/data/*.py aws_prm_tagging/data/*.csv "$(ARTIFACTS_DIR)/aws_prm_tagging/data/"
