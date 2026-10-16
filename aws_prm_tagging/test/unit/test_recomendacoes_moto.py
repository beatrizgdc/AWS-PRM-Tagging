"""Recomendações de tag na origem (itens 12 e 14) contra AWS simulada."""
from __future__ import annotations

import json

import pytest

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")

from aws_prm_tagging import main, recomendacoes, regions  # noqa: E402

R = "us-east-1"
V = "pc:abc123"


@pytest.fixture
def aws(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"), ("AWS_DEFAULT_REGION", R), ("MOTO_EC2_LOAD_DEFAULT_AMIS", "false")):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with moto.mock_aws():
        yield boto3.Session(region_name=R)


def _tipos(recs):
    return sorted(r["tipo"] for r in recs)


def _cluster_eks(s):
    ec2 = s.client("ec2")
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    sub = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24")["Subnet"]["SubnetId"]
    role = s.client("iam").create_role(RoleName="r", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    s.client("eks").create_cluster(name="prod", roleArn=role, resourcesVpcConfig={"subnetIds": [sub]})
    return sub, role


def test_node_group_sem_launch_template_e_com_lt_sem_tag(aws):
    sub, role = _cluster_eks(aws)
    eks, ec2 = aws.client("eks"), aws.client("ec2")
    eks.create_nodegroup(clusterName="prod", nodegroupName="sem-lt", subnets=[sub], nodeRole=role)
    lt_sem = ec2.create_launch_template(LaunchTemplateName="lt-sem", LaunchTemplateData={"InstanceType": "t3.micro"})["LaunchTemplate"]["LaunchTemplateId"]
    lt_com = ec2.create_launch_template(
        LaunchTemplateName="lt-com",
        LaunchTemplateData={"TagSpecifications": [
            {"ResourceType": "instance", "Tags": [{"Key": "aws-apn-id", "Value": V}]},
            {"ResourceType": "volume", "Tags": [{"Key": "aws-apn-id", "Value": V}]},
        ]},
    )["LaunchTemplate"]["LaunchTemplateId"]
    eks.create_nodegroup(clusterName="prod", nodegroupName="lt-sem-tag", subnets=[sub], nodeRole=role, launchTemplate={"id": lt_sem, "version": "1"})
    eks.create_nodegroup(clusterName="prod", nodegroupName="lt-ok", subnets=[sub], nodeRole=role, launchTemplate={"id": lt_com, "version": "1"})

    recs, falhas = recomendacoes.gerar_recomendacoes(aws, [R], V)
    eks_recs = {r["alvo"]: r["tipo"] for r in recs if r["servico"] == "Amazon EKS"}
    assert eks_recs == {
        "prod/sem-lt": "eks_node_group_sem_launch_template",
        "prod/lt-sem-tag": "eks_launch_template_sem_tag",
    }, "o node group com LT já tagueado não deveria gerar recomendação"
    artefato = next(r for r in recs if r["alvo"] == "prod/sem-lt")["artefato"]["conteudo"]
    assert {"Key": "aws-apn-id", "Value": V} in artefato["TagSpecifications"][0]["Tags"]


def test_karpenter_lbc_e_ebs_csi(aws):
    _cluster_eks(aws)
    ec2 = aws.client("ec2")
    ami = ec2.register_image(Name="img-karpenter", RootDeviceName="/dev/sda1")["ImageId"]
    ec2.run_instances(ImageId=ami, MinCount=2, MaxCount=2, TagSpecifications=[{"ResourceType": "instance", "Tags": [
        {"Key": "karpenter.sh/nodepool", "Value": "default"}, {"Key": "kubernetes.io/cluster/prod", "Value": "owned"}]}])
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=1, TagSpecifications=[{"ResourceType": "volume", "Tags": [
        {"Key": "ebs.csi.aws.com/cluster", "Value": "true"}]}])
    vpc = ec2.describe_vpcs()["Vpcs"][0]["VpcId"]
    subs = [ec2.create_subnet(VpcId=vpc, CidrBlock=c, AvailabilityZone=z)["Subnet"]["SubnetId"]
            for c, z in (("172.31.200.0/24", "us-east-1a"), ("172.31.201.0/24", "us-east-1b"))]
    aws.client("elbv2").create_load_balancer(Name="k8s-lb", Subnets=subs, Tags=[{"Key": "elbv2.k8s.aws/cluster", "Value": "prod"}])

    recs, _ = recomendacoes.gerar_recomendacoes(aws, [R], V)
    tipos = _tipos(r for r in recs if r["servico"] == "Amazon EKS")
    assert tipos == ["eks_ebs_csi_sem_tag", "eks_karpenter_sem_tag", "eks_load_balancer_controller_sem_tag"]
    karp = next(r for r in recs if r["tipo"] == "eks_karpenter_sem_tag")
    assert "2 de 2" in karp["situacao"] and "EC2NodeClass" in karp["artefato"]["conteudo"]


def test_ecs_so_recomenda_service_sem_propagate_tags(aws):
    ecs = aws.client("ecs")
    ecs.create_cluster(clusterName="app")
    ecs.register_task_definition(family="web", containerDefinitions=[{"name": "c", "image": "nginx", "memory": 512}])
    ecs.create_service(cluster="app", serviceName="sem", taskDefinition="web", desiredCount=0)
    ecs.create_service(cluster="app", serviceName="com", taskDefinition="web", desiredCount=0, propagateTags="SERVICE")
    recs, _ = recomendacoes.gerar_recomendacoes(aws, [R], V)
    alvos = [r["alvo"] for r in recs if r["tipo"] == "ecs_service_sem_propagate_tags"]
    assert len(alvos) == 1 and alvos[0].endswith("service/app/sem")


def test_conta_vazia_nao_gera_recomendacao_nem_falha_de_eks_ecs(aws):
    recs, falhas = recomendacoes.gerar_recomendacoes(aws, [R], V)
    assert recs == []
    assert not [f for f in falhas if f["etapa"] in ("recomendacoes:eks", "recomendacoes:ecs")]


def test_cli_recomendar_grava_json_e_markdown(aws, tmp_path, monkeypatch):
    ecs = aws.client("ecs")
    ecs.create_cluster(clusterName="app")
    ecs.register_task_definition(family="web", containerDefinitions=[{"name": "c", "image": "nginx", "memory": 512}])
    ecs.create_service(cluster="app", serviceName="sem", taskDefinition="web", desiredCount=0)
    monkeypatch.setattr(regions, "get_active_regions", lambda session, base_region="us-east-1": [R])
    j, md = tmp_path / "r.json", tmp_path / "r.md"
    assert main.run(["recomendar", "--expected-tag-value", V, "--output", str(j), "--markdown", str(md)]) == 0
    assert json.loads(j.read_text())["total"] >= 1
    texto = md.read_text()
    assert "propagateTags" in texto and "PropagateTags: SERVICE" in texto


def test_drs_nao_inicializado_nao_e_falha(monkeypatch):
    """Achado na sandbox (2026-09-30): conta que nunca habilitou o DRS
    devolve UninitializedAccountException — é o estado normal, não falha."""
    from botocore.exceptions import ClientError

    class _Pag:
        def paginate(self):
            raise ClientError({"Error": {"Code": "UninitializedAccountException", "Message": "Account not initialized"}},
                              "DescribeReplicationConfigurationTemplates")

    class _Drs:
        def get_paginator(self, _):
            return _Pag()

    class _Sessao:
        def client(self, *a, **k):
            return _Drs()

    assert recomendacoes._drs(_Sessao(), R, V) == []
    monkeypatch.setattr(recomendacoes, "_COLETORES", (("drs", recomendacoes._drs),))
    recs, falhas = recomendacoes.gerar_recomendacoes(_Sessao(), [R], V)
    assert recs == [] and falhas == []
