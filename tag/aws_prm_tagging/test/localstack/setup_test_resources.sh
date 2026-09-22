#!/usr/bin/env bash
# Cria os recursos de teste usados pelo cenário de LocalStack descrito em
# test/localstack/README.md. Requer o LocalStack já rodando (ver run_test.sh)
# e o perfil AWS "localstack" configurado em ~/.aws/config apontando para
# http://localhost.localstack.cloud:4566.
#
# Todos os valores abaixo (região, valor de tag, nomes) são de teste,
# isolados do LocalStack, e não têm relação com nenhuma conta AWS real.
set -euo pipefail

export AWS_PROFILE=localstack
export AWS_DEFAULT_REGION=us-east-1

EXPECTED_TAG_VALUE="pc:test123"
CONFLICTING_TAG_VALUE="pc:outrovalor"

echo "== Resolvendo AMI disponível no LocalStack =="
AMI_ID=$(aws ec2 describe-images --owners amazon --query 'Images[0].ImageId' --output text)
echo "AMI: $AMI_ID"

echo "== Criando instancia EC2 com tag OK (aws-apn-id == valor esperado) =="
INSTANCE_OK=$(aws ec2 run-instances --image-id "$AMI_ID" --instance-type t3.micro --count 1 \
  --tag-specifications "ResourceType=instance,Tags=[{Key=aws-apn-id,Value=$EXPECTED_TAG_VALUE},{Key=Name,Value=inst-ok}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "instancia OK: $INSTANCE_OK"

echo "== Criando instancia EC2 SEM a tag aws-apn-id =="
INSTANCE_SEM_TAG=$(aws ec2 run-instances --image-id "$AMI_ID" --instance-type t3.micro --count 1 \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=inst-sem-tag}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "instancia sem tag: $INSTANCE_SEM_TAG"

echo "== Criando instancia EC2 com tag CONFLITANTE + sinal de CloudFormation =="
INSTANCE_CONFLITO=$(aws ec2 run-instances --image-id "$AMI_ID" --instance-type t3.micro --count 1 \
  --tag-specifications "ResourceType=instance,Tags=[{Key=aws-apn-id,Value=$CONFLICTING_TAG_VALUE},{Key=Name,Value=inst-conflito},{Key=aws:cloudformation:stack-name,Value=minha-stack}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "instancia conflito: $INSTANCE_CONFLITO"

echo "== Criando instancia EC2 SEM aws-apn-id mas com indicio de Terraform (catch-all por valor, chave arbitraria) =="
INSTANCE_TERRAFORM=$(aws ec2 run-instances --image-id "$AMI_ID" --instance-type t3.micro --count 1 \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=inst-terraform},{Key=Provisioner,Value=Terraform}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "instancia terraform_heuristico: $INSTANCE_TERRAFORM"

echo "== Criando bucket S3 com tag OK =="
aws s3api create-bucket --bucket prm-test-bucket-ok >/dev/null
aws s3api put-bucket-tagging --bucket prm-test-bucket-ok \
  --tagging "TagSet=[{Key=aws-apn-id,Value=$EXPECTED_TAG_VALUE}]"
echo "bucket: prm-test-bucket-ok"

echo "== Habilitando AWS Organizations (conta atual vira a conta de gerenciamento) =="
aws organizations create-organization --feature-set ALL >/dev/null 2>&1 || true
ROOT_ID=$(aws organizations list-roots --query 'Roots[0].Id' --output text)
echo "root: $ROOT_ID"

OU_ID=$(aws organizations create-organizational-unit --parent-id "$ROOT_ID" --name "Producao" \
  --query 'OrganizationalUnit.Id' --output text)
echo "OU: $OU_ID"

ACCOUNT_ID=$(aws organizations create-account --account-name "Cliente Teste" \
  --email cliente-teste@example.com --query 'CreateAccountStatus.AccountId' --output text)
echo "conta filha criada: $ACCOUNT_ID"

aws organizations move-account --account-id "$ACCOUNT_ID" \
  --source-parent-id "$ROOT_ID" --destination-parent-id "$OU_ID"
echo "conta $ACCOUNT_ID movida para a OU $OU_ID"

echo "== Criando VPC/Subnet para o cluster EKS =="
VPC_ID=$(aws ec2 create-vpc --cidr-block 10.0.0.0/16 --query 'Vpc.VpcId' --output text)
SUBNET_ID=$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block 10.0.1.0/24 --query 'Subnet.SubnetId' --output text)
echo "vpc: $VPC_ID / subnet: $SUBNET_ID"

echo "== Criando cluster EKS com tag OK =="
aws eks create-cluster --name prm-test-cluster \
  --role-arn arn:aws:iam::000000000000:role/eks-role \
  --resources-vpc-config subnetIds="$SUBNET_ID" \
  --tags aws-apn-id="$EXPECTED_TAG_VALUE" >/dev/null

echo "aguardando cluster ficar ACTIVE..."
for _ in $(seq 1 30); do
  status=$(aws eks describe-cluster --name prm-test-cluster --query 'cluster.status' --output text)
  if [ "$status" = "ACTIVE" ]; then
    break
  fi
  sleep 2
done
echo "cluster status: $status"

echo "== Criando node group com tag OK =="
aws eks create-nodegroup --cluster-name prm-test-cluster --nodegroup-name prm-test-ng \
  --node-role arn:aws:iam::000000000000:role/eks-node-role \
  --subnets "$SUBNET_ID" \
  --tags aws-apn-id="$EXPECTED_TAG_VALUE" >/dev/null

echo
echo "== Recursos de teste criados com sucesso =="
echo "instancia OK          : $INSTANCE_OK"
echo "instancia sem tag      : $INSTANCE_SEM_TAG"
echo "instancia conflito     : $INSTANCE_CONFLITO"
echo "instancia terraform    : $INSTANCE_TERRAFORM"
echo "bucket S3              : prm-test-bucket-ok"
echo "OU                     : $OU_ID (Producao)"
echo "conta filha            : $ACCOUNT_ID (Cliente Teste)"
echo "cluster EKS            : prm-test-cluster"
echo "node group EKS         : prm-test-ng"
