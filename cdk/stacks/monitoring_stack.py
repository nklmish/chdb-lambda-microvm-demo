"""CloudWatch log group, metric alarms, and SNS alerts."""
from aws_cdk import Stack, Duration, aws_logs as logs, aws_cloudwatch as cw, aws_cloudwatch_actions as cw_actions, aws_sns as sns
from constructs import Construct

class MonitoringStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)
        
        self.log_group = logs.LogGroup(self, "AgentLogs",
            log_group_name="/nyc-taxi-agent/runtime",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        
        self.alert_topic = sns.Topic(self, "AlertTopic", display_name="NYC Taxi Agent Alerts")
        
        # Error rate alarm (>5% over 5 minutes)
        error_metric = self.log_group.add_metric_filter("ErrorCount",
            filter_pattern=logs.FilterPattern.literal("ERROR"),
            metric_name="ErrorCount", metric_namespace="NycTaxiAgent",
        )
        cw.Alarm(self, "ErrorRateAlarm",
            metric=error_metric.metric(period=Duration.minutes(5), statistic="Sum"),
            threshold=5, evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        ).add_alarm_action(cw_actions.SnsAction(self.alert_topic))
