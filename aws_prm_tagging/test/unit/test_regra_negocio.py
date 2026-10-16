"""Regra de negócio:

- valor de outro parceiro em `aws-apn-id` é sobrescrito (estado anterior
  registrado);
- TODO IaC (Terraform/OpenTofu, CloudFormation, CDK, Pulumi) nunca é
  tagueado e vai para a lista do cliente;
- chave de grafia parecida: `aws-apn-id` é adicionada e o estado anterior
  registrado;
- Terraform detectado pelo `userAgent` do CloudTrail (Etapa 3 e histórico).
"""
from __future__ import annotations

import csv
import io
import json

import pytest
from botocore.exceptions import ClientError

from aws_prm_tagging import cloudtrail_iac, decision, iac_detection, relatorio_cliente, tag_execution
from aws_prm_tagging.handler_continuous_tagging import handler

EXPECTED = "pc:5ugbbrmu7ud3u5hsipfzug61p"
UA_TERRAFORM = (
    "APN/1.0 HashiCorp/1.0 Terraform/1.9.5 (+https://www.terraform.io) terraform-provider-aws/5.70.0 "
    "(+https://registry.terraform.io/providers/hashicorp/aws) aws-sdk-go-v2/1.32.2 os/linux"
)


# ---------------------------------------------------------------------------
# Detecção de IaC por tags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tags, tipo, ferramenta",
    [
        ({"managed-by": "pulumi"}, iac_detection.IAC_PULUMI_HEURISTICO, "Pulumi"),
        ({"pulumi:project": "loja"}, iac_detection.IAC_PULUMI_HEURISTICO, "Pulumi"),
        ({"ManagedBy": "OpenTofu"}, iac_detection.IAC_TERRAFORM_HEURISTICO, "OpenTofu"),
        ({"Provisioner": "cdk"}, iac_detection.IAC_CLOUDFORMATION, "AWS CDK"),
        ({"aws:cloudformation:stack-name": "s"}, iac_detection.IAC_CLOUDFORMATION, "CloudFormation/CDK"),
        ({"Nome": "x", "Owner": "time-a"}, iac_detection.IAC_DESCONHECIDO, None),
    ],
)
def test_detect_iac_todas_as_ferramentas(tags, tipo, ferramenta):
    r = iac_detection.detect_iac(tags)
    assert r["tipo"] == tipo
    assert r["ferramenta"] == ferramenta
    if tipo != iac_detection.IAC_DESCONHECIDO:
        assert r["evidencia"]


def test_managed_by_tem_prioridade_na_evidencia():
    r = iac_detection.detect_iac({"Outra": "terraform", "managed-by": "pulumi"})
    assert r["ferramenta"] == "Pulumi"


# ---------------------------------------------------------------------------
# Detecção pelo evento do CloudTrail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "detail, ferramenta",
    [
        ({"userAgent": UA_TERRAFORM}, "Terraform"),
        ({"userAgent": "OpenTofu/1.8.0 terraform-provider-aws/5.70.0"}, "OpenTofu"),
        ({"userAgent": "pulumi-aws/v6.56.1 aws-sdk-go/1.55"}, "Pulumi"),
        ({"userAgent": "cloudformation.amazonaws.com", "sourceIPAddress": "cloudformation.amazonaws.com"},
         "CloudFormation/CDK"),
        ({"userAgent": "x", "userIdentity": {"invokedBy": "cloudformation.amazonaws.com"}}, "CloudFormation/CDK"),
    ],
)
def test_detect_iac_from_cloudtrail(detail, ferramenta):
    r = iac_detection.detect_iac_from_cloudtrail(detail)
    assert r is not None and r["ferramenta"] == ferramenta
    assert r["tipo"] in iac_detection.IAC_DETECTADO
    assert r["evidencia"].startswith("CloudTrail ")


@pytest.mark.parametrize(
    "detail",
    [
        {"userAgent": "aws-cli/2.17.0 Python/3.11"},
        {"userAgent": "Boto3/1.35.0 md/Botocore#1.35.0", "sourceIPAddress": "10.0.0.1"},
        {"userAgent": "AWS Internal", "sourceIPAddress": "AWS Internal"},
        {},
        None,
    ],
)
def test_detect_iac_from_cloudtrail_sem_sinal(detail):
    assert iac_detection.detect_iac_from_cloudtrail(detail) is None


# ---------------------------------------------------------------------------
# Histórico do CloudTrail (LookupEvents)
# ---------------------------------------------------------------------------


class _FakeCloudTrail:
    def __init__(self, eventos_por_nome=None, erro=None):
        self.eventos_por_nome = eventos_por_nome or {}
        self.erro = erro
        self.chamadas: list[str] = []

    def lookup_events(self, LookupAttributes, MaxResults, NextToken=None):
        nome = LookupAttributes[0]["AttributeValue"]
        self.chamadas.append(nome)
        if self.erro:
            raise ClientError({"Error": {"Code": self.erro, "Message": "negado"}}, "LookupEvents")
        return {"Events": self.eventos_por_nome.get(nome, [])}


def _ev(nome, user_agent, read_only=False):
    return {
        "EventName": nome,
        "EventTime": "2026-10-01T10:00:00Z",
        "ReadOnly": "true" if read_only else "false",
        "CloudTrailEvent": json.dumps({"eventName": nome, "userAgent": user_agent, "readOnly": read_only}),
    }


def test_nomes_para_consulta():
    assert cloudtrail_iac.nomes_para_consulta("arn:aws:ec2:us-east-1:1:instance/i-0abc") == [
        "arn:aws:ec2:us-east-1:1:instance/i-0abc",
        "i-0abc",
    ]
    assert cloudtrail_iac.nomes_para_consulta("arn:aws:s3:::meu-bucket") == ["arn:aws:s3:::meu-bucket", "meu-bucket"]
    assert cloudtrail_iac.nomes_para_consulta("arn:aws:lambda:us-east-1:1:function:fn")[-1] == "fn"


def test_historico_acha_terraform_pelo_id():
    ct = _FakeCloudTrail({"i-0abc": [_ev("RunInstances", UA_TERRAFORM)]})
    r = cloudtrail_iac.iac_por_historico(ct, "arn:aws:ec2:us-east-1:1:instance/i-0abc")
    assert r["ferramenta"] == "Terraform"
    assert "RunInstances" in r["evidencia"]


def test_historico_ignora_leitura_de_data_source():
    """Um data source do Terraform só LÊ o recurso — não prova que o
    Terraform o gerencia."""
    ct = _FakeCloudTrail({"meu-bucket": [_ev("GetBucketTagging", UA_TERRAFORM, read_only=True)]})
    assert cloudtrail_iac.iac_por_historico(ct, "arn:aws:s3:::meu-bucket") is None


def _recurso(arn, regiao="us-east-1", status="sem_tag", tipo="desconhecido"):
    return {"arn": arn, "regiao": regiao, "status_tag": status, "iac": {"tipo": tipo, "stack_name": None}}


class _Sessao:
    def __init__(self, clients):
        self.clients = clients

    def client(self, nome, region_name=None):
        return self.clients[region_name]


def test_enriquecer_so_candidatos_e_marca_resultado():
    ct = _FakeCloudTrail({"b-tf": [_ev("CreateBucket", UA_TERRAFORM)]})
    recursos = [
        _recurso("arn:aws:s3:::b-tf"),
        _recurso("arn:aws:s3:::b-console"),
        _recurso("arn:aws:s3:::b-ok", status="ok"),
        _recurso("arn:aws:s3:::b-cfn", tipo="cloudformation"),
    ]
    stats = cloudtrail_iac.enriquecer_iac(_Sessao({"us-east-1": ct}), recursos)
    assert recursos[0]["iac"]["tipo"] == iac_detection.IAC_TERRAFORM_HEURISTICO
    assert recursos[0]["iac_historico"] == cloudtrail_iac.HISTORICO_DETECTADO
    assert recursos[1]["iac_historico"] == cloudtrail_iac.HISTORICO_SEM_SINAL
    assert "iac_historico" not in recursos[2] and "iac_historico" not in recursos[3]
    assert stats["consultados"] == 2 and stats["detectados"] == 1
    assert not any("b-ok" in c or "b-cfn" in c for c in ct.chamadas)


def test_enriquecer_access_denied_para_a_regiao_e_segue():
    negado = _FakeCloudTrail(erro="AccessDeniedException")
    ok = _FakeCloudTrail()
    recursos = [
        _recurso("arn:aws:s3:::a1", regiao="eu-west-1"),
        _recurso("arn:aws:s3:::a2", regiao="eu-west-1"),
        _recurso("arn:aws:s3:::b1", regiao="us-east-1"),
    ]
    stats = cloudtrail_iac.enriquecer_iac(_Sessao({"eu-west-1": negado, "us-east-1": ok}), recursos)
    assert len(negado.chamadas) == 1  # não insiste na região negada
    assert [r["iac_historico"] for r in recursos] == ["erro", "nao_verificado", "sem_sinal"]
    assert [f["regiao"] for f in stats["falhas"]] == ["eu-west-1"]


def test_enriquecer_respeita_orcamento_de_tempo():
    tempos = iter([0, 0, 100, 100, 100])
    recursos = [_recurso("arn:aws:s3:::x1"), _recurso("arn:aws:s3:::x2")]
    stats = cloudtrail_iac.enriquecer_iac(
        _Sessao({"us-east-1": _FakeCloudTrail()}), recursos, orcamento_segundos=10, relogio=lambda: next(tempos)
    )
    assert recursos[0]["iac_historico"] == "sem_sinal"
    assert recursos[1]["iac_historico"] == "nao_verificado"
    assert stats["nao_verificados"] == 1


# ---------------------------------------------------------------------------
# Relatório para o cliente
# ---------------------------------------------------------------------------


def _relatorio_execucao():
    def d(arn, decisao, **extra):
        base = {
            "arn": arn, "servico": "Amazon S3", "regiao": "us-east-1", "tipo_recurso": None,
            "decisao": decisao, "motivo": "m", "iac": {"tipo": "desconhecido", "stack_name": None},
            "estado_anterior": None,
        }
        base.update(extra)
        return base

    decisao_report = {
        "conta_id": "000000000000",
        "valor_tag_esperado": EXPECTED,
        "recursos": [
            d("arn:aws:s3:::novo", "taguear"),
            d("arn:aws:s3:::outro", "sobrescrever", estado_anterior={"aws-apn-id": "pc:parceiroB", "chaves_similares": {}}),
            d("arn:aws:s3:::caixa", "taguear_com_tag_similar",
              estado_anterior={"aws-apn-id": None, "chaves_similares": {"AWS-APN-ID": "pc:x"}}),
            d("arn:aws:s3:::tf", "pular_iac",
              iac={"tipo": "terraform_heuristico", "ferramenta": "Terraform", "evidencia": "CloudTrail userAgent: T",
                   "stack_name": None}),
            d("arn:aws:s3:::cfn", "pular_iac",
              iac={"tipo": "cloudformation", "ferramenta": "CloudFormation/CDK", "evidencia": "tag", "stack_name": "pilha"},
              estado_anterior={"aws-apn-id": "pc:velho", "chaves_similares": {}}),
            d("arn:aws:s3:::eb", "pular_iac",
              iac={"tipo": "cloudformation", "stack_name": "awseb-e-1", "gerenciado_por_ferramenta_aws": "AWS Elastic Beanstalk"}),
            d("arn:aws:s3:::ok", "ja_ok"),
        ],
    }
    return tag_execution.run_tagging_execution(
        decisao_report, session=None, expected_tag_value=EXPECTED, revalidate=False
    )


def test_execucao_registra_acao_e_resumo():
    rel = _relatorio_execucao()
    por_arn = {r["arn"]: r for r in rel["recursos"]}
    assert por_arn["arn:aws:s3:::novo"]["acao"] == tag_execution.ACAO_ADICIONADA
    assert por_arn["arn:aws:s3:::outro"]["acao"] == tag_execution.ACAO_SOBRESCRITA
    assert por_arn["arn:aws:s3:::caixa"]["acao"] == tag_execution.ACAO_ADICIONADA_COM_TAG_SIMILAR
    assert por_arn["arn:aws:s3:::tf"]["categoria_final"] == "pulado_iac"
    assert rel["resumo"]["por_acao"] == {"adicionada": 1, "adicionada_com_tag_similar": 1, "sobrescrita": 1}


def test_relatorio_cliente_listas_e_exemplos():
    rc = relatorio_cliente.build_relatorio_cliente(_relatorio_execucao())
    assert rc["resumo"]["total_recursos_iac"] == 3
    assert rc["resumo"]["iac_por_ferramenta"] == {
        "AWS Elastic Beanstalk": 1, "CloudFormation/CDK": 1, "Terraform": 1
    }
    assert rc["resumo"]["total_sobrescritos"] == 1
    assert rc["resumo"]["total_adicionados_com_chave_similar"] == 1
    cfn = next(i for i in rc["recursos_iac"] if i["arn"] == "arn:aws:s3:::cfn")
    assert cfn["valor_atual_aws_apn_id"] == "pc:velho" and cfn["stack_cloudformation"] == "pilha"
    eb = next(i for i in rc["recursos_iac"] if i["arn"] == "arn:aws:s3:::eb")
    assert "opções do AWS Elastic Beanstalk" in eb["acao_recomendada"]
    assert set(rc["como_aplicar_no_iac"]) == {"Terraform", "CloudFormation"}
    assert EXPECTED in rc["como_aplicar_no_iac"]["Terraform"]
    sim = next(a for a in rc["estado_anterior"] if a["arn"] == "arn:aws:s3:::caixa")
    assert sim["simulado"] is True and sim["chaves_similares_anteriores"] == {"AWS-APN-ID": "pc:x"}


def test_relatorio_cliente_csv_e_markdown():
    rc = relatorio_cliente.build_relatorio_cliente(_relatorio_execucao())
    linhas = list(csv.reader(io.StringIO(relatorio_cliente.to_csv(rc))))
    assert linhas[0][0] == "lista"
    assert sum(1 for l in linhas[1:] if l[0] == "iac") == 3
    assert sum(1 for l in linhas[1:] if l[0] == "estado_anterior") == 2
    md = relatorio_cliente.to_markdown(rc)
    assert "## 1. Recursos gerenciados por IaC" in md
    assert "pc:parceiroB" in md and "default_tags" in md


def test_relatorio_cliente_vazio():
    rc = relatorio_cliente.build_relatorio_cliente({"recursos": [], "valor_tag_esperado": EXPECTED})
    assert rc["recursos_iac"] == [] and rc["estado_anterior"] == []
    assert "0 recurso(s)" in relatorio_cliente.to_markdown(rc)


# ---------------------------------------------------------------------------
# Etapa 3 — handler
# ---------------------------------------------------------------------------


class _FakeGeneric:
    def __init__(self, tags_por_arn):
        self.tags_por_arn = tags_por_arn
        self.escritas = []

    def get_resources(self, ResourceARNList=None, **_):
        return {
            "ResourceTagMappingList": [
                {"ResourceARN": a, "Tags": [{"Key": k, "Value": v} for k, v in self.tags_por_arn[a].items()]}
                for a in ResourceARNList
                if self.tags_por_arn.get(a)
            ]
        }

    def tag_resources(self, ResourceARNList, Tags):
        self.escritas.append(list(ResourceARNList))
        for a in ResourceARNList:
            self.tags_por_arn.setdefault(a, {}).update(Tags)
        return {"FailedResourcesMap": {}}


class _FakeSns:
    def __init__(self):
        self.msgs = []

    def publish(self, TopicArn, Message, Subject):
        self.msgs.append(json.loads(Message))


@pytest.fixture
def _env(monkeypatch):
    monkeypatch.setenv("EXPECTED_TAG_VALUE", EXPECTED)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:000000000000:prm")


def _evento_bucket(factory, nome, user_agent):
    return factory(
        event_source="s3.amazonaws.com",
        event_name="CreateBucket",
        detail_overrides={"requestParameters": {"bucketName": nome}, "userAgent": user_agent},
    )


def test_etapa3_bucket_do_terraform_nao_e_tagueado(_env, cloudtrail_event_factory, fake_session_factory):
    arn = "arn:aws:s3:::b-tf"
    generico = _FakeGeneric({arn: {}})
    sns = _FakeSns()
    handler(
        _evento_bucket(cloudtrail_event_factory, "b-tf", UA_TERRAFORM),
        session=fake_session_factory({"resourcegroupstaggingapi": generico}),
        sns_client=sns,
    )
    assert generico.escritas == []
    msg = sns.msgs[0]
    assert msg["categoria_final"] == "pulado_iac"
    assert msg["iac"]["ferramenta"] == "Terraform"
    assert "userAgent" in msg["iac"]["evidencia"]


def test_etapa3_sobrescreve_outro_parceiro_e_publica_anterior(_env, cloudtrail_event_factory, fake_session_factory):
    arn = "arn:aws:s3:::b-outro"
    generico = _FakeGeneric({arn: {"aws-apn-id": "pc:parceiroB"}})
    sns = _FakeSns()
    handler(
        _evento_bucket(cloudtrail_event_factory, "b-outro", "aws-cli/2.17"),
        session=fake_session_factory({"resourcegroupstaggingapi": generico}),
        sns_client=sns,
    )
    assert generico.escritas == [[arn]]
    assert generico.tags_por_arn[arn]["aws-apn-id"] == EXPECTED
    msg = sns.msgs[0]
    assert msg["categoria_final"] == "tagueado_sucesso"
    assert msg["acao"] == "sobrescrita"
    assert msg["estado_anterior"] == {"aws-apn-id": "pc:parceiroB", "chaves_similares": {}}


def test_etapa3_chave_parecida_adiciona_a_nossa(_env, cloudtrail_event_factory, fake_session_factory):
    arn = "arn:aws:s3:::b-caixa"
    generico = _FakeGeneric({arn: {"AWS-APN-ID": "pc:digitado"}})
    sns = _FakeSns()
    handler(
        _evento_bucket(cloudtrail_event_factory, "b-caixa", "console.amazonaws.com"),
        session=fake_session_factory({"resourcegroupstaggingapi": generico}),
        sns_client=sns,
    )
    assert generico.tags_por_arn[arn] == {"AWS-APN-ID": "pc:digitado", "aws-apn-id": EXPECTED}
    assert sns.msgs[0]["acao"] == "adicionada_com_tag_similar"


class _FakeEc2SemVolumes:
    def describe_instances(self, InstanceIds):
        return {"Reservations": [{"Instances": [{"InstanceId": i, "BlockDeviceMappings": []}]} for i in InstanceIds]}


def _evento_ec2_running(instance_id):
    return {
        "version": "0",
        "id": "e-1",
        "detail-type": "EC2 Instance State-change Notification",
        "source": "aws.ec2",
        "account": "000000000000",
        "region": "us-east-1",
        "detail": {"instance-id": instance_id, "state": "running"},
    }


def test_etapa3_instancia_do_terraform_pelo_historico(_env, fake_session_factory):
    """Evento nativo de EC2 não tem userAgent: o RunInstances original vem
    do histórico do CloudTrail."""
    iid = "i-0aaaaaaaaaaaaaaa1"
    arn = f"arn:aws:ec2:us-east-1:000000000000:instance/{iid}"
    generico = _FakeGeneric({arn: {}})
    sns = _FakeSns()
    sessao = fake_session_factory(
        {
            "resourcegroupstaggingapi": generico,
            "ec2": _FakeEc2SemVolumes(),
            "cloudtrail": _FakeCloudTrail({iid: [_ev("RunInstances", UA_TERRAFORM)]}),
        }
    )
    handler(_evento_ec2_running(iid), session=sessao, sns_client=sns)
    assert generico.escritas == []
    assert sns.msgs[0]["categoria_final"] == "pulado_iac"
    assert "RunInstances" in sns.msgs[0]["iac"]["evidencia"]


def test_etapa3_instancia_sem_historico_e_tagueada(_env, fake_session_factory):
    iid = "i-0bbbbbbbbbbbbbbb2"
    arn = f"arn:aws:ec2:us-east-1:000000000000:instance/{iid}"
    generico = _FakeGeneric({arn: {}})
    sessao = fake_session_factory(
        {"resourcegroupstaggingapi": generico, "ec2": _FakeEc2SemVolumes(), "cloudtrail": _FakeCloudTrail()}
    )
    handler(_evento_ec2_running(iid), session=sessao, sns_client=_FakeSns())
    assert generico.escritas == [[arn]]


def test_etapa3_historico_negado_nao_bloqueia(_env, fake_session_factory):
    iid = "i-0ccccccccccccccc3"
    arn = f"arn:aws:ec2:us-east-1:000000000000:instance/{iid}"
    generico = _FakeGeneric({arn: {}})
    sessao = fake_session_factory(
        {
            "resourcegroupstaggingapi": generico,
            "ec2": _FakeEc2SemVolumes(),
            "cloudtrail": _FakeCloudTrail(erro="AccessDeniedException"),
        }
    )
    handler(_evento_ec2_running(iid), session=sessao, sns_client=_FakeSns())
    assert generico.escritas == [[arn]]


# ---------------------------------------------------------------------------
# Decisão — precedência completa
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "valor, iac_tipo, similar, esperado",
    [
        (EXPECTED, "terraform_heuristico", False, decision.DECISAO_JA_OK),
        (None, "terraform_heuristico", True, decision.DECISAO_PULAR_IAC),
        ("pc:outro", "pulumi_heuristico", False, decision.DECISAO_PULAR_IAC),
        ("pc:outro", "desconhecido", True, decision.DECISAO_SOBRESCREVER),
        (None, "desconhecido", True, decision.DECISAO_TAGUEAR_COM_TAG_SIMILAR),
        (None, "desconhecido", False, decision.DECISAO_TAGUEAR),
    ],
)
def test_precedencia_mvp2(valor, iac_tipo, similar, esperado):
    decisao, motivo = decision._decidir(valor, EXPECTED, iac_tipo, similar)
    assert decisao == esperado
    assert motivo


# ---------------------------------------------------------------------------
# CLI — apply grava o relatório do cliente junto
# ---------------------------------------------------------------------------


def test_cli_apply_grava_relatorio_cliente(tmp_path, monkeypatch):
    from aws_prm_tagging import main

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    entrada = tmp_path / "decisao.json"
    entrada.write_text(
        json.dumps(
            {
                "conta_id": "000000000000",
                "valor_tag_esperado": EXPECTED,
                "recursos": [
                    {"arn": "arn:aws:s3:::tf", "servico": "Amazon S3", "regiao": "us-east-1", "decisao": "pular_iac",
                     "iac": {"tipo": "terraform_heuristico", "ferramenta": "Terraform"}},
                ],
            }
        ),
        encoding="utf-8",
    )
    saida = tmp_path / "resultado.json"
    assert main.run(["apply", "--input", str(entrada), "--output", str(saida), "--no-revalidate"]) == 0
    for ext in (".md", ".csv", ".json"):
        assert (tmp_path / f"resultado_cliente{ext}").exists()
    rc = json.loads((tmp_path / "resultado_cliente.json").read_text(encoding="utf-8"))
    assert rc["resumo"]["iac_por_ferramenta"] == {"Terraform": 1}
    # e o subcomando avulso regenera a partir do resultado
    assert main.run(["relatorio-cliente", "--input", str(saida), "--prefixo", str(tmp_path / "x")]) == 0
    assert (tmp_path / "x.md").exists()


class _FakeEc2ComVolume:
    def describe_instances(self, InstanceIds):
        return {"Reservations": [{"Instances": [
            {"InstanceId": i, "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeId": "vol-0root"}}]}
            for i in InstanceIds]}]}


def test_etapa3_volume_herda_iac_da_instancia(_env, fake_session_factory):
    """O root volume nasce do mesmo RunInstances do "Terraform": também não
    é tagueado (vai para a lista do cliente)."""
    iid = "i-0ddddddddddddddd4"
    arn = f"arn:aws:ec2:us-east-1:000000000000:instance/{iid}"
    vol = "arn:aws:ec2:us-east-1:000000000000:volume/vol-0root"
    generico = _FakeGeneric({arn: {}, vol: {}})
    sns = _FakeSns()
    sessao = fake_session_factory(
        {
            "resourcegroupstaggingapi": generico,
            "ec2": _FakeEc2ComVolume(),
            "cloudtrail": _FakeCloudTrail({iid: [_ev("RunInstances", UA_TERRAFORM)]}),
        }
    )
    handler(_evento_ec2_running(iid), session=sessao, sns_client=sns)
    assert generico.escritas == []
    assert {m["arn"]: m["categoria_final"] for m in sns.msgs} == {arn: "pulado_iac", vol: "pulado_iac"}


def test_etapa3_volume_de_instancia_sem_iac_e_tagueado(_env, fake_session_factory):
    iid = "i-0eeeeeeeeeeeeeee5"
    arn = f"arn:aws:ec2:us-east-1:000000000000:instance/{iid}"
    vol = "arn:aws:ec2:us-east-1:000000000000:volume/vol-0root"
    generico = _FakeGeneric({arn: {}, vol: {}})
    sessao = fake_session_factory(
        {"resourcegroupstaggingapi": generico, "ec2": _FakeEc2ComVolume(), "cloudtrail": _FakeCloudTrail()}
    )
    handler(_evento_ec2_running(iid), session=sessao, sns_client=_FakeSns())
    assert sorted(a for lote in generico.escritas for a in lote) == sorted([arn, vol])
