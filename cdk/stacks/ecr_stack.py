"""ECR repository with image scanning and lifecycle rules."""
from aws_cdk import Stack, RemovalPolicy, aws_ecr as ecr
from constructs import Construct

class EcrStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)
        
        self.repository = ecr.Repository(self, "AgentRepo",
            repository_name="nyc-taxi-agent",
            image_scan_on_push=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.repository.add_lifecycle_rule(max_image_count=10, description="Keep last 10 images")
