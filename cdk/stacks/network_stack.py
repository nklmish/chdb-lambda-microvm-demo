"""VPC with private subnets and VPC endpoints for all required AWS services."""
from aws_cdk import Stack, CfnOutput, aws_ec2 as ec2, aws_ssm as ssm
from constructs import Construct

class NetworkStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        self.vpc = ec2.Vpc(self, "AgentVpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="Private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=24),
                ec2.SubnetConfiguration(name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
            ],
        )

        # Security group for VPC interface endpoints (allow HTTPS from VPC)
        self.endpoint_sg = ec2.SecurityGroup(self, "EndpointSg", vpc=self.vpc, description="VPC Endpoints")
        self.endpoint_sg.add_ingress_rule(ec2.Peer.ipv4(self.vpc.vpc_cidr_block), ec2.Port.tcp(443))

        # Security group for EFS/S3 Files mount targets (allow NFS from VPC)
        self.efs_sg = ec2.SecurityGroup(self, "EfsSg", vpc=self.vpc, description="EFS/S3 Files mounts")
        self.efs_sg.add_ingress_rule(ec2.Peer.ipv4(self.vpc.vpc_cidr_block), ec2.Port.tcp(2049))

        # Interface endpoints
        for svc_name, svc in [
            ("BedrockRuntime", ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME),
            ("EcrApi", ec2.InterfaceVpcEndpointAwsService.ECR),
            ("EcrDkr", ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER),
            ("Logs", ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS),
            ("Monitoring", ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_MONITORING),
            ("Efs", ec2.InterfaceVpcEndpointAwsService.ELASTIC_FILESYSTEM),
        ]:
            self.vpc.add_interface_endpoint(svc_name, service=svc, security_groups=[self.endpoint_sg])

        # Gateway endpoint for S3
        self.vpc.add_gateway_endpoint("S3", service=ec2.GatewayVpcEndpointAwsService.S3)

        # publish cross-stack handles via SSM for scripts/create_runtime.py
        # Mirrors the 029a /agentcore/AGENT_EXECUTION_ROLE_ARN pattern (String-only).
        # Subnets use StringListParameter — boto3 ssm:GetParameter returns the value as a
        # comma-joined string for StringList-typed parameters; consumers split on ",".
        ssm.StringParameter(
            self, "EfsSgIdSsm",
            parameter_name="/nyctaxi/network/EFS_SG_ID",
            string_value=self.efs_sg.security_group_id,
            description="EFS SG ID",
        )
        ssm.StringListParameter(
            self, "PrivateSubnetIdsSsm",
            parameter_name="/nyctaxi/network/PRIVATE_SUBNET_IDS",
            string_list_value=[s.subnet_id for s in self.vpc.private_subnets],
            description="Private subnet IDs",
        )
        CfnOutput(
            self, "EfsSgId",
            value=self.efs_sg.security_group_id,
            export_name="NycTaxiNetwork-EfsSgId",
        )
        CfnOutput(
            self, "PrivateSubnetIds",
            value=",".join([s.subnet_id for s in self.vpc.private_subnets]),
            export_name="NycTaxiNetwork-PrivateSubnetIds",
        )
