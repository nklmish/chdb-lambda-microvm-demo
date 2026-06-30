"""CodeBuild project for quarterly image rebuilds + EventBridge trigger."""
from aws_cdk import Stack, Duration, aws_codebuild as cb, aws_ecr as ecr, aws_events as events, aws_events_targets as targets
from constructs import Construct

class CicdStack(Stack):
    def __init__(self, scope: Construct, id: str, ecr_repo: ecr.IRepository, **kwargs):
        super().__init__(scope, id, **kwargs)
        
        project = cb.Project(self, "RebuildProject",
            project_name="nyc-taxi-agent-rebuild",
            environment=cb.BuildEnvironment(
                build_image=cb.LinuxBuildImage.STANDARD_7_0,
                privileged=True,  # Required for container builds
            ),
            build_spec=cb.BuildSpec.from_object({
                "version": "0.2",
                "phases": {
                    "install": {"commands": [
                        # Finch for CodeBuild — install if not pre-installed
                        "which finch || (curl -fsSL https://github.com/runfinch/finch/releases/latest/download/finch-linux-amd64.tar.gz | tar xz -C /usr/local/bin)",
                    ]},
                    "build": {"commands": [
                        "finch build --build-arg DATA_MODE=full -t nyc-taxi-agent .",
                        f"finch tag nyc-taxi-agent {ecr_repo.repository_uri}:latest",
                    ]},
                    "post_build": {"commands": [
                        f"aws ecr get-login-password | finch login --username AWS --password-stdin {ecr_repo.repository_uri}",
                        f"finch push {ecr_repo.repository_uri}:latest",
                    ]},
                },
            }),
        )
        ecr_repo.grant_push(project)
        
        # Quarterly trigger (Jan, Apr, Jul, Oct on 1st at 06:00 UTC)
        events.Rule(self, "QuarterlyRebuild",
            schedule=events.Schedule.cron(month="1,4,7,10", day="1", hour="6", minute="0"),
            targets=[targets.CodeBuildProject(project)],
        )
