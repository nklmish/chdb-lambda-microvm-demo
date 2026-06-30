"""S3 Files mount infrastructure — native AWS::S3Files resources for NOAA weather data."""
from aws_cdk import Stack, CfnResource, CfnOutput, aws_ec2 as ec2, aws_iam as iam, aws_s3 as s3
from constructs import Construct

class S3FilesStack(Stack):
    def __init__(self, scope: Construct, id: str, vpc: ec2.IVpc,
                 mount_sg: ec2.ISecurityGroup, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── S3 bucket for NOAA weather data (your own copy, not the public bucket) ──
        self.weather_bucket = s3.Bucket(self, "WeatherBucket",
            bucket_name=f"nyc-taxi-noaa-gsod-{self.account}-{self.region}",
            versioned=True,  # Required by S3 Files
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )

        # ── IAM role for S3 Files service to access the bucket ──
        self.s3files_role = iam.Role(self, "S3FilesAccessRole",
            assumed_by=iam.ServicePrincipal(
                "elasticfilesystem.amazonaws.com",
                conditions={
                    "StringEquals": {
                        "aws:SourceAccount": self.account,
                    },
                },
            ),
        )
        self.weather_bucket.grant_read_write(self.s3files_role)
        # S3 Files also needs EventBridge + EC2 networking permissions on the role
        self.s3files_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "s3:GetBucketNotification", "s3:PutBucketNotification",
                "s3:GetBucketVersioning", "s3:GetBucketLocation",
                "s3:GetEncryptionConfiguration",
            ],
            resources=[self.weather_bucket.bucket_arn],
        ))
        self.s3files_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "events:PutRule", "events:DeleteRule", "events:DescribeRule",
                "events:PutTargets", "events:RemoveTargets", "events:ListRules",
            ],
            resources=[f"arn:aws:events:{self.region}:{self.account}:rule/*"],
        ))
        self.s3files_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface",
                "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets",
                "ec2:DescribeSecurityGroups", "ec2:DescribeVpcs",
            ],
            resources=["*"],  # EC2 Describe* actions require Resource: *
        ))

        # ── S3 Files FileSystem (L1 — no L2 construct exists yet) ──
        # AWS::S3Files::FileSystem: creates a file system scoped to the bucket
        self.filesystem = CfnResource(self, "WeatherFs",
            type="AWS::S3Files::FileSystem",
            properties={
                "Bucket": self.weather_bucket.bucket_arn,
                "RoleArn": self.s3files_role.role_arn,
            },
        )

        # ── Mount Target in first private subnet ──
        # AWS::S3Files::MountTarget: creates an NFS endpoint (ENI) in a VPC subnet
        private_subnets = vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
        self.mount_target = CfnResource(self, "WeatherMt",
            type="AWS::S3Files::MountTarget",
            properties={
                "FileSystemId": self.filesystem.get_att("FileSystemId").to_string(),
                "SubnetId": private_subnets.subnet_ids[0],
                "SecurityGroups": [mount_sg.security_group_id],
            },
        )
        self.mount_target.add_dependency(self.filesystem)

        # ── Outputs ──
        self.file_system_id = self.filesystem.get_att("FileSystemId").to_string()
        self.file_system_arn = self.filesystem.get_att("FileSystemArn").to_string()

        CfnOutput(self, "FileSystemId", value=self.file_system_id)
        CfnOutput(self, "MountTargetId",
                  value=self.mount_target.get_att("MountTargetId").to_string())
        CfnOutput(self, "WeatherBucketName", value=self.weather_bucket.bucket_name)
