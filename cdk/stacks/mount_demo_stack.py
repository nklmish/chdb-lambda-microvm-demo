"""EC2 host that NFS-mounts the S3 Files filesystem and runs the agent image.

This is the genuine "S3 Files mount" demo. AgentCore Runtime's CreateAgentRuntime
API has no field to mount an external EFS / S3 Files filesystem (only ephemeral
sessionStorage), so the deployed runtime falls back to direct s3() reads for
weather. This stack instead runs the *same* container image on a plain EC2 host
that mounts the S3 Files filesystem at /mnt/noaa-gsod — so weather_tools.py takes
the `file('/mnt/noaa-gsod/...')` branch and the mount is actually exercised.

Access is via SSM Session Manager (no public IP, no SSH, no inbound ports).

NOTE: the user-data below (package names, NFS mount options, IMDS hop limit) is
synth-verified but validated end-to-end only on a real deploy — expect to tweak
mount options against the current S3 Files mount docs if the mount hangs.
"""
from aws_cdk import (
    Stack,
    CfnOutput,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_ecr as ecr,
)
from constructs import Construct

BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"


class MountDemoStack(Stack):
    def __init__(self, scope: Construct, id: str, vpc: ec2.IVpc,
                 mount_sg: ec2.ISecurityGroup, file_system_id: str,
                 ecr_repo: ecr.IRepository, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── Instance role: SSM access + ECR pull + Bedrock + AgentCore Memory + SSM params ──
        role = iam.Role(self, "MountDemoInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
                # Required for `mount -t s3files` — grants s3files:ClientMount (TLS+IAM mount).
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonS3FilesClientReadOnlyAccess"),
            ],
        )
        # Direct S3 read on the weather bucket enables S3 Files "intelligent read
        # routing" (large reads served straight from S3). Without it reads still
        # work via the file system, just not the optimized path.
        weather_bucket_arn = f"arn:aws:s3:::nyc-taxi-noaa-gsod-{self.account}-{self.region}"
        role.add_to_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:GetObjectVersion"],
            resources=[f"{weather_bucket_arn}/*"],
        ))
        # Bedrock invoke — same ARNs as the AgentCore execution role (cross-region profile + 3 backings)
        role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                # Inference-profile ARN in the deploy region (cross-region profile is
                # invoked via the caller's region).
                f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{BEDROCK_MODEL_ID}",
                "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-20250514-v1:0",
                "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-sonnet-4-20250514-v1:0",
                "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-sonnet-4-20250514-v1:0",
            ],
        ))
        # AgentCore Memory data-plane (same set the agent runtime role gets)
        role.add_to_policy(iam.PolicyStatement(
            actions=[
                "bedrock-agentcore:CreateEvent", "bedrock-agentcore:RetrieveMemoryRecords",
                "bedrock-agentcore:GetMemory", "bedrock-agentcore:ListMemoryRecords",
                "bedrock-agentcore:ListEvents", "bedrock-agentcore:GetEvent",
                "bedrock-agentcore:ListSessions", "bedrock-agentcore:ListActors",
            ],
            resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:memory/*"],
        ))
        # Read deploy config from SSM (memory id + Langfuse creds)
        role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters"],
            resources=[
                f"arn:aws:ssm:{self.region}:{self.account}:parameter/agentcore/*",
                f"arn:aws:ssm:{self.region}:{self.account}:parameter/langfuse/*",
            ],
        ))
        ecr_repo.grant_pull(role)  # also adds the ecr:GetAuthorizationToken on *

        # ── Security group: egress only (SSM + NAT). NFS reachability comes from
        #    mount_sg's 2049 ingress from the VPC CIDR, so no extra ingress here. ──
        sg = ec2.SecurityGroup(self, "MountDemoSg", vpc=vpc,
                               description="NYC Taxi mount-demo EC2", allow_all_outbound=True)

        # ── User data: install docker + nfs, mount S3 Files, run the agent image ──
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "set -xeuo pipefail",
            "exec > >(tee /var/log/mount-demo-userdata.log) 2>&1",
            "dnf install -y docker amazon-efs-utils python3-pip",
            # The S3 Files mount helper (mount -t s3files) needs amazon-efs-utils >= 3.0.0
            # and botocore. AL2023's packaged version may be older — install efs-utils
            # from source if the s3files helper is missing.
            "python3 -m pip install -q botocore || true",
            'if ! grep -q s3files /sbin/mount.efs 2>/dev/null && [ ! -e /sbin/mount.s3files ]; then '
            'dnf install -y git rust cargo make rpm-build openssl-devel && '
            'git clone https://github.com/aws/efs-utils /tmp/efs-utils && '
            'cd /tmp/efs-utils && make rpm && dnf install -y build/amazon-efs-utils*.rpm && cd /; fi',
            "command -v aws || dnf install -y awscli",
            "systemctl enable --now docker",
            'TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 300")',
            'REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)',
            f'FS_ID="{file_system_id}"',
            'ACCOUNT=$(aws sts get-caller-identity --query Account --output text)',
            # Mount via the S3 Files helper — does TLS + IAM auth and resolves the
            # mount target from the file-system id (must be in the instance's AZ).
            "mkdir -p /mnt/noaa-gsod",
            'for i in 1 2 3 4 5; do mount -t s3files "$FS_ID":/ /mnt/noaa-gsod && break || sleep 15; done',
            "mount | grep noaa-gsod || echo 'WARNING: S3 Files mount not present'",
            # Fetch deploy config
            'MEM_ID=$(aws ssm get-parameter --region "$REGION" --name /agentcore/AGENTCORE_MEMORY_ID --query Parameter.Value --output text)',
            'LF_HOST=$(aws ssm get-parameter --region "$REGION" --name /langfuse/LANGFUSE_HOST --query Parameter.Value --output text)',
            'LF_PK=$(aws ssm get-parameter --region "$REGION" --name /langfuse/LANGFUSE_PUBLIC_KEY --query Parameter.Value --output text)',
            'LF_SK=$(aws ssm get-parameter --region "$REGION" --name /langfuse/LANGFUSE_SECRET_KEY --with-decryption --query Parameter.Value --output text)',
            'OTEL_HEADERS="Authorization=Basic $(printf "%s:%s" "$LF_PK" "$LF_SK" | base64 | tr -d "\\n"),x-langfuse-ingestion-version=4"',
            # Pull + run the agent image with the mount bind-mounted in
            'aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"',
            f'IMAGE="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/{ecr_repo.repository_name}:latest"',
            'docker pull "$IMAGE"',
            "docker run -d --name nyc-taxi-agent --restart unless-stopped -p 8080:8080 "
            "-v /mnt/noaa-gsod:/mnt/noaa-gsod "
            '-e AWS_REGION="$REGION" '
            f'-e BEDROCK_MODEL_ID="{BEDROCK_MODEL_ID}" '
            "-e IS_PROD=true -e LANGFUSE_TRACING_ENVIRONMENT=PRD -e DISABLE_ADOT_OBSERVABILITY=true "
            "-e WEATHER_MOUNT_PATH=/mnt/noaa-gsod "
            '-e AGENTCORE_MEMORY_ID="$MEM_ID" '
            '-e OTEL_EXPORTER_OTLP_ENDPOINT="$LF_HOST/api/public/otel" '
            '-e OTEL_EXPORTER_OTLP_HEADERS="$OTEL_HEADERS" '
            '"$IMAGE"',
        )

        instance = ec2.Instance(self, "MountDemoInstance",
            vpc=vpc,
            # Must be in the mount target's AZ. S3FilesStack creates the mount target
            # in the first private subnet, so pin this instance to that same AZ.
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                availability_zones=[vpc.availability_zones[0]],
            ),
            # Graviton/arm64 — the container image is built for linux/arm64 (AgentCore
            # runs it on Graviton). An x86_64 instance fails with "exec format error".
            # 32GB RAM (t4g.2xlarge): query_with_fresh_data materialises the baked side
            # into a pandas DataFrame (baked_ds.to_df()); with the baked table at ~90M
            # rows (2024+2025) a loosely-filtered fresh query can build a ~10-13GB frame,
            # which OOMs smaller instances (t4g.small/2GB wedged the host + SSM agent).
            instance_type=ec2.InstanceType("t4g.2xlarge"),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(
                cpu_type=ec2.AmazonLinuxCpuType.ARM_64
            ),
            role=role,
            security_group=sg,
            user_data=user_data,
            require_imdsv2=True,
            # The default AL2023 root volume (~8GB) is too small once the ~1.8GB
            # agent image is pulled alongside any previous image during a redeploy
            # (docker keeps the old image until pruned) — the disk fills, the pull
            # fails ("no space left on device") and the SSM agent wedges. 30GB gives
            # comfortable room for an image swap (old + new) plus the OS.
            block_devices=[ec2.BlockDevice(
                device_name="/dev/xvda",
                volume=ec2.BlockDeviceVolume.ebs(30, volume_type=ec2.EbsDeviceVolumeType.GP3),
            )],
        )
        # Containers in docker's bridge network need IMDS hop limit ≥ 2 to reach
        # the instance role credentials (default hop limit 1 blocks them).
        instance.instance.add_property_override(
            "MetadataOptions", {"HttpTokens": "required", "HttpPutResponseHopLimit": 2}
        )

        CfnOutput(self, "MountDemoInstanceId", value=instance.instance_id)
        CfnOutput(self, "ConnectCommand",
                  value=f"aws ssm start-session --target {instance.instance_id} --region {self.region}")
