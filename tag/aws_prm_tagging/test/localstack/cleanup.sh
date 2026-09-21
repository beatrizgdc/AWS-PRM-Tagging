#!/usr/bin/env bash
# Encerra o LocalStack usado pelo teste. O LocalStack community roda sem
# persistencia por padrao, entao parar o container ja remove todos os
# recursos de teste criados por setup_test_resources.sh.
set -euo pipefail

localstack stop

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rm -f "$SCRIPT_DIR/relatorio_teste.json"

echo "LocalStack parado e relatorio de teste removido."
