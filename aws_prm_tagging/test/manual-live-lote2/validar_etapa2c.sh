#!/usr/bin/env bash
# Validação da Etapa 1 (descoberta nativa) + Etapa 2c (apply --live) numa
# conta SANDBOX compartilhada — item 4 do plano. Ver README.md desta pasta.
#
# MVP2 (regra nova da AWS): o bucket "outro-parceiro" agora é SOBRESCRITO
# (valor anterior registrado), a fila com chave parecida recebe aws-apn-id
# ao lado da chave antiga, e os buckets de Terraform e Pulumi são pulados e
# aparecem em resultado_live_cliente.md (lista para o cliente).
#
# O QUE CRIA (tudo gratuito enquanto vazio; prefixo único por execução):
#   5 buckets S3, 2 filas SQS, 1 tópico SNS, 1 tabela DynamoDB on-demand,
#   1 log group, 1 repositório ECR — e, só com --lambda-role-arn, 1 função Lambda.
# Cada recurso é registrado em <saida>/manifest.tsv com o comando de remoção.
# NÃO apaga nada: rode ./limpar.sh <saida>/manifest.tsv quando terminar.
#
# Segurança em conta compartilhada: a escrita (apply --live) usa
# --somente-arns com o manifest — NENHUM recurso de outra pessoa é tocado.
# A varredura (map) lê a região inteira, mas é somente leitura.
set -euo pipefail

uso() { echo "uso: $0 --profile P --valor pc:xxxx [--regiao us-east-1] [--lambda-role-arn ARN] [--sem-s3]"; exit 1; }
PROFILE=""; VALOR=""; REGIAO="us-east-1"; LAMBDA_ROLE=""; SEM_S3=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2;;
    --valor) VALOR="$2"; shift 2;;
    --regiao) REGIAO="$2"; shift 2;;
    --lambda-role-arn) LAMBDA_ROLE="$2"; shift 2;;
    *) uso;;
  esac
done
[[ -n "$PROFILE" && -n "$VALOR" ]] || uso

AWS=(aws --profile "$PROFILE" --region "$REGIAO" --output text)
RAIZ="$(cd "$(dirname "$0")/../../.." && pwd)"   # diretório pai de aws_prm_tagging/
PREFIXO="prm-val-$(date +%s)"
SAIDA="$(pwd)/saida-${PREFIXO}"
mkdir -p "$SAIDA"
MANIFEST="$SAIDA/manifest.tsv"
CONTA="$("${AWS[@]}" sts get-caller-identity --query Account)"
echo "# arn	categoria_esperada	comando_de_remocao" > "$MANIFEST"

trap 'echo; echo "!! Erro no meio da execução. Os recursos criados até aqui estão em $MANIFEST"; echo "!! Remova com: $(dirname "$0")/limpar.sh $MANIFEST"' ERR

registra() { printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$MANIFEST"; echo "  criado: $1  (esperado: $2)"; }
remocao_prefixo="aws --profile $PROFILE --region $REGIAO"

cria_bucket() {  # nome
  if [[ "$REGIAO" == "us-east-1" ]]; then "${AWS[@]}" s3api create-bucket --bucket "$1" >/dev/null
  else "${AWS[@]}" s3api create-bucket --bucket "$1" --create-bucket-configuration "LocationConstraint=$REGIAO" >/dev/null; fi
}

echo "== Conta $CONTA, região $REGIAO, prefixo $PREFIXO"
echo "== 1/5 Criando recursos de teste"

if [[ $SEM_S3 -eq 0 ]]; then
B1="${PREFIXO}-sem-tag"; cria_bucket "$B1"
registra "arn:aws:s3:::$B1" tagueado_sucesso "$remocao_prefixo s3api delete-bucket --bucket $B1"

B2="${PREFIXO}-outro-parceiro"; cria_bucket "$B2"
"${AWS[@]}" s3api put-bucket-tagging --bucket "$B2" --tagging 'TagSet=[{Key=aws-apn-id,Value=pc:outroparceiro}]'
registra "arn:aws:s3:::$B2" sobrescrito "$remocao_prefixo s3api delete-bucket --bucket $B2"

B3="${PREFIXO}-ja-ok"; cria_bucket "$B3"
"${AWS[@]}" s3api put-bucket-tagging --bucket "$B3" --tagging "TagSet=[{Key=aws-apn-id,Value=$VALOR}]"
registra "arn:aws:s3:::$B3" ja_ok "$remocao_prefixo s3api delete-bucket --bucket $B3"

B4="${PREFIXO}-terraform"; cria_bucket "$B4"
"${AWS[@]}" s3api put-bucket-tagging --bucket "$B4" --tagging 'TagSet=[{Key=managed-by,Value=terraform}]'
registra "arn:aws:s3:::$B4" pulado_iac "$remocao_prefixo s3api delete-bucket --bucket $B4"

B5="${PREFIXO}-pulumi"; cria_bucket "$B5"
"${AWS[@]}" s3api put-bucket-tagging --bucket "$B5" --tagging 'TagSet=[{Key=managed-by,Value=pulumi},{Key=aws-apn-id,Value=pc:outroparceiro}]'
registra "arn:aws:s3:::$B5" pulado_iac "$remocao_prefixo s3api delete-bucket --bucket $B5"
else
  cria_fila() {  # sufixo categoria [tags]
    local nome="${PREFIXO}-q-$1" url
    if [[ -n "${3:-}" ]]; then url="$("${AWS[@]}" sqs create-queue --queue-name "$nome" --tags "$3" --query QueueUrl)"
    else url="$("${AWS[@]}" sqs create-queue --queue-name "$nome" --query QueueUrl)"; fi
    registra "arn:aws:sqs:$REGIAO:$CONTA:$nome" "$2" "$remocao_prefixo sqs delete-queue --queue-url $url"
  }
  cria_fila outro-parceiro sobrescrito "aws-apn-id=pc:outroparceiro"
  cria_fila ja-ok ja_ok "aws-apn-id=$VALOR"
  cria_fila terraform pulado_iac "managed-by=terraform"
  cria_fila pulumi pulado_iac "managed-by=pulumi,aws-apn-id=pc:outroparceiro"
fi

Q1="${PREFIXO}-sem-tag"; URL1="$("${AWS[@]}" sqs create-queue --queue-name "$Q1" --query QueueUrl)"
registra "arn:aws:sqs:$REGIAO:$CONTA:$Q1" tagueado_sucesso "$remocao_prefixo sqs delete-queue --queue-url $URL1"

Q2="${PREFIXO}-similar"; URL2="$("${AWS[@]}" sqs create-queue --queue-name "$Q2" --tags AWS-APN-ID=digitado-errado --query QueueUrl)"
registra "arn:aws:sqs:$REGIAO:$CONTA:$Q2" adicionado_com_similar "$remocao_prefixo sqs delete-queue --queue-url $URL2"

T1="$("${AWS[@]}" sns create-topic --name "${PREFIXO}-sem-tag" --query TopicArn)"
registra "$T1" tagueado_sucesso "$remocao_prefixo sns delete-topic --topic-arn $T1"

D1="${PREFIXO}-sem-tag"
"${AWS[@]}" dynamodb create-table --table-name "$D1" --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=id,AttributeType=S --key-schema AttributeName=id,KeyType=HASH >/dev/null
registra "arn:aws:dynamodb:$REGIAO:$CONTA:table/$D1" tagueado_sucesso "$remocao_prefixo dynamodb delete-table --table-name $D1"

L1="/${PREFIXO}/sem-tag"; "${AWS[@]}" logs create-log-group --log-group-name "$L1"
registra "arn:aws:logs:$REGIAO:$CONTA:log-group:$L1" tagueado_sucesso "$remocao_prefixo logs delete-log-group --log-group-name $L1"

E1="${PREFIXO}-sem-tag"; R1="$("${AWS[@]}" ecr create-repository --repository-name "$E1" --query repository.repositoryArn)"
registra "$R1" tagueado_sucesso "$remocao_prefixo ecr delete-repository --repository-name $E1 --force"

if [[ -n "$LAMBDA_ROLE" ]]; then
  ZIP="$SAIDA/fn.zip"; python3 - "$ZIP" <<'PY'
import sys, zipfile; zipfile.ZipFile(sys.argv[1], "w").writestr("index.py", "def handler(e, c):\n    return 1\n")
PY
  F1="${PREFIXO}-sem-tag"
  FA="$("${AWS[@]}" lambda create-function --function-name "$F1" --runtime python3.12 --handler index.handler \
        --role "$LAMBDA_ROLE" --zip-file "fileb://$ZIP" --query FunctionArn)"
  registra "$FA" tagueado_sucesso "$remocao_prefixo lambda delete-function --function-name $F1"
fi

echo "== 2/5 Etapa 1 (map) — somente leitura, região $REGIAO"
cd "$RAIZ"
python3 -m aws_prm_tagging.main map --profile "$PROFILE" --regioes "$REGIAO" \
  --expected-tag-value "$VALOR" --output "$SAIDA/mapa.json"

echo "== 3/5 Etapa 2a (decide) — sem chamada de API"
python3 -m aws_prm_tagging.main decide --input "$SAIDA/mapa.json" \
  --expected-tag-value "$VALOR" --output "$SAIDA/decisao.json"

echo "== 4/5 Etapa 2b (dry-run) e 2c (--live), SÓ nos recursos do manifest"
python3 -m aws_prm_tagging.main apply --profile "$PROFILE" --input "$SAIDA/decisao.json" \
  --somente-arns "$MANIFEST" --output "$SAIDA/resultado_dry_run.json"
python3 -m aws_prm_tagging.main apply --profile "$PROFILE" --input "$SAIDA/decisao.json" \
  --somente-arns "$MANIFEST" --live --output "$SAIDA/resultado_live.json"

echo "== 5/5 Verificação (lê as tags de volta na AWS)"
trap - ERR
python3 "$RAIZ/aws_prm_tagging/test/manual-live-lote2/verificar_resultado.py" \
  --profile "$PROFILE" --regiao "$REGIAO" --manifest "$MANIFEST" \
  --mapa "$SAIDA/mapa.json" --resultado "$SAIDA/resultado_live.json" --valor "$VALOR" \
  --cliente "$SAIDA/resultado_live_cliente.json" \
  && STATUS=0 || STATUS=$?

echo
echo "Relatório para o cliente (IaC + estado anterior): $SAIDA/resultado_live_cliente.md"
echo "Saídas em: $SAIDA  (contêm ARNs da conta — não versionar)"
echo "Para remover TUDO que este script criou:  $(dirname "$0")/limpar.sh $MANIFEST"
exit $STATUS
