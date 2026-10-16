"""Descoberta nativa contra AWS SIMULADA (moto) — offline, sem
credencial, sem custo.

Três garantias:

1. **Consistência de ARN** — para cada serviço que o moto suporta na
   Tagging API, o ARN que a descoberta nativa monta é o MESMO que o
   `GetResources` devolve (senão o mesmo recurso apareceria duas vezes no
   relatório). Foi este teste que achou a divergência do snapshot EBS.
2. **Lacuna fechada** — recurso criado sem NENHUMA tag não aparece no
   `GetResources`, mas aparece na descoberta nativa, como `sem_tag`.
3. **Ponta a ponta do CLI** — `map` -> `decide` -> `apply --live` rodando o
   código real: os recursos sem tag terminam tagueados.

Limitação: o moto não cobre todos os serviços na Tagging API (ex.: ECR,
API Gateway, Kinesis); para esses a consistência de ARN depende de
validação em conta real.
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")

from aws_prm_tagging import main, native_discovery, regions  # noqa: E402
from aws_prm_tagging.services import load_services  # noqa: E402

R = "us-east-1"
VALOR = "pc:abc123"
TAG = [{"Key": "outra", "Value": "x"}]


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", R)
    monkeypatch.setenv("MOTO_EC2_LOAD_DEFAULT_AMIS", "false")  # sem snapshots artificiais de AMI
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with moto.mock_aws():
        yield boto3.Session(region_name=R)


def _criar_pares(s) -> dict[str, tuple]:
    """Cria, por serviço, um recurso COM uma tag qualquer e outro SEM tag
    nenhuma. Devolve {servico: (id_com_tag, id_sem_tag)} só para referência."""
    c = lambda n: s.client(n, region_name=R)  # noqa: E731
    ec2 = c("ec2")
    ami = ec2.register_image(Name="img", RootDeviceName="/dev/sda1")["ImageId"]
    ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1, TagSpecifications=[{"ResourceType": "instance", "Tags": TAG}])
    ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1)
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=1, TagSpecifications=[{"ResourceType": "volume", "Tags": TAG}])
    vol = ec2.create_volume(AvailabilityZone="us-east-1a", Size=1)["VolumeId"]
    ec2.create_snapshot(VolumeId=vol, TagSpecifications=[{"ResourceType": "snapshot", "Tags": TAG}])
    ec2.create_snapshot(VolumeId=vol)
    c("s3").create_bucket(Bucket="bucket-com-tag")
    c("s3").put_bucket_tagging(Bucket="bucket-com-tag", Tagging={"TagSet": TAG})
    c("s3").create_bucket(Bucket="bucket-sem-tag")
    for nome, extra in (("dbcom", {"Tags": TAG}), ("dbsem", {})):
        c("rds").create_db_instance(
            DBInstanceIdentifier=nome, DBInstanceClass="db.t3.micro", Engine="postgres",
            MasterUsername="u", MasterUserPassword="p1234567", AllocatedStorage=20, **extra,
        )
    for nome, extra in (("tcom", {"Tags": TAG}), ("tsem", {})):
        c("dynamodb").create_table(
            TableName=nome, KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST", **extra,
        )
    role = c("iam").create_role(RoleName="r", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    z = io.BytesIO()
    zipfile.ZipFile(z, "w").writestr("a.py", "x=1")
    for nome, extra in (("fcom", {"Tags": {"outra": "x"}}), ("fsem", {})):
        c("lambda").create_function(
            FunctionName=nome, Runtime="python3.12", Role=role, Handler="a.x", Code={"ZipFile": z.getvalue()}, **extra
        )
    c("sqs").create_queue(QueueName="qcom", tags={"outra": "x"})
    c("sqs").create_queue(QueueName="qsem")
    c("sns").create_topic(Name="tcom", Tags=TAG)
    c("sns").create_topic(Name="tsem")
    c("logs").create_log_group(logGroupName="lcom", tags={"outra": "x"})
    c("logs").create_log_group(logGroupName="lsem")
    c("secretsmanager").create_secret(Name="scom", SecretString="x", Tags=TAG)
    c("secretsmanager").create_secret(Name="ssem", SecretString="x")
    c("kms").create_key(Tags=[{"TagKey": "outra", "TagValue": "x"}])
    c("kms").create_key()
    defn = '{"StartAt":"a","States":{"a":{"Type":"Pass","End":true}}}'
    c("stepfunctions").create_state_machine(name="mcom", definition=defn, roleArn=role, tags=[{"key": "outra", "value": "x"}])
    c("stepfunctions").create_state_machine(name="msem", definition=defn, roleArn=role)
    c("ecs").create_cluster(clusterName="ccom", tags=[{"key": "outra", "value": "x"}])
    c("ecs").create_cluster(clusterName="csem")
    c("efs").create_file_system(CreationToken="a", Tags=TAG)
    c("efs").create_file_system(CreationToken="b")


def _get_resources_arns(s) -> set[str]:
    pag = s.client("resourcegroupstaggingapi", region_name=R).get_paginator("get_resources")
    return {m["ResourceARN"] for p in pag.paginate() for m in p["ResourceTagMappingList"]}


def test_arns_nativos_batem_com_os_da_tagging_api(aws):
    _criar_pares(aws)
    conta = aws.client("sts").get_caller_identity()["Account"]
    genericos = _get_resources_arns(aws)
    nativos, falhas = native_discovery.discover_native_resources(aws, R, conta, load_services(), VALOR)

    chaves_nativas = {native_discovery._chave_comparacao(r["arn"]) for r in nativos}
    nao_reproduzidos = sorted(a for a in genericos if native_discovery._chave_comparacao(a) not in chaves_nativas)
    assert nao_reproduzidos == [], f"formato de ARN divergente entre a listagem nativa e a Tagging API: {nao_reproduzidos}"


def test_recurso_sem_nenhuma_tag_so_aparece_pela_descoberta_nativa(aws):
    _criar_pares(aws)
    conta = aws.client("sts").get_caller_identity()["Account"]
    genericos = [{"arn": a} for a in _get_resources_arns(aws)]
    nativos, _ = native_discovery.discover_native_resources(aws, R, conta, load_services(), VALOR)
    novos = native_discovery.merge_native_resources(genericos, nativos)
    arns = {r["arn"] for r in novos}

    for esperado in (
        "arn:aws:s3:::bucket-sem-tag",
        f"arn:aws:dynamodb:{R}:{conta}:table/tsem",
        f"arn:aws:lambda:{R}:{conta}:function:fsem",
        f"arn:aws:sqs:{R}:{conta}:qsem",
        f"arn:aws:rds:{R}:{conta}:db:dbsem",
    ):
        assert esperado in arns, f"{esperado} (criado sem tag) não foi descoberto"
    for r in novos:
        assert r["status_tag"] == "sem_tag" and r["origem_descoberta"] == "nativa"
    # nenhum recurso COM tag duplicado
    assert not any(a.endswith(("bucket-com-tag", "table/tcom", "function:fcom", ":qcom")) for a in arns)


def test_enumerador_quebrado_nao_derruba_os_demais(aws, monkeypatch):
    _criar_pares(aws)
    conta = aws.client("sts").get_caller_identity()["Account"]
    quebrado = native_discovery._Enumerador(
        "quebrado", "s3", "list_buckets", lambda resp: (_ for _ in ()).throw(RuntimeError("boom")),
        lambda i, r, a: None, ("s3:ListAllMyBuckets",),
    )
    monkeypatch.setattr(native_discovery, "_ENUMERADORES", (quebrado,) + native_discovery._ENUMERADORES)
    nativos, falhas = native_discovery.discover_native_resources(aws, R, conta, load_services(), VALOR)
    assert any(f["etapa"] == "nativo:quebrado" for f in falhas)
    assert any(r["arn"].endswith("bucket-sem-tag") for r in nativos)


def test_iam_actions_de_leitura_sao_so_leitura():
    for acao in native_discovery.required_iam_actions():
        verbo = acao.split(":", 1)[1]
        assert verbo.startswith(("Describe", "List", "Get", "GET")), acao


def test_cli_ponta_a_ponta_map_decide_apply_live(aws, tmp_path, monkeypatch):
    """map -> decide -> apply --live pelo CLI real, contra o moto."""
    _criar_pares(aws)
    monkeypatch.setattr(regions, "get_active_regions", lambda session, base_region="us-east-1": [R])
    mapa, decisao, resultado = tmp_path / "mapa.json", tmp_path / "decisao.json", tmp_path / "resultado.json"

    assert main.run(["map", "--expected-tag-value", VALOR, "--output", str(mapa)]) == 0
    relatorio = json.loads(mapa.read_text())
    assert relatorio["resumo"]["total_descobertos_so_via_api_nativa"] > 0
    arns = [r["arn"] for r in relatorio["recursos"]]
    assert len(arns) == len(set(arns)), "ARN duplicado no relatório da Etapa 1"

    assert main.run(["decide", "--input", str(mapa), "--expected-tag-value", VALOR, "--output", str(decisao)]) == 0
    assert main.run(["apply", "--input", str(decisao), "--live", "--output", str(resultado)]) == 0

    execucao = json.loads(resultado.read_text())
    tagueados = {r["arn"] for r in execucao["recursos"] if r["categoria_final"] == "tagueado_sucesso"}
    conta = aws.client("sts").get_caller_identity()["Account"]
    for alvo in (
        "arn:aws:s3:::bucket-sem-tag",
        f"arn:aws:dynamodb:{R}:{conta}:table/tsem",
        f"arn:aws:lambda:{R}:{conta}:function:fsem",
        f"arn:aws:rds:{R}:{conta}:db:dbsem",
    ):
        assert alvo in tagueados, f"{alvo} (nunca teve tag) deveria ter sido tagueado"

    # SQS: o `tag:TagResources` do MOTO ainda não suporta SQS ("Service not
    # yet supported") — limitação do simulador, não do código. O que importa
    # aqui é o comportamento: a falha é reportada por recurso, com o código
    # original, sem derrubar o resto da execução.
    fila = next(r for r in execucao["recursos"] if r["arn"] == f"arn:aws:sqs:{R}:{conta}:qsem")
    assert fila["categoria_final"] in ("tagueado_sucesso", "falhou")
    if fila["categoria_final"] == "falhou":
        assert fila["detalhe_erro"]["codigo"]

    # E a tag está de fato no recurso:
    tags_bucket = aws.client("s3").get_bucket_tagging(Bucket="bucket-sem-tag")["TagSet"]
    assert {"Key": "aws-apn-id", "Value": VALOR} in tags_bucket


def test_apply_somente_arns_nao_toca_em_recurso_fora_da_lista(aws, tmp_path, monkeypatch):
    """Segurança em conta compartilhada: com --somente-arns, só os ARNs da
    lista são tagueados; os demais recursos sem tag continuam sem tag."""
    _criar_pares(aws)
    monkeypatch.setattr(regions, "get_active_regions", lambda session, base_region="us-east-1": [R])
    mapa, decisao, resultado, lista = (tmp_path / n for n in ("m.json", "d.json", "r.json", "arns.txt"))
    lista.write_text("# só este\narn:aws:s3:::bucket-sem-tag\tqualquer-coisa\n")
    assert main.run(["map", "--expected-tag-value", VALOR, "--regioes", R, "--output", str(mapa)]) == 0
    assert main.run(["decide", "--input", str(mapa), "--expected-tag-value", VALOR, "--output", str(decisao)]) == 0
    assert main.run(["apply", "--input", str(decisao), "--live", "--somente-arns", str(lista), "--output", str(resultado)]) == 0

    execucao = json.loads(resultado.read_text())
    assert {r["arn"] for r in execucao["recursos"]} == {"arn:aws:s3:::bucket-sem-tag"}
    s3 = aws.client("s3")
    assert {"Key": "aws-apn-id", "Value": VALOR} in s3.get_bucket_tagging(Bucket="bucket-sem-tag")["TagSet"]
    tabela = aws.client("dynamodb").list_tags_of_resource(
        ResourceArn=f"arn:aws:dynamodb:{R}:{aws.client('sts').get_caller_identity()['Account']}:table/tsem"
    )["Tags"]
    assert not any(t["Key"] == "aws-apn-id" for t in tabela), "recurso fora da lista foi tagueado"


def test_map_regioes_rejeita_regiao_inativa(aws, tmp_path, monkeypatch):
    monkeypatch.setattr(regions, "get_active_regions", lambda session, base_region="us-east-1": [R])
    assert main.run(["map", "--expected-tag-value", VALOR, "--regioes", "xx-nada-1", "--output", str(tmp_path / "m.json")]) == 1


def test_politica_iam_cobre_permissoes_de_leitura():
    """A política gerada (`politica-iam`, lida do template do hub) precisa
    conter toda ação exigida pela descoberta nativa, pelas recomendações e
    pelo preflight — senão a Lambda ou quem roda o CLI recebe AccessDenied
    (que vira falhas_descoberta) sem saber por quê."""
    from aws_prm_tagging import politica_iam, recomendacoes

    exigidas = set(native_discovery.required_iam_actions()) | set(recomendacoes.IAM_LEITURA) | {
        "cloudtrail:DescribeTrails",
        "cloudtrail:GetTrailStatus",
        "cloudtrail:GetEventSelectors",
        "cloudtrail:LookupEvents",
    }
    faltando = sorted(exigidas - set(politica_iam.acoes_leitura()))
    assert faltando == [], f"template-hub.yaml (role do tagueamento inicial) sem: {faltando}"


def test_politica_iam_escrita_sempre_restrita_a_aws_apn_id():
    from aws_prm_tagging import politica_iam

    pol = politica_iam.politica(com_escrita=True)
    escrita = [st for st in pol["Statement"] if st["Sid"] != "PrmLeitura"]
    assert escrita and all(
        st["Condition"] == {"ForAllValues:StringEquals": {"aws:TagKeys": ["aws-apn-id"]}} for st in escrita
    )
    acoes = {a for st in escrita for a in (st["Action"] if isinstance(st["Action"], list) else [st["Action"]])}
    assert {"tag:TagResources", "ec2:CreateTags", "apigateway:PUT"} <= acoes
def test_ecs_service_sem_tag_e_descoberto(aws):
    """service ECS sem tag entra na descoberta nativa.
    O `run_task` Fargate do moto tem bug interno — as tasks são cobertas no
    teste seguinte, com client falso."""
    ecs = aws.client("ecs", region_name=R)
    conta = aws.client("sts").get_caller_identity()["Account"]
    ecs.create_cluster(clusterName="app")
    ecs.register_task_definition(family="web", containerDefinitions=[{"name": "c", "image": "nginx", "memory": 512}])
    ecs.create_service(cluster="app", serviceName="web", taskDefinition="web", desiredCount=0)
    nativos, falhas = native_discovery.discover_native_resources(aws, R, conta, load_services(), VALOR)
    assert any(r["arn"].endswith("service/app/web") for r in nativos), "service ECS não descoberto"
    assert not [f for f in falhas if "ecs" in f["etapa"]]


def test_ecs_tasks_formato_longo_entram_e_curto_fica_de_fora():
    longa = "arn:aws:ecs:us-east-1:111111111111:task/app/0123456789abcdef"
    curta = "arn:aws:ecs:us-east-1:111111111111:task/0123456789abcdef"

    class _Pag:
        def __init__(self, paginas):
            self.paginas = paginas

        def paginate(self, **_):
            return iter(self.paginas)

    class _Ecs:
        def get_paginator(self, op):
            return _Pag({
                "list_clusters": [{"clusterArns": ["arn:aws:ecs:us-east-1:111111111111:cluster/app"]}],
                "list_services": [{"serviceArns": []}],
                "list_tasks": [{"taskArns": [longa, curta]}],
            }[op])

    class _Sessao:
        def client(self, *a, **k):
            return _Ecs()

    assert native_discovery._ecs_servicos_e_tasks(_Sessao(), "us-east-1") == [(longa, "task")]


def test_map_so_em_us_east_2_acha_bucket_sem_tag_de_la_e_nao_de_outras_regioes(aws, tmp_path, monkeypatch):
    """Roteiro de sandbox só em Ohio: com --regioes us-east-2, o bucket sem
    tag de us-east-2 precisa aparecer (antes ficava invisível, porque o S3 só
    era listado na passada de us-east-1) e o de us-east-1 NÃO."""
    s3 = aws.client("s3")
    s3.create_bucket(Bucket="ohio-sem-tag", CreateBucketConfiguration={"LocationConstraint": "us-east-2"})
    s3.create_bucket(Bucket="virginia-sem-tag")
    monkeypatch.setattr(regions, "get_active_regions", lambda session, base_region="us-east-1": ["us-east-1", "us-east-2"])
    mapa = tmp_path / "m.json"
    assert main.run(["map", "--expected-tag-value", VALOR, "--regioes", "us-east-2", "--output", str(mapa)]) == 0
    por_arn = {r["arn"]: r for r in json.loads(mapa.read_text())["recursos"]}
    assert "arn:aws:s3:::ohio-sem-tag" in por_arn and por_arn["arn:aws:s3:::ohio-sem-tag"]["regiao"] == "us-east-2"
    assert "arn:aws:s3:::virginia-sem-tag" not in por_arn, "bucket fora do escopo de --regioes"
