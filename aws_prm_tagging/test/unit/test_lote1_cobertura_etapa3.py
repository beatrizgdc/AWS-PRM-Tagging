"""Regressões do Lote 1 (docs/plano-finalizacao.md) — bugs e lacunas de
cobertura da Etapa 3 corrigidos em 2026-09-30. 100% offline.

Os shapes de evento usados aqui seguem o que o CloudTrail grava (camelCase,
sufixo de versão no eventName, empacotamento XML→JSON do EC2) — os que ainda
não foram capturados em sandbox estão marcados como tal e fazem parte do
roteiro de validação sem custo (test/manual-live-etapa3/README.md, Lote 1).
"""
from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from aws_prm_tagging import event_parser, handler_continuous_tagging, resource_discovery
from aws_prm_tagging.event_parser import ExtractedResource
from aws_prm_tagging.resource_expansion import expand_instance_volumes

REGIAO = "us-east-1"
CONTA = "000000000000"


def _arns(resultado):
    return [r.arn for r in resultado]


# ---------------------------------------------------------------------------
# Lambda — bug do eventName versionado + camelCase
# ---------------------------------------------------------------------------


def test_lambda_evento_versionado_do_cloudtrail_e_reconhecido(cloudtrail_event_factory):
    evento = cloudtrail_event_factory(
        event_source="lambda.amazonaws.com",
        event_name="CreateFunction20150331",
        detail_overrides={
            "responseElements": {"functionArn": f"arn:aws:lambda:{REGIAO}:{CONTA}:function:minha-fn"}
        },
    )
    resultado = event_parser.parse_creation_event(evento)
    assert _arns(resultado) == [f"arn:aws:lambda:{REGIAO}:{CONTA}:function:minha-fn"]
    assert resultado[0].servico == "AWS Lambda"


def test_lambda_sem_arn_na_resposta_constroi_pelo_nome_pedido(cloudtrail_event_factory):
    evento = cloudtrail_event_factory(
        event_source="lambda.amazonaws.com",
        event_name="CreateFunction20150331",
        detail_overrides={"requestParameters": {"functionName": "minha-fn"}, "responseElements": None},
    )
    assert _arns(event_parser.parse_creation_event(evento)) == [
        f"arn:aws:lambda:{REGIAO}:{CONTA}:function:minha-fn"
    ]


def test_lambda_remove_qualificador_de_versao_do_arn(cloudtrail_event_factory):
    evento = cloudtrail_event_factory(
        event_source="lambda.amazonaws.com",
        event_name="CreateFunction20150331",
        detail_overrides={
            "responseElements": {"functionArn": f"arn:aws:lambda:{REGIAO}:{CONTA}:function:minha-fn:1"}
        },
    )
    assert _arns(event_parser.parse_creation_event(evento)) == [
        f"arn:aws:lambda:{REGIAO}:{CONTA}:function:minha-fn"
    ]


@pytest.mark.parametrize(
    "bruto,normalizado",
    [
        ("CreateFunction20150331", "CreateFunction"),
        ("UpdateFunctionConfiguration20150331v2", "UpdateFunctionConfiguration"),
        ("CreateDistribution2020_05_31", "CreateDistribution"),
        ("CreateDistributionWithTags2020_05_31", "CreateDistributionWithTags"),
        ("CreateClusterV2", "CreateClusterV2"),  # MSK — \"V2\" é parte do nome, não sufixo
        ("RunInstances", "RunInstances"),
    ],
)
def test_normalize_event_name(bruto, normalizado):
    assert event_parser.normalize_event_name(bruto) == normalizado


# ---------------------------------------------------------------------------
# DynamoDB — camelCase + restores
# ---------------------------------------------------------------------------


def test_dynamodb_create_table_le_arn_em_camelcase(cloudtrail_event_factory):
    arn = f"arn:aws:dynamodb:{REGIAO}:{CONTA}:table/Musica"
    evento = cloudtrail_event_factory(
        event_source="dynamodb.amazonaws.com",
        event_name="CreateTable",
        detail_overrides={"responseElements": {"tableDescription": {"tableArn": arn}}},
    )
    assert _arns(event_parser.parse_creation_event(evento)) == [arn]


def test_dynamodb_create_table_fallback_pelo_nome(cloudtrail_event_factory):
    evento = cloudtrail_event_factory(
        event_source="dynamodb.amazonaws.com",
        event_name="CreateTable",
        detail_overrides={"requestParameters": {"tableName": "Musica"}},
    )
    assert _arns(event_parser.parse_creation_event(evento)) == [f"arn:aws:dynamodb:{REGIAO}:{CONTA}:table/Musica"]


@pytest.mark.parametrize("operacao", ["RestoreTableFromBackup", "RestoreTableToPointInTime"])
def test_dynamodb_restore_usa_tabela_de_destino(cloudtrail_event_factory, operacao):
    evento = cloudtrail_event_factory(
        event_source="dynamodb.amazonaws.com",
        event_name=operacao,
        detail_overrides={"requestParameters": {"targetTableName": "Restaurada", "sourceTableName": "Origem"}},
    )
    assert _arns(event_parser.parse_creation_event(evento)) == [
        f"arn:aws:dynamodb:{REGIAO}:{CONTA}:table/Restaurada"
    ]


# ---------------------------------------------------------------------------
# CloudFront — nome versionado e variante WithTags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event_name",
    ["CreateDistribution2020_05_31", "CreateDistributionWithTags", "CreateDistributionWithTags2020_05_31"],
)
def test_cloudfront_variantes_de_evento(cloudtrail_event_factory, event_name):
    evento = cloudtrail_event_factory(
        event_source="cloudfront.amazonaws.com",
        event_name=event_name,
        detail_overrides={"responseElements": {"distribution": {"id": "E123ABC"}}},
    )
    assert _arns(event_parser.parse_creation_event(evento)) == [f"arn:aws:cloudfront::{CONTA}:distribution/E123ABC"]


# ---------------------------------------------------------------------------
# EC2 — evento nativo, CreateFleet, snapshots, NAT, EIP
# ---------------------------------------------------------------------------


def _evento_ec2_state_change(instance_id="i-0abc1234def567890", state="running"):
    return {
        "version": "0",
        "id": "evt-1",
        "detail-type": "EC2 Instance State-change Notification",
        "source": "aws.ec2",
        "account": CONTA,
        "region": REGIAO,
        "detail": {"instance-id": instance_id, "state": state},
    }


def test_evento_nativo_ec2_gera_arn_da_instancia():
    resultado = event_parser.parse_creation_event(_evento_ec2_state_change())
    assert _arns(resultado) == [f"arn:aws:ec2:{REGIAO}:{CONTA}:instance/i-0abc1234def567890"]
    assert resultado[0].servico == "Amazon EC2"


def test_evento_nativo_ec2_com_id_invalido_devolve_vazio():
    assert event_parser.parse_creation_event(_evento_ec2_state_change(instance_id="nao-e-instancia")) == []


def test_create_fleet_encontra_ids_com_item_unico_ou_lista(cloudtrail_event_factory):
    # Empacotamento XML->JSON do CreateFleet (item único vira dict, vários
    # viram lista) — shape ainda NÃO capturado em sandbox; o extractor só
    # depende do formato inconfundível do ID de instância.
    evento = cloudtrail_event_factory(
        event_name="CreateFleet",
        detail_overrides={
            "responseElements": {
                "CreateFleetResponse": {
                    "fleetId": "fleet-1",
                    "fleetInstanceSet": {
                        "item": [
                            {"instanceIds": {"item": "i-0aaaaaaaaaaaaaaa1"}},
                            {"instanceIds": {"item": ["i-0bbbbbbbbbbbbbbb2", "i-0ccccccccccccccc3"]}},
                        ]
                    },
                }
            }
        },
    )
    assert _arns(event_parser.parse_creation_event(evento)) == [
        f"arn:aws:ec2:{REGIAO}:{CONTA}:instance/i-0aaaaaaaaaaaaaaa1",
        f"arn:aws:ec2:{REGIAO}:{CONTA}:instance/i-0bbbbbbbbbbbbbbb2",
        f"arn:aws:ec2:{REGIAO}:{CONTA}:instance/i-0ccccccccccccccc3",
    ]


def test_create_snapshot_gera_arn_sem_conta(cloudtrail_event_factory):
    # ARN de snapshot EBS não tem conta (formato documentado pela AWS).
    evento = cloudtrail_event_factory(
        event_name="CreateSnapshot", detail_overrides={"responseElements": {"snapshotId": "snap-0123"}}
    )
    assert _arns(event_parser.parse_creation_event(evento)) == [f"arn:aws:ec2:{REGIAO}::snapshot/snap-0123"]


def test_create_snapshots_multiplos(cloudtrail_event_factory):
    evento = cloudtrail_event_factory(
        event_name="CreateSnapshots",
        detail_overrides={
            "responseElements": {"snapshotSet": {"items": [{"snapshotId": "snap-1"}, {"snapshotId": "snap-2"}]}}
        },
    )
    assert len(event_parser.parse_creation_event(evento)) == 2


def test_create_nat_gateway(cloudtrail_event_factory):
    evento = cloudtrail_event_factory(
        event_name="CreateNatGateway",
        detail_overrides={"responseElements": {"CreateNatGatewayResponse": {"natGateway": {"natGatewayId": "nat-0abc"}}}},
    )
    assert _arns(event_parser.parse_creation_event(evento)) == [f"arn:aws:ec2:{REGIAO}:{CONTA}:natgateway/nat-0abc"]


def test_allocate_address(cloudtrail_event_factory):
    evento = cloudtrail_event_factory(
        event_name="AllocateAddress", detail_overrides={"responseElements": {"allocationId": "eipalloc-0abc"}}
    )
    assert _arns(event_parser.parse_creation_event(evento)) == [
        f"arn:aws:ec2:{REGIAO}:{CONTA}:elastic-ip/eipalloc-0abc"
    ]


# ---------------------------------------------------------------------------
# RDS — restores/réplica/snapshot + desambiguação DocumentDB/Neptune
# ---------------------------------------------------------------------------


def test_rds_restore_le_arn_achatado(cloudtrail_event_factory):
    arn = f"arn:aws:rds:{REGIAO}:{CONTA}:db:restaurado"
    evento = cloudtrail_event_factory(
        event_source="rds.amazonaws.com",
        event_name="RestoreDBInstanceFromDBSnapshot",
        detail_overrides={"responseElements": {"dBInstanceArn": arn}},
    )
    assert _arns(event_parser.parse_creation_event(evento)) == [arn]


def test_rds_point_in_time_fallback_pelo_identificador_de_destino(cloudtrail_event_factory):
    evento = cloudtrail_event_factory(
        event_source="rds.amazonaws.com",
        event_name="RestoreDBInstanceToPointInTime",
        detail_overrides={"requestParameters": {"targetDBInstanceIdentifier": "pitr-1"}},
    )
    assert _arns(event_parser.parse_creation_event(evento)) == [f"arn:aws:rds:{REGIAO}:{CONTA}:db:pitr-1"]


def test_rds_cluster_snapshot(cloudtrail_event_factory):
    evento = cloudtrail_event_factory(
        event_source="rds.amazonaws.com",
        event_name="CreateDBClusterSnapshot",
        detail_overrides={"requestParameters": {"dBClusterSnapshotIdentifier": "snap-manual"}},
    )
    assert _arns(event_parser.parse_creation_event(evento)) == [
        f"arn:aws:rds:{REGIAO}:{CONTA}:cluster-snapshot:snap-manual"
    ]


@pytest.mark.parametrize(
    "engine,servico_esperado",
    [
        ("docdb", "Amazon DocumentDB (with MongoDB compatibility)"),
        ("neptune", "Amazon Neptune"),
        ("aurora-postgresql", "Amazon Relational Database Service (RDS)"),
        (None, "Amazon Relational Database Service (RDS)"),
    ],
)
def test_rds_create_db_cluster_rotula_pelo_engine(cloudtrail_event_factory, engine, servico_esperado):
    request = {"dBClusterIdentifier": "c1"}
    if engine:
        request["engine"] = engine
    evento = cloudtrail_event_factory(
        event_source="rds.amazonaws.com",
        event_name="CreateDBCluster",
        detail_overrides={
            "requestParameters": request,
            "responseElements": {"dBClusterArn": f"arn:aws:rds:{REGIAO}:{CONTA}:cluster:c1"},
        },
    )
    resultado = event_parser.parse_creation_event(evento)
    assert resultado[0].servico == servico_esperado


# ---------------------------------------------------------------------------
# ElastiCache — replication group / serverless
# ---------------------------------------------------------------------------


def test_elasticache_replication_group_achatado(cloudtrail_event_factory):
    arn = f"arn:aws:elasticache:{REGIAO}:{CONTA}:replicationgroup:rg-1"
    evento = cloudtrail_event_factory(
        event_source="elasticache.amazonaws.com",
        event_name="CreateReplicationGroup",
        detail_overrides={"responseElements": {"aRN": arn}},
    )
    assert _arns(event_parser.parse_creation_event(evento)) == [arn]


def test_elasticache_serverless_fallback_pelo_nome(cloudtrail_event_factory):
    evento = cloudtrail_event_factory(
        event_source="elasticache.amazonaws.com",
        event_name="CreateServerlessCache",
        detail_overrides={"requestParameters": {"serverlessCacheName": "cache-1"}},
    )
    assert _arns(event_parser.parse_creation_event(evento)) == [
        f"arn:aws:elasticache:{REGIAO}:{CONTA}:serverlesscache:cache-1"
    ]


# ---------------------------------------------------------------------------
# Expansão instância -> volumes EBS
# ---------------------------------------------------------------------------


def _instancia(instance_id):
    return ExtractedResource(
        arn=f"arn:aws:ec2:{REGIAO}:{CONTA}:instance/{instance_id}", servico="Amazon EC2", regiao=REGIAO
    )


def _reserva(instance_id, volumes):
    return {
        "Instances": [
            {
                "InstanceId": instance_id,
                "BlockDeviceMappings": [{"DeviceName": f"/dev/x{i}", "Ebs": {"VolumeId": v}} for i, v in enumerate(volumes)],
            }
        ]
    }


class _FakeEc2:
    def __init__(self, por_instancia: dict, inexistentes=(), erro_codigo=None):
        self.por_instancia = por_instancia
        self.inexistentes = set(inexistentes)
        self.erro_codigo = erro_codigo
        self.chamadas: list[list[str]] = []

    def describe_instances(self, InstanceIds):
        self.chamadas.append(list(InstanceIds))
        if self.erro_codigo:
            raise ClientError({"Error": {"Code": self.erro_codigo, "Message": "x"}}, "DescribeInstances")
        if self.inexistentes & set(InstanceIds):
            raise ClientError({"Error": {"Code": "InvalidInstanceID.NotFound", "Message": "x"}}, "DescribeInstances")
        return {"Reservations": [_reserva(i, self.por_instancia.get(i, [])) for i in InstanceIds]}


def test_expansao_acrescenta_volume_root_e_dados(fake_session_factory):
    ec2 = _FakeEc2({"i-0aaaaaaaaaaaaaaa1": ["vol-root", "vol-dados"]})
    adicionais, falhas = expand_instance_volumes(
        fake_session_factory({"ec2": ec2}), [_instancia("i-0aaaaaaaaaaaaaaa1")]
    )
    assert _arns(adicionais) == [
        f"arn:aws:ec2:{REGIAO}:{CONTA}:volume/vol-root",
        f"arn:aws:ec2:{REGIAO}:{CONTA}:volume/vol-dados",
    ]
    assert all(a.servico == "Amazon EC2" for a in adicionais)
    assert falhas == []


def test_expansao_nao_duplica_volume_ja_presente(fake_session_factory):
    ec2 = _FakeEc2({"i-0aaaaaaaaaaaaaaa1": ["vol-root"]})
    ja_extraido = ExtractedResource(
        arn=f"arn:aws:ec2:{REGIAO}:{CONTA}:volume/vol-root", servico="Amazon EC2", regiao=REGIAO
    )
    adicionais, _ = expand_instance_volumes(
        fake_session_factory({"ec2": ec2}), [_instancia("i-0aaaaaaaaaaaaaaa1"), ja_extraido]
    )
    assert adicionais == []


def test_expansao_ignora_recursos_que_nao_sao_instancia(fake_session_factory):
    bucket = ExtractedResource(arn="arn:aws:s3:::b", servico="Amazon S3", regiao=REGIAO)
    adicionais, falhas = expand_instance_volumes(fake_session_factory({}), [bucket])
    assert adicionais == [] and falhas == []


def test_expansao_instancia_inexistente_nao_e_falha(fake_session_factory):
    ec2 = _FakeEc2({}, inexistentes={"i-0aaaaaaaaaaaaaaa1"})
    adicionais, falhas = expand_instance_volumes(fake_session_factory({"ec2": ec2}), [_instancia("i-0aaaaaaaaaaaaaaa1")])
    assert adicionais == [] and falhas == []


def test_expansao_um_id_inexistente_nao_derruba_o_lote(fake_session_factory):
    ec2 = _FakeEc2({"i-0bbbbbbbbbbbbbbb2": ["vol-b"]}, inexistentes={"i-0aaaaaaaaaaaaaaa1"})
    adicionais, falhas = expand_instance_volumes(
        fake_session_factory({"ec2": ec2}), [_instancia("i-0aaaaaaaaaaaaaaa1"), _instancia("i-0bbbbbbbbbbbbbbb2")]
    )
    assert _arns(adicionais) == [f"arn:aws:ec2:{REGIAO}:{CONTA}:volume/vol-b"]
    assert falhas == []


def test_expansao_access_denied_vira_falha_por_instancia(fake_session_factory):
    ec2 = _FakeEc2({}, erro_codigo="UnauthorizedOperation")
    adicionais, falhas = expand_instance_volumes(fake_session_factory({"ec2": ec2}), [_instancia("i-0aaaaaaaaaaaaaaa1")])
    assert adicionais == []
    assert falhas[0]["codigo"] == "UnauthorizedOperation"
    assert falhas[0]["arn"].endswith("i-0aaaaaaaaaaaaaaa1")


# ---------------------------------------------------------------------------
# Handler — client SNS na região do tópico (pré-requisito do split hub/regional)
# ---------------------------------------------------------------------------


def test_client_sns_usa_regiao_do_topico():
    class _Sessao:
        def __init__(self):
            self.pedidos = []

        def client(self, nome, region_name=None):
            self.pedidos.append((nome, region_name))
            return object()

    sessao = _Sessao()
    handler_continuous_tagging._sns_client_for_topic(sessao, "arn:aws:sns:sa-east-1:000000000000:prm")
    assert sessao.pedidos == [("sns", "sa-east-1")]


# ---------------------------------------------------------------------------
# Etapa 1 — rótulo de faturamento dos sub-recursos de EKS (item 13)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tipo,esperado",
    [
        ("node", "Amazon EC2"),
        ("ebs_volume", "Amazon EC2"),
        ("load_balancer", "Elastic Load Balancing"),
        ("cluster", "Amazon EKS"),
        ("node_group", "Amazon EKS"),
    ],
)
def test_subrecurso_eks_tem_servico_de_faturamento_correto(tipo, esperado):
    r = resource_discovery._build_resource(
        "arn:x", "Amazon EKS", REGIAO, {}, "pc:abc", tipo_recurso=tipo, cluster_eks="c1"
    )
    assert r["servico"] == "Amazon EKS"  # roteamento/escopo inalterados
    assert r["servico_faturamento"] == esperado
    assert r["cluster_eks"] == "c1"


def test_recurso_generico_servico_de_faturamento_igual_ao_servico():
    r = resource_discovery._build_resource("arn:x", "Amazon S3", REGIAO, {}, "pc:abc")
    assert r["servico_faturamento"] == "Amazon S3"
