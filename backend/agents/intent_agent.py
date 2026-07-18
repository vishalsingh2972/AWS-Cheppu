"""
Intent Agent – classifies user input into structured AWS intents.
Uses fast local keyword matching; no external API required.
"""
import re
import logging

logger = logging.getLogger(__name__)


def _classify(message: str) -> dict:
    msg = message.lower().strip()

    # ── Extract entities ──────────────────────────────────────────────
    instance_ids = re.findall(r'i-[0-9a-f]{8,17}', message)
    ports = re.findall(r'\b(80|443|22|3389|8080|8443|21|25|3306|5432|6379|27017|\d{4,5})\b', msg)
    port = int(ports[0]) if ports else None

    # ── Write operations ─────────────────────────────────────────────
    if re.search(r'\b(stop|terminate|shut.?down|kill)\b', msg) and instance_ids:
        return {
            "intent": "STOP_EC2", "is_write": True, "risk_level": "HIGH",
            "params": {"instance_ids": instance_ids},
        }

    if re.search(r'\b(start|launch|boot|power.?on|turn.?on)\b', msg) and instance_ids:
        return {
            "intent": "START_EC2", "is_write": True, "risk_level": "MEDIUM",
            "params": {"instance_ids": instance_ids},
        }

    if re.search(r'\b(disable|block|close|remove|revoke|deny)\b', msg) \
            and re.search(r'\bport\b', msg) and port:
        return {
            "intent": "DISABLE_PORT", "is_write": True, "risk_level": "HIGH",
            "params": {"port": port},
        }

    if re.search(r'\b(add|open|allow|enable|create)\b', msg) \
            and re.search(r'\bport\b', msg) and port:
        return {
            "intent": "ADD_SG_RULE", "is_write": True, "risk_level": "MEDIUM",
            "params": {"port": port, "protocol": "tcp", "cidr": "0.0.0.0/0"},
        }

    # ── Read operations ──────────────────────────────────────────────
    if re.search(r'\b(cost|bill|spend|spending|charge|invoice|budget|pricing|expense)\b', msg):
        period = "week" if re.search(r'\b(week|weekly|7.?day)\b', msg) else "month"
        return {
            "intent": "COST_REPORT", "is_write": False, "risk_level": "LOW",
            "params": {"period": period},
        }

    if re.search(r'\b(security.?group|firewall|sg|inbound|outbound|ingress|egress|rule|port)\b', msg):
        return {
            "intent": "LIST_SECURITY_GROUPS", "is_write": False, "risk_level": "LOW",
            "params": {},
        }

    if re.search(r'\b(ebs|volume|disk|storage|block.?store|gib|snapshot)\b', msg):
        f = "unattached" if re.search(r'\b(unattached|unused|detached|available|free)\b', msg) else "all"
        return {
            "intent": "LIST_EBS", "is_write": False, "risk_level": "LOW",
            "params": {"filter": f},
        }

    if re.search(r'\b(ec2|instance|server|vm|virtual.?machine|compute|node)\b', msg):
        if re.search(r'\b(idle|underutil|low.?cpu|unused|wasted|running)\b', msg):
            f = "idle"
        elif re.search(r'\b(stopped?|off|down)\b', msg):
            f = "stopped"
        else:
            f = "all"
        return {
            "intent": "LIST_EC2", "is_write": False, "risk_level": "LOW",
            "params": {"filter": f},
        }

    # ── Generic AWS queries → default to EC2 list ────────────────────
    if re.search(r'\b(show|list|get|fetch|what|how many|status|check)\b', msg):
        return {
            "intent": "LIST_EC2", "is_write": False, "risk_level": "LOW",
            "params": {"filter": "all"},
        }

    return {"intent": "UNKNOWN", "is_write": False, "risk_level": "LOW", "params": {}}


class IntentAgent:
    async def classify(self, message: str) -> dict:
        result = _classify(message)
        logger.info(f"Intent: {result['intent']} | params: {result['params']}")
        return result
