"""IAM execution role with least-privilege permissions."""
from aws_cdk import Stack, CfnOutput, aws_iam as iam, aws_ec2 as ec2, aws_ssm as ssm
from constructs import Construct

class IamStack(Stack):
    def __init__(self, scope: Construct, id: str, vpc: ec2.IVpc, **kwargs):
        super().__init__(scope, id, **kwargs)

        # Defense against iam:*/organizations:*/account:* escalation comes from
        # DefaultPolicy not granting those actions — no permissions boundary needed.
        # (Pre- had a deny-only permissions boundary which was redundant with
        # the role's scoped grants and, due to boundaries being allow-listed MAX
        # filters, actually collapsed the role's effective permissions to empty.
        # See CAND-GATE14-RUNTIME-502-DIAGNOSTIC for the diagnostic trail.)
        self.execution_role = iam.Role(self, "AgentExecutionRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )

        # Bedrock Invoke — AgentCoreBedrockInvoke Sid
        # Supersedes legacy no-Sid bedrock:InvokeModel on foundation-model/anthropic.claude-*
        # which was region-locked to self.region only and missing InvokeModelWithResponseStream.
        # Covers: inference-profile ARN (cross-region profile) + all 3 backing foundation-model
        # ARNs (us-east-1/us-east-2/us-west-2) — IAM evaluates both profile + backing model ARN
        # when cross-region profile routes. Family-complete: streaming (converse_stream →
        # InvokeModelWithResponseStream) + non-streaming fallback (converse → InvokeModel).
        self.execution_role.add_to_policy(iam.PolicyStatement(
            sid="AgentCoreBedrockInvoke",
            effect=iam.Effect.ALLOW,
            actions=[
                "bedrock:InvokeModelWithResponseStream",
                "bedrock:InvokeModel",
            ],
            resources=[
                # Inference-profile ARN must be in the DEPLOY region — the cross-region
                # profile is invoked via the caller's region (self.region resolves to
                # us-east-1 for the existing prod deploy, so this is backward-compatible).
                f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/us.anthropic.claude-sonnet-4-20250514-v1:0",
                # The 3 backing foundation-model regions the `us.` profile routes to.
                "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-20250514-v1:0",
                "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-sonnet-4-20250514-v1:0",
                "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-sonnet-4-20250514-v1:0",
            ],
        ))

        # S3 read for NOAA weather data bucket (your own copy)
        self.execution_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:ListBucket"],
            resources=[
                f"arn:aws:s3:::nyc-taxi-noaa-gsod-{self.account}-{self.region}",
                f"arn:aws:s3:::nyc-taxi-noaa-gsod-{self.account}-{self.region}/*",
            ],
        ))

        # S3 Files mount — container needs NFS client access to the mount target
        # S3 Files uses NFS (port 2049) via ENI, so the container needs:
        # 1. Network access to mount target ENI (handled by security groups in NetworkStack)
        # 2. S3 Files DescribeFileSystem/DescribeMountTarget for mount resolution
        self.execution_role.add_to_policy(iam.PolicyStatement(
            actions=["s3files:DescribeFileSystems", "s3files:DescribeMountTargets"],
            resources=[f"arn:aws:s3files:{self.region}:{self.account}:file-system/*"],
        ))

        # ECR pull
        self.execution_role.add_to_policy(iam.PolicyStatement(
            actions=["ecr:GetAuthorizationToken", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
            resources=["*"],  # ecr:GetAuthorizationToken requires *
        ))

        # CloudWatch Logs
        self.execution_role.add_to_policy(iam.PolicyStatement(
            actions=["logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/nyc-taxi-agent/*"],
        ))

        # AgentCore canonical log-groups — Allows AgentCore's ADOT forwarder to
        # create/write to its canonical log-group pattern. A'-2 CONFIRMED:
        # IAM sim only covered the CDK-created /nyc-taxi-agent/runtime group,
        # not AgentCore's auto-created /aws/bedrock-agentcore/runtimes/<rid>-<env>/DEFAULT path.
        self.execution_role.add_to_policy(iam.PolicyStatement(
            sid="AgentCoreCanonicalLogGroups",
            effect=iam.Effect.ALLOW,
            actions=[
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "logs:DescribeLogStreams",
            ],
            resources=[
                f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/*",
                f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/*:*",
            ],
        ))

        # CloudWatch Logs list-level action — logs:DescribeLogGroups is a List-level
        # action with NO resource type per AWS Service Authorization Reference; it
        # operates account-wide, not log-group-scoped. Scoping it to an ARN prefix
        # yields IAM semantic no-match (implicitDeny). Required by
        # aws-opentelemetry-distro auto-instrumentation per AWS CloudWatch-OTLP-UsingADOT.html.
        # B.6 split-statement fix (D-D12-IAM-LIST-LEVEL-RESOURCE-STAR-SEMANTICS).
        self.execution_role.add_to_policy(iam.PolicyStatement(
            sid="AgentCoreLogsListLevel",
            effect=iam.Effect.ALLOW,
            actions=["logs:DescribeLogGroups"],
            resources=["*"],
        ))

        # AgentCore Memory data-plane extension — 4 actions missing from original grant,
        # exposed by FINDING B (bedrock-agentcore:ListEvents AccessDeniedException
        # on AgentExecutionRole at per-invoke session-manager initialization).
        # All 4 are ARN-scopable (SAR resource type = memory*; IAM sim confirmed
        # implicitDeny-on-both-scopes = ungranted, NOT List-level-requires-star).
        # B.1 append-only; existing Memory statement untouched.
        self.execution_role.add_to_policy(iam.PolicyStatement(
            sid="AgentCoreMemoryDataPlaneExtension",
            effect=iam.Effect.ALLOW,
            actions=[
                "bedrock-agentcore:ListEvents",
                "bedrock-agentcore:GetEvent",
                "bedrock-agentcore:ListSessions",
                "bedrock-agentcore:ListActors",
            ],
            resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:memory/*"],
        ))

        # AgentCore Memory (scoped to memory resources)
        self.execution_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:CreateEvent", "bedrock-agentcore:RetrieveMemoryRecords",
                     "bedrock-agentcore:CreateMemory", "bedrock-agentcore:GetMemory",
                     "bedrock-agentcore:UpdateMemory", "bedrock-agentcore:ListMemoryRecords"],
            resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:memory/*"],
        ))

        # Dedicated memory execution role — consumed by scripts/create_memory.py:14
        # Trust principal is bedrock-agentcore.amazonaws.com (NOT bedrock), distinct from
        # the container runtime's AgentExecutionRole. Explicit role_name required for the
        # hardcoded ARN in create_memory.py; CfnOutput provides defense-in-depth lookup.
        # Defense against iam:*/organizations:*/account:* escalation comes from
        # inline-policy scope (MemoryOps grants only bedrock-agentcore:* on memory/*
        # and logs:* on /aws/bedrock-agentcore/memory/*) — no permissions boundary needed.
        # IAM roles are account-GLOBAL, so a fixed name collides when this stack
        # is deployed to a second region in the same account. Suffix the name with
        # the region for everything except us-east-1, which keeps the original
        # name so existing us-east-1 deployments are NOT renamed/replaced.
        mem_role_name = (
            "NycTaxiAgentMemoryRole" if self.region == "us-east-1"
            else f"NycTaxiAgentMemoryRole-{self.region}"
        )
        self.memory_execution_role = iam.Role(
            self,
            "MemoryExecutionRole",
            role_name=mem_role_name,
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            inline_policies={
                "MemoryOps": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            effect=iam.Effect.ALLOW,
                            actions=[
                                "bedrock-agentcore:CreateMemory",
                                "bedrock-agentcore:GetMemory",
                                "bedrock-agentcore:UpdateMemory",
                                "bedrock-agentcore:DeleteMemory",
                                "bedrock-agentcore:ListMemories",
                            ],
                            resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:memory/*"],
                        ),
                        iam.PolicyStatement(
                            effect=iam.Effect.ALLOW,
                            actions=[
                                "logs:CreateLogGroup",
                                "logs:CreateLogStream",
                                "logs:PutLogEvents",
                            ],
                            resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/memory/*"],
                        ),
                    ],
                ),
            },
        )

        CfnOutput(
            self, "MemoryExecutionRoleArn",
            value=self.memory_execution_role.role_arn,
            export_name="NycTaxiIam-MemoryExecutionRoleArn",
        )

        CfnOutput(
            self, "AgentExecutionRoleArn",
            value=self.execution_role.role_arn,
            export_name="NycTaxiIam-AgentExecutionRoleArn",
        )

        ssm.StringParameter(
            self, "AgentExecutionRoleArnSsm",
            parameter_name="/agentcore/AGENT_EXECUTION_ROLE_ARN",
            string_value=self.execution_role.role_arn,
            description="AgentCore Runtime execution role ARN",
        )
