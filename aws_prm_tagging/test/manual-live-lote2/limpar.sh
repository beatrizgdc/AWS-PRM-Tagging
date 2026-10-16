#!/usr/bin/env bash
# Remove os recursos listados num manifest.tsv gerado por validar_etapa2c.sh
# (terceira coluna = comando de remoção). Continua mesmo se um comando
# falhar (ex.: recurso já apagado à mão) e mostra o que sobrou.
set -uo pipefail
[[ $# -eq 1 && -f "$1" ]] || { echo "uso: $0 <manifest.tsv>"; exit 1; }
FALHAS=0
while IFS=$'\t' read -r arn _ comando; do
  [[ -z "$arn" || "$arn" == \#* ]] && continue
  echo "removendo $arn"
  if ! eval "$comando" >/dev/null; then echo "  !! falhou — remova manualmente: $comando"; FALHAS=$((FALHAS+1)); fi
done < "$1"
[[ $FALHAS -eq 0 ]] && echo "OK: tudo removido." || echo "$FALHAS recurso(s) não removido(s) — ver acima."
exit $FALHAS
