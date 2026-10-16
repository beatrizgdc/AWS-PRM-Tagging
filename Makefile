# Build das Lambdas para o SAM CLI (Metadata.BuildMethod: makefile nos
# templates). `$(ARTIFACTS_DIR)` vem do `sam build`. Só entra no pacote o que
# as Lambdas importam: os módulos .py e o CSV oficial em data/.
# https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/building-custom-runtimes.html

FUNCOES := build-Etapa3Function build-Etapa4DriftFunction \
	build-ListarRegioesFunction build-ProcessarRegiaoFunction \
	build-TagueamentoInicialCRFunction build-ZabbixFunction

$(FUNCOES):
	mkdir -p "$(ARTIFACTS_DIR)/aws_prm_tagging/data"
	cp aws_prm_tagging/*.py "$(ARTIFACTS_DIR)/aws_prm_tagging/"
	cp aws_prm_tagging/data/*.py aws_prm_tagging/data/*.csv "$(ARTIFACTS_DIR)/aws_prm_tagging/data/"

# Layer com boto3/botocore fixados por hash (aws_prm_tagging/requirements-lambda.txt),
# usada por todas as funções da stack. Só wheels puros para python3.12.
build-PrmSdkLayer:
	python3 -m pip install --quiet --no-compile --disable-pip-version-check \
	  --require-hashes --only-binary=:all: \
	  --platform manylinux2014_aarch64 --platform manylinux2014_x86_64 \
	  --python-version 3.12 --implementation cp \
	  -r aws_prm_tagging/requirements-lambda.txt --target "$(ARTIFACTS_DIR)/python"
	rm -rf "$(ARTIFACTS_DIR)/python/bin"

.PHONY: $(FUNCOES) build-PrmSdkLayer
