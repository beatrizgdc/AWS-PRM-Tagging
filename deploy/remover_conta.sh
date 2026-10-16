#!/usr/bin/env bash
# Remove a automação AWS PRM de uma conta: stacks prm-regional (todas as
# regiões onde existirem) e prm-hub. NUNCA remove tags já aplicadas nos
# recursos. MVP2: os log groups das Lambdas fazem parte das stacks e somem
# com elas. --apagar-logs ainda limpa os órfãos de implantações do MVP1
# (/aws/lambda/prm-hub-* e /aws/lambda/prm-regional-*), listando antes.
set -uo pipefail

uso() {
  cat <<'EOF'
uso: deploy/remover_conta.sh --profile P [--hub us-east-1] [--apagar-logs] [--sim]

  --profile P      perfil AWS da conta
  --hub REGIAO     região da stack hub (default: us-east-1)
  --apagar-logs    também apaga os log groups /aws/lambda/prm-hub-* e /aws/lambda/prm-regional-*
                   (o sam delete não remove; só os prefixos exatos das nossas stacks)
  --sim            não pede confirmação
EOF
  exit 1
}

PROFILE=""; HUB="us-east-1"; LOGS=0; SIM=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2;;
    --hub) HUB="$2"; shift 2;;
    --apagar-logs) LOGS=1; shift;;
    --sim) SIM=1; shift;;
    *) uso;;
  esac
done
[[ -n "$PROFILE" ]] || uso
for cmd in aws sam; do command -v "$cmd" >/dev/null || { echo "falta '$cmd' no PATH"; exit 1; }; done

CONTA=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text) || exit 1
echo "== Procurando stacks prm-regional nas regiões ativas da conta $CONTA (somente leitura)"
ATIVAS=$(aws ec2 describe-regions --profile "$PROFILE" --region "$HUB" --query 'Regions[].RegionName' --output text)
REGIONAIS=()
for R in $ATIVAS; do
  if aws cloudformation describe-stacks --stack-name prm-regional --region "$R" --profile "$PROFILE" >/dev/null 2>&1; then
    REGIONAIS+=("$R")
  fi
done
TEM_HUB=0
aws cloudformation describe-stacks --stack-name prm-hub --region "$HUB" --profile "$PROFILE" >/dev/null 2>&1 && TEM_HUB=1

echo "  prm-regional em: ${REGIONAIS[*]:-nenhuma região}"
echo "  prm-hub em $HUB: $([[ $TEM_HUB -eq 1 ]] && echo sim || echo não)"
[[ ${#REGIONAIS[@]} -eq 0 && $TEM_HUB -eq 0 ]] && { echo "nada a remover"; exit 0; }

if [[ $SIM -eq 0 ]]; then
  read -r -p "Remover as stacks acima? As tags já aplicadas permanecem. (digite 'sim'): " RESP
  [[ "$RESP" == "sim" ]] || { echo "cancelado"; exit 1; }
fi

for R in "${REGIONAIS[@]}"; do
  echo "-- removendo prm-regional em $R"
  sam delete --stack-name prm-regional --region "$R" --profile "$PROFILE" --no-prompts || echo "!! falha em $R"
done
if [[ $TEM_HUB -eq 1 ]]; then
  echo "-- removendo prm-hub em $HUB"
  sam delete --stack-name prm-hub --region "$HUB" --profile "$PROFILE" --no-prompts || echo "!! falha no hub"
fi

echo "== Log groups das Lambdas (prefixos exatos das nossas stacks)"
REGIOES_LOGS=$(printf '%s\n' "${REGIONAIS[@]}" "$HUB" | sort -u)
for R in $REGIOES_LOGS; do
  for PFX in /aws/lambda/prm-hub- /aws/lambda/prm-regional-; do
    for G in $(aws logs describe-log-groups --log-group-name-prefix "$PFX" --region "$R" --profile "$PROFILE" \
                 --query 'logGroups[].logGroupName' --output text 2>/dev/null); do
      [[ "$G" == "None" ]] && continue
      if [[ $LOGS -eq 1 ]]; then
        aws logs delete-log-group --log-group-name "$G" --region "$R" --profile "$PROFILE" && echo "  apagado $R $G"
      else
        echo "  existe $R $G (use --apagar-logs para remover)"
      fi
    done
  done
done
echo
echo "Fica na conta, de propósito: as tags aws-apn-id já aplicadas e a stack aws-sam-cli-managed-default"
echo "(bucket de artefatos do SAM, reaproveitado por outros deploys; artefatos sob o prefixo prm-tagging/)."
