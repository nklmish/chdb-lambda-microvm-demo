"""FederationStack (us-west-2) — private Aurora Serverless v2 for the zone-tipping leg.

The agentic MicroVM fleet runs in us-west-2 (the AgentCore VPC is in us-east-1), so
this stack stands up a dedicated small VPC + a private Aurora Serverless v2 Postgres
that the fleet reaches over native TCP via a Lambda MicroVMs *egress network
connector* (created post-deploy by scripts/setup_federation.py — the connector API
is `lambda-core`, not CloudFormation).

Security posture (no public exposure):
  * Aurora `publicly_accessible=False`, in PRIVATE_WITH_EGRESS subnets.
  * Storage encrypted (KMS); `rds.force_ssl=1` so connections must use TLS.
  * DB security group: 5432 inbound ONLY from the egress-connector SG (no CIDR).
  * Master creds in Secrets Manager; the app reads a scoped read-only user from SSM
    /postgres/* (published post-deploy; SecureString can't be created by CFN).
  * Serverless v2 min capacity 0 → auto-pause to $0 compute at rest.

This file publishes String handles to SSM /federation/* for the post-deploy script.
NB: not synth-tested locally (no CDK toolchain in the venv) — `cdk synth NycTaxiFederation`
before deploy.
"""
from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_rds as rds,
    aws_ssm as ssm,
)
from constructs import Construct


class FederationStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # Dedicated VPC in the fleet's region. 1 NAT for the agentic workers'
        # internet egress (Langfuse Cloud, cross-region SSM, Bedrock) when routed
        # through the VPC by the egress connector. S3 via a free gateway endpoint.
        vpc = ec2.Vpc(
            self, "FedVpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24),
                ec2.SubnetConfiguration(
                    name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
            ],
        )
        vpc.add_gateway_endpoint("S3", service=ec2.GatewayVpcEndpointAwsService.S3)

        # SG worn by the egress connector's ENIs — the ONLY source the DB accepts.
        connector_sg = ec2.SecurityGroup(
            self, "MicrovmEgressSg", vpc=vpc,
            description="Lambda MicroVMs egress connector ENIs (agentic fleet)",
            allow_all_outbound=True)

        # DB SG: 5432 inbound ONLY from the connector SG (least privilege, no CIDR).
        db_sg = ec2.SecurityGroup(
            self, "AuroraSg", vpc=vpc,
            description="Aurora Postgres — reachable only from the MicroVM egress connector",
            allow_all_outbound=False)
        db_sg.add_ingress_rule(
            connector_sg, ec2.Port.tcp(5432),
            "chDB postgresql() federation from agentic MicroVMs")

        engine = rds.DatabaseClusterEngine.aurora_postgres(
            version=rds.AuroraPostgresEngineVersion.VER_16_4)

        # Force TLS on every connection (security-team requirement).
        cluster_pg = rds.ParameterGroup(
            self, "AuroraClusterPg", engine=engine,
            parameters={"rds.force_ssl": "1"})

        cluster = rds.DatabaseCluster(
            self, "Aurora",
            engine=engine,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[db_sg],
            writer=rds.ClusterInstance.serverless_v2("Writer", publicly_accessible=False),
            serverless_v2_min_capacity=0,     # auto-pause → $0 compute at rest
            serverless_v2_max_capacity=2,
            default_database_name="nyctaxi",
            credentials=rds.Credentials.from_generated_secret(
                "taxiadmin", secret_name="nyctaxi/federation/aurora-master"),
            parameter_group=cluster_pg,
            storage_encrypted=True,
            enable_data_api=True,             # HTTPS seeding from outside the VPC
            removal_policy=RemovalPolicy.DESTROY,   # demo; set RETAIN for prod
            deletion_protection=False,        # demo
        )

        # Operator role Lambda assumes to create the connector's ENIs in the VPC.
        connector_operator_role = iam.Role(
            self, "ConnectorOperatorRole",
            assumed_by=iam.ServicePrincipal("network-connectors.lambda.amazonaws.com"),
            description="Lets Lambda MicroVMs create ENIs for the egress connector",
            inline_policies={
                "eni": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        sid="CreateENI",
                        actions=["ec2:CreateNetworkInterface"],
                        resources=[
                            "arn:aws:ec2:*:*:network-interface/*",
                            "arn:aws:ec2:*:*:subnet/*",
                            "arn:aws:ec2:*:*:security-group/*",
                        ]),
                    iam.PolicyStatement(
                        sid="TagENI",
                        actions=["ec2:CreateTags"],
                        resources=["arn:aws:ec2:*:*:network-interface/*"],
                        conditions={"StringEquals": {
                            "ec2:ManagedResourceOperator":
                                "network-connectors.lambda.amazonaws.com"}}),
                ]),
            },
        )

        # Handles for scripts/setup_federation.py (create connector, seed, publish
        # /postgres/*). String params only — the SecureString password is written
        # post-deploy by the script (CFN cannot create SecureString params).
        private_subnet_ids = [s.subnet_id for s in vpc.private_subnets]
        params = {
            "VPC_ID": vpc.vpc_id,
            "CONNECTOR_SG_ID": connector_sg.security_group_id,
            "CONNECTOR_OPERATOR_ROLE_ARN": connector_operator_role.role_arn,
            "AURORA_SECRET_ARN": cluster.secret.secret_arn if cluster.secret else "",
            "AURORA_CLUSTER_ARN": (
                f"arn:aws:rds:{self.region}:{self.account}:cluster:"
                f"{cluster.cluster_identifier}"),
            "AURORA_ENDPOINT": cluster.cluster_endpoint.hostname,
        }
        for key, value in params.items():
            ssm.StringParameter(
                self, f"Ssm{key.title().replace('_', '')}",
                parameter_name=f"/federation/{key}", string_value=value)
        ssm.StringListParameter(
            self, "SsmPrivateSubnetIds",
            parameter_name="/federation/PRIVATE_SUBNET_IDS",
            string_list_value=private_subnet_ids)

        CfnOutput(self, "AuroraEndpoint", value=cluster.cluster_endpoint.hostname)
        CfnOutput(self, "ConnectorSgId", value=connector_sg.security_group_id)
        CfnOutput(self, "ConnectorOperatorRoleArn",
                  value=connector_operator_role.role_arn)
