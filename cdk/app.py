#!/usr/bin/env python3
import os

import aws_cdk as cdk
from stacks.network_stack import NetworkStack
from stacks.ecr_stack import EcrStack
from stacks.iam_stack import IamStack
from stacks.s3_files_stack import S3FilesStack
from stacks.monitoring_stack import MonitoringStack
from stacks.cicd_stack import CicdStack
from stacks.mount_demo_stack import MountDemoStack

app = cdk.App()
# Account/region come from the deploying credentials (CDK_DEFAULT_*), so this
# app deploys into whatever account `cdk deploy` is run against — no hardcoded
# account. Region defaults to us-east-1 (AgentCore Runtime + Memory + the `us.`
# cross-region inference profile are US-region constructs).
env = cdk.Environment(
    account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    region=os.getenv("CDK_DEFAULT_REGION") or os.getenv("AWS_REGION") or "us-east-1",
)

network = NetworkStack(app, "NycTaxiNetwork", env=env)
ecr = EcrStack(app, "NycTaxiEcr", env=env)
iam = IamStack(app, "NycTaxiIam", vpc=network.vpc, env=env)
s3_files = S3FilesStack(app, "NycTaxiS3Files", vpc=network.vpc,
                         mount_sg=network.efs_sg, env=env)  # efs_sg allows NFS port 2049, same port S3 Files uses
monitoring = MonitoringStack(app, "NycTaxiMonitoring", env=env)
cicd = CicdStack(app, "NycTaxiCicd", ecr_repo=ecr.repository, env=env)

# EC2 host that NFS-mounts the S3 Files filesystem and runs the same container
# image with /mnt/noaa-gsod mounted — the genuine "S3 Files mount" demo that
# AgentCore Runtime cannot provide (its API has no external-filesystem mount).
mount_demo = MountDemoStack(app, "NycTaxiMountDemo", vpc=network.vpc,
                            mount_sg=network.efs_sg,
                            file_system_id=s3_files.file_system_id,
                            ecr_repo=ecr.repository, env=env)
mount_demo.add_dependency(s3_files)
mount_demo.add_dependency(ecr)

app.synth()
