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
