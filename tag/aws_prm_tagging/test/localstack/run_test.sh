#!/usr/bin/env bash
# Orquestra o teste de ponta a ponta descrito em test/localstack/README.md:
# sobe o LocalStack, cria os recursos de teste, roda o modulo principal
# (aws_prm_tagging.main) contra ele e imprime um resumo do relatorio gerado.
#
# Nao toca em nenhuma conta AWS real: tudo roda contra o endpoint local do
# LocalStack (http://localhost.localstack.cloud:4566).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Diretorio que contem o pacote aws_prm_tagging/ (import root) — este script
# vive em aws_prm_tagging/test/localstack/, entao sao 3 niveis acima.
IMPORT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
OUTPUT_FILE="${1:-$SCRIPT_DIR/relatorio_teste.json}"

command -v localstack >/dev/null 2>&1 || {
  echo "Erro: CLI 'localstack' nao encontrada. Instale com 'python3 -m pip install localstack' antes de rodar este teste." >&2
  exit 1
}
command -v docker >/dev/null 2>&1 || {
  echo "Erro: docker nao encontrado. O LocalStack roda como container Docker." >&2
  exit 1
}

if ! grep -q '^\[profile localstack\]' ~/.aws/config 2>/dev/null; then
  echo "== Perfil 'localstack' nao encontrado em ~/.aws/config — criando =="
  mkdir -p ~/.aws
  cat >> ~/.aws/config <<'EOF'

[profile localstack]
region = us-east-1
output = json
endpoint_url = http://localhost.localstack.cloud:4566
EOF
  cat >> ~/.aws/credentials <<'EOF'

[localstack]
aws_access_key_id = test
aws_secret_access_key = test
EOF
fi

echo "== Subindo LocalStack =="
localstack start -d

echo "== Aguardando LocalStack ficar saudavel =="
for _ in $(seq 1 30); do
  code=$(curl -s -m 2 -o /dev/null -w "%{http_code}" http://localhost:4566/_localstack/health || true)
  if [ "$code" = "200" ]; then
    echo "LocalStack pronto."
    break
  fi
  sleep 2
done

echo "== Criando recursos de teste =="
bash "$SCRIPT_DIR/setup_test_resources.sh"

echo
echo "== Rodando aws_prm_tagging.main contra o LocalStack =="
cd "$IMPORT_ROOT"
python3 -m aws_prm_tagging.main \
  --expected-tag-value pc:test123 \
  --profile localstack \
  --output "$OUTPUT_FILE"

echo
echo "== Resumo do relatorio gerado ($OUTPUT_FILE) =="
python3 -c "
import json
with open('$OUTPUT_FILE') as f:
    report = json.load(f)
print(json.dumps(report['resumo'], indent=2, ensure_ascii=False))
print()
print('arvore_ou presente:', report['arvore_ou'] is not None)
"

echo
echo "Relatorio completo em: $OUTPUT_FILE"
echo "Para conferir os criterios de aceite, ver a secao 'Resultado esperado' em test/localstack/README.md"
echo "Para encerrar o LocalStack: bash $SCRIPT_DIR/cleanup.sh"
