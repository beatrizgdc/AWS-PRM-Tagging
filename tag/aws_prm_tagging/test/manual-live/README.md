# Smoke test manual da Etapa 2c (`apply --live`) contra 1 recurso

> **Atenção: isto roda contra uma conta AWS REAL, não LocalStack** — mesmo
> escopado a um único recurso, o comando `--live` faz uma chamada de
> ESCRITA de verdade (`tag:TagResources`/`eks:TagResource`/
> `bedrock:TagResource`/`elasticloadbalancing:AddTags`, conforme o
> recurso). Confirme o `--profile` antes de rodar `--live` — nunca rode
> sem `--profile` explícito (a cadeia padrão de credenciais pode resolver
> pra uma conta diferente da pretendida).

## Por que este teste existe

Antes de considerar a Etapa 3 (automação contínua, sem revisão humana por
execução), a Etapa 2c precisa ter sido exercitada contra uma conta real
pelo menos uma vez — as Etapas 1/2a/2b (mapeamento, decisão, dry-run) só
validam leitura; só a escrita de verdade confirma que
`tag:TagResources`/etc. e a permissão nativa do serviço dono do recurso
(ver [docs/producao.md](../../docs/producao.md#permissões-iam-para-a-etapa-2c-apply---live-execução-real))
realmente funcionam como documentado.

Escopar a um único recurso de cada vez (em vez de rodar `--live` contra a
conta inteira) reduz o raio de impacto de qualquer coisa inesperada a
praticamente zero, sem abrir mão de testar o caminho de escrita real.

## Pré-requisitos

- Perfil AWS configurado (`--profile`) apontando para a conta de teste —
  ver [README.md#configurar-credenciais-aws](../../README.md#configurar-credenciais-aws).
- Um relatório de decisão (`decide`) já gerado contra essa conta — ver
  [README.md#uso](../../README.md#uso) pelos comandos `map`/`decide`.
  Rodar a partir de `tag/` (pai de `aws_prm_tagging/`), não desta pasta —
  ver [README.md#onde-rodar-os-comandos](../../README.md#onde-rodar-os-comandos).
- Um recurso descartável já existente nesse relatório com `"decisao":
  "taguear"` — idealmente algo criado especificamente para este teste
  (bucket S3, fila SQS, log group), não um recurso reaproveitado cuja
  finalidade real você não tem certeza. **Nunca escolha um recurso
  compartilhado/de cliente.**

## Passo a passo

**1. Isolar 1 recurso no relatório de decisão** — filtra
`decisao_report.json` (saída de `decide`) para conter só o ARN escolhido,
sem tocar em nada na AWS ainda:

```bash
cd tag  # pai de aws_prm_tagging/
python3 -c "
import json
ARN = 'COLE_O_ARN_DO_RECURSO_DE_TESTE_AQUI'
INPUT = 'decisao_report.json'  # ajuste para o nome do seu relatório de decisão
d = json.load(open(INPUT))
recurso = next((r for r in d['recursos'] if r['arn'] == ARN), None)
assert recurso is not None, f'ARN nao encontrado no relatorio: {ARN}'
assert recurso['decisao'] == 'taguear', f'decisao nao e taguear: {recurso[\"decisao\"]}'
d['recursos'] = [recurso]
json.dump(d, open('decisao_um_recurso_teste.json', 'w'), indent=2, ensure_ascii=False)
print('OK:', recurso['servico'], recurso['arn'])
"
```

`decisao_um_recurso_teste.json` cai no `.gitignore` automaticamente (padrão
`*_teste.json`) — nunca deve ser commitado, tem ARN e conta real.

**2. Dry-run só nesse recurso** (confirma que a revalidação passa antes de
escrever de verdade):

```bash
python3 -m aws_prm_tagging.main apply --input decisao_um_recurso_teste.json --profile <perfil> --output resultado_um_recurso_dry.json
```

Confira `resultado_um_recurso_dry.json`: `resumo.por_categoria_final` deve
mostrar `simulado_sucesso: 1` e tudo mais zerado. Se aparecer `falhou`,
`conflito`, `pulado_iac` ou `revisar_tag_similar`, pare aqui — investigue
antes de ir pro `--live` (nenhum desses geraria uma tentativa de escrita de
qualquer forma, mas indica algo inesperado sobre o estado do recurso).

**3. `--live` de verdade** (único passo desta sequência que escreve na
conta):

```bash
python3 -m aws_prm_tagging.main apply --input decisao_um_recurso_teste.json --profile <perfil> --live --output resultado_um_recurso_live.json
```

Confira `resumo.por_categoria_final`: espera-se `tagueado_sucesso: 1`.

**4. Confirmar na AWS que a tag chegou de verdade** (não só no relatório) —
ajuste o comando ao tipo de recurso testado, ex. para um CloudWatch Log
Group:

```bash
aws logs list-tags-log-group --log-group-name <nome> --profile <perfil>
```

**5. Reverter** (se o recurso for descartável só para este teste, mas você
quer deixá-lo limpo depois):

```bash
aws logs untag-log-group --log-group-name <nome> --tags aws-apn-id --profile <perfil>
```

(comando de tag/untag varia por serviço — `sqs:untag-queue`,
`s3api:delete-bucket-tagging`, `ec2:delete-tags` etc., conforme o recurso
escolhido no passo 1.)

## O que este teste NÃO cobre

- Os ~80 serviços do CSV têm, cada um, uma permissão IAM nativa própria
  além de `tag:TagResources` (ver
  [docs/producao.md](../../docs/producao.md#permissões-iam-para-a-etapa-2c-apply---live-execução-real))
  — testar 1 recurso de 1 serviço só confirma aquele serviço específico,
  não a lista inteira.
- Durabilidade da tag em recursos de EKS (nodes/load balancers/volumes) —
  ver [docs/melhorias-futuras.md](../../docs/melhorias-futuras.md).
- Qualquer coisa sobre a Etapa 3 (automação contínua) — este teste só
  valida a Etapa 2c isolada, rodada manualmente.
