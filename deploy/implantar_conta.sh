#!/usr/bin/env bash
# Implanta a automação AWS PRM numa conta (stack hub + stack regional em cada
# região). Rodar a partir da pasta que contém o Makefile e aws_prm_tagging/.
#
# Segurança por padrão: tagueamento inicial e varredura recorrente em
# DRY-RUN (nada é escrito; só relatório por e-mail). A Etapa 3 (reação em
# tempo real a recursos NOVOS) escreve a tag desde o deploy — é o propósito
# dela. Ver docs/MVP-1.md, seção "Como implantar".
set -uo pipefail

uso() {
  cat <<'EOF'
uso: deploy/implantar_conta.sh --profile P --valor pc:XXXX --email E [opções]

obrigatórios:
  --profile P               perfil AWS da conta do cliente
  --valor pc:XXXX           valor da tag aws-apn-id (product code do contrato)
  --email E                 e-mail que recebe as notificações (confirmar a assinatura)

opções:
  --hub REGIAO              região da stack hub (default: us-east-1)
  --regioes a,b,c           regiões da stack regional (default: lista do preflight —
                            regiões ativas + us-east-1)
  --modo-inicial M          dry-run (default) | live | desligado
  --modo-varredura M        dry-run (default) | live | desligado
  --reexecutar N            muda para rodar o tagueamento inicial de novo num update (default: 1)
  --contrato-confirmado     marca o contrato como confirmado para esta conta (default: não)
  --regioes-varredura a,b   restringe tagueamento inicial/varredura a estas regiões (default: todas)
  --sim                     não pede confirmação
  --somente-mostrar         mostra o plano e os comandos, não executa nada
EOF
  exit 1
}

PROFILE=""; VALOR=""; EMAIL=""; HUB="us-east-1"; REGIOES=""; MODO_INI="dry-run"; MODO_VAR="dry-run"
REEXEC="1"; CONTRATO="false"; REG_VAR=""; SIM=0; MOSTRAR=0; PREFIXO="prm-tagging"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2;;
    --valor) VALOR="$2"; shift 2;;
    --email) EMAIL="$2"; shift 2;;
    --hub) HUB="$2"; shift 2;;
    --regioes) REGIOES="$2"; shift 2;;
    --modo-inicial) MODO_INI="$2"; shift 2;;
    --modo-varredura) MODO_VAR="$2"; shift 2;;
    --reexecutar) REEXEC="$2"; shift 2;;
    --contrato-confirmado) CONTRATO="true"; shift;;
    --regioes-varredura) REG_VAR="$2"; shift 2;;
    --sim) SIM=1; shift;;
    --somente-mostrar) MOSTRAR=1; shift;;
    *) echo "opção desconhecida: $1"; uso;;
  esac
done
[[ -n "$PROFILE" && -n "$VALOR" && -n "$EMAIL" ]] || uso
[[ "$VALOR" =~ ^pc:[A-Za-z0-9]+$ ]] || { echo "--valor precisa ser pc:<código alfanumérico>"; exit 1; }
for m in "$MODO_INI" "$MODO_VAR"; do
  [[ "$m" =~ ^(dry-run|live|desligado)$ ]] || { echo "modo inválido: $m"; exit 1; }
done
[[ -f Makefile && -d aws_prm_tagging ]] || { echo "rode a partir da pasta que contém Makefile e aws_prm_tagging/"; exit 1; }
for cmd in aws sam make python3; do
  command -v "$cmd" >/dev/null || { echo "falta '$cmd' no PATH (ver docs/MVP-1.md, pré-requisitos)"; exit 1; }
done

CONTA=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text) || { echo "credencial inválida para o perfil $PROFILE"; exit 1; }

# --- 1. preflight (somente leitura): regiões e trail
if [[ -z "$REGIOES" ]]; then
  echo "== Preflight (somente leitura)"
  python3 -m aws_prm_tagging.main preflight --profile "$PROFILE" --output prm_preflight.json
  ST=$?
  [[ $ST -eq 1 ]] && { echo "preflight falhou — ver mensagens acima"; exit 1; }
  REGIOES=$(python3 -c "import json;print(','.join(json.load(open('prm_preflight.json'))['regioes_stackset']))")
  if [[ $ST -eq 2 ]]; then
    echo "!! ATENÇÃO: há região sem trail do CloudTrail registrando escrita (ver prm_preflight.json)."
    echo "!! Nelas, a Etapa 3 só reage a instâncias EC2 e tasks ECS (eventos nativos)."
  fi
fi
[[ ",$REGIOES," == *",us-east-1,"* ]] || echo "!! ATENÇÃO: us-east-1 fora da lista — eventos de serviços globais (Route 53, CloudFront) não serão vistos."

# --- 2. plano
echo
echo "== Plano de implantação"
echo "  conta ............... $CONTA (perfil $PROFILE)"
echo "  valor da tag ........ aws-apn-id = $VALOR"
echo "  e-mail .............. $EMAIL"
echo "  stack hub ........... prm-hub em $HUB"
echo "  stack regional ...... prm-regional em: $REGIOES"
echo "  tagueamento inicial . $MODO_INI (reexecutar=$REEXEC)"
echo "  varredura recorrente  $MODO_VAR"
echo "  contrato confirmado . $CONTRATO"
[[ -n "$REG_VAR" ]] && echo "  regiões da varredura  $REG_VAR"
echo "  Etapa 3 ............. ESCREVE a tag em recursos NOVOS desde o deploy, em todas as regiões acima"
[[ "$MODO_INI" == "live" || "$MODO_VAR" == "live" ]] && echo "  !! modo LIVE: a tag será escrita também nos recursos EXISTENTES (exige consentimento do cliente)"
echo

HUB_PARAMS=(NotificationEmail="$EMAIL" ExpectedTagValue="$VALOR" ModoTagueamentoInicial="$MODO_INI"
            ReexecutarTagueamentoInicial="$REEXEC" ModoVarreduraRecorrente="$MODO_VAR" ContratoConfirmado="$CONTRATO")
[[ -n "$REG_VAR" ]] && HUB_PARAMS+=(RegioesVarredura="$REG_VAR")
REG_PARAMS=(ExpectedTagValue="$VALOR" HubRegion="$HUB")

if [[ $MOSTRAR -eq 1 ]]; then
  echo "== Comandos (--somente-mostrar: nada foi executado)"
  echo "sam build --template-file aws_prm_tagging/infra/template-hub.yaml"
  echo "sam deploy --stack-name prm-hub --region $HUB --profile $PROFILE --resolve-s3 --s3-prefix $PREFIXO --capabilities CAPABILITY_IAM --parameter-overrides ${HUB_PARAMS[*]}"
  echo "sam build --template-file aws_prm_tagging/infra/template-regional.yaml"
  for R in ${REGIOES//,/ }; do
    echo "sam deploy --stack-name prm-regional --region $R --profile $PROFILE --resolve-s3 --s3-prefix $PREFIXO --capabilities CAPABILITY_IAM --parameter-overrides ${REG_PARAMS[*]}"
  done
  exit 0
fi

if [[ $SIM -eq 0 ]]; then
  read -r -p "Confirmar a implantação acima? (digite 'sim'): " RESP
  [[ "$RESP" == "sim" ]] || { echo "cancelado — nada foi implantado"; exit 1; }
fi

# --- 3. hub
echo "== Stack hub ($HUB)"
sam build --template-file aws_prm_tagging/infra/template-hub.yaml || { echo "falha no build do hub"; exit 1; }
sam deploy --stack-name prm-hub --region "$HUB" --profile "$PROFILE" --resolve-s3 --s3-prefix "$PREFIXO" \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset --parameter-overrides "${HUB_PARAMS[@]}" \
  || { echo "falha no deploy do hub — nenhuma stack regional foi implantada"; exit 1; }

# --- 4. regionais (uma região que falhe não impede as outras)
echo "== Stacks regionais"
sam build --template-file aws_prm_tagging/infra/template-regional.yaml || { echo "falha no build da regional"; exit 1; }
OK=(); FALHA=()
for R in ${REGIOES//,/ }; do
  echo "-- $R"
  if sam deploy --stack-name prm-regional --region "$R" --profile "$PROFILE" --resolve-s3 --s3-prefix "$PREFIXO" \
       --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset --parameter-overrides "${REG_PARAMS[@]}"; then
    OK+=("$R")
  else
    FALHA+=("$R")
  fi
done

echo
echo "== Resumo"
echo "  hub: prm-hub em $HUB"
echo "  regionais OK (${#OK[@]}): ${OK[*]:-nenhuma}"
if [[ ${#FALHA[@]} -gt 0 ]]; then
  echo "  regionais com FALHA (${#FALHA[@]}): ${FALHA[*]}"
  echo "  (região bloqueada por SCP é causa comum — confira o erro acima; as demais seguem funcionando)"
fi
echo
echo "Próximos passos:"
echo "  1. Confirme a assinatura no e-mail que a AWS enviou para $EMAIL."
echo "  2. Leia os resumos do tagueamento inicial ($MODO_INI), um por região."
echo "  3. Para aplicar a tag nos recursos existentes (com consentimento do cliente):"
echo "     deploy/implantar_conta.sh --profile $PROFILE --valor $VALOR --email $EMAIL --hub $HUB \\"
echo "       --regioes $REGIOES --modo-inicial live --reexecutar $((REEXEC + 1))"
[[ ${#FALHA[@]} -eq 0 ]]
