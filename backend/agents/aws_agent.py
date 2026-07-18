"""
AWS Agent – routes classified intents to the appropriate AWS service module.
Handles both preview (dry-run) and execution modes.
"""
import logging
from aws.ec2 import EC2Service
from aws.security_group import SecurityGroupService
from aws.cost import CostService

logger = logging.getLogger(__name__)


class AWSAgent:
    def __init__(self):
        self.ec2 = EC2Service()
        self.sg = SecurityGroupService()
        self.cost = CostService()

    async def execute(self, intent: str, params: dict) -> dict:
        """Execute an intent and return display + voice_summary."""
        handlers = {
            "LIST_EC2":             self.ec2.list_instances,
            "LIST_EBS":             self.ec2.list_ebs_volumes,
            "LIST_SECURITY_GROUPS": self.sg.list_security_groups,
            "COST_REPORT":          self.cost.get_cost_report,
            "STOP_EC2":             self.ec2.stop_instances,
            "START_EC2":            self.ec2.start_instances,
            "DISABLE_PORT":         self.sg.disable_port,
            "ADD_SG_RULE":          self.sg.add_rule,
        }
        handler = handlers.get(intent)
        if not handler:
            return {
                "display": f"⚠️ Unknown intent: {intent}",
                "voice_summary": "Unknown operation."
            }
        try:
            return await handler(params)
        except Exception as e:
            logger.error(f"AWS execution error [{intent}]: {e}")
            return {
                "display": f"❌ AWS Error: {str(e)}",
                "voice_summary": f"An AWS error occurred: {str(e)}"
            }

    async def preview(self, intent: str, params: dict) -> dict:
        """Generate a preview/confirmation message for write operations."""
        previews = {
            "STOP_EC2":     self.ec2.preview_stop,
            "START_EC2":    self.ec2.preview_start,
            "DISABLE_PORT": self.sg.preview_disable_port,
            "ADD_SG_RULE":  self.sg.preview_add_rule,
        }
        handler = previews.get(intent)
        if not handler:
            return {
                "display": f"Confirm: execute {intent}?",
                "voice_summary": f"Please confirm the {intent} operation."
            }
        try:
            return await handler(params)
        except Exception as e:
            logger.error(f"Preview error [{intent}]: {e}")
            return {
                "display": f"⚠️ Could not generate preview: {e}\n\nProceed anyway?",
                "voice_summary": "Could not generate a preview. Do you want to proceed?"
            }
