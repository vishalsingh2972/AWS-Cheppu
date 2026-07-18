"""
Security Group Service – list security groups and manage inbound rules.
"""
import boto3
import logging

logger = logging.getLogger(__name__)


class SecurityGroupService:
    def __init__(self):
        self.client = boto3.client("ec2")

    # ──────────────────────────── READ ────────────────────────────

    async def list_security_groups(self, params: dict) -> dict:
        response = self.client.describe_security_groups()
        groups = response["SecurityGroups"]

        if not groups:
            return {"display": "ℹ️  No security groups found.", "voice_summary": "No security groups found."}

        lines = [f"{'Security Groups':─<50}", ""]
        for sg in groups:
            name = sg.get("GroupName", sg["GroupId"])
            lines.append(f"  ▸ {sg['GroupId']}  ({name})")
            lines.append(f"    VPC: {sg.get('VpcId', 'N/A')}")
            lines.append(f"    Description: {sg.get('Description', '—')[:60]}")

            inbound = sg.get("IpPermissions", [])
            if inbound:
                lines.append("    Inbound rules:")
                for rule in inbound[:5]:  # Cap display at 5 rules
                    proto = rule.get("IpProtocol", "-1")
                    from_port = rule.get("FromPort", "*")
                    to_port = rule.get("ToPort", "*")
                    cidrs = [r["CidrIp"] for r in rule.get("IpRanges", [])]
                    cidr_str = ", ".join(cidrs) if cidrs else "N/A"

                    if proto == "-1":
                        lines.append(f"      ALL traffic ← {cidr_str}")
                    elif from_port == to_port:
                        lines.append(f"      {proto.upper()} {from_port} ← {cidr_str}")
                    else:
                        lines.append(f"      {proto.upper()} {from_port}-{to_port} ← {cidr_str}")

                if len(inbound) > 5:
                    lines.append(f"      ... and {len(inbound) - 5} more rules")
            lines.append("")

        count = len(groups)
        display = "\n".join(lines)

        # Build conversational reply
        if count == 1:
            sg = groups[0]
            n_rules = len(sg.get("IpPermissions", []))
            voice = (
                f"You have 1 security group: {sg.get('GroupName', sg['GroupId'])}. "
                f"It has {n_rules} inbound rule{'s' if n_rules != 1 else ''}."
            )
        else:
            names = [g.get("GroupName", g["GroupId"]) for g in groups[:3]]
            voice = (
                f"You have {count} security groups. "
                f"They include: {', '.join(names)}"
                + (f", and {count - 3} more." if count > 3 else ".")
            )
        return {"display": display, "voice_summary": voice}

    # ──────────────────────────── WRITE: DISABLE PORT ────────────────────────────

    async def preview_disable_port(self, params: dict) -> dict:
        port = params.get("port")
        sg_id = params.get("sg_id")

        # If no sg_id given, find security groups with that port open
        matching = await self._find_sgs_with_port(port)

        if not matching:
            return {
                "display": f"ℹ️  No security groups found with port {port} open.",
                "voice_summary": f"No security groups have port {port} open."
            }

        lines = [f"⚠️  DISABLE PORT {port}\n"]
        for sg in matching:
            lines.append(f"  Security Group: {sg['GroupId']} ({sg.get('GroupName', '')})")
            for rule in sg["matching_rules"]:
                cidrs = ", ".join(rule.get("cidrs", []))
                lines.append(f"  Inbound: {cidrs} → TCP {port}")
        lines.append("")
        lines.append("  ⚡ Impact:")
        lines.append("  • Traffic on this port will be blocked immediately")
        lines.append("  • Any services listening on this port become unreachable")

        if port == 80:
            lines.append("  • ⚠️  HTTP web traffic will stop serving")
        elif port == 443:
            lines.append("  • ⚠️  HTTPS web traffic will stop serving")
        elif port == 22:
            lines.append("  • ⚠️  SSH access will be blocked")
        elif port == 3306 or port == 5432:
            lines.append("  • ⚠️  Database connections will be refused")

        lines.append("\n  Do you want to proceed?")

        display = "\n".join(lines)
        voice = (
            f"I found {len(matching)} security group{'s' if len(matching) != 1 else ''} "
            f"with port {port} open. This will block all traffic on port {port}. Do you want to proceed?"
        )
        return {"display": display, "voice_summary": voice, "sg_ids": [s["GroupId"] for s in matching]}

    async def disable_port(self, params: dict) -> dict:
        port = int(params.get("port", 0))
        sg_id = params.get("sg_id")

        matching = await self._find_sgs_with_port(port)
        if sg_id:
            matching = [m for m in matching if m["GroupId"] == sg_id]

        if not matching:
            return {
                "display": f"ℹ️  No matching rules for port {port}.",
                "voice_summary": f"No rules found for port {port}."
            }

        revoked = []
        for sg in matching:
            for rule in sg["matching_rules"]:
                ip_permissions = [{
                    "IpProtocol": "tcp",
                    "FromPort": port,
                    "ToPort": port,
                    "IpRanges": [{"CidrIp": c} for c in rule.get("cidrs", [])],
                }]
                self.client.revoke_security_group_ingress(
                    GroupId=sg["GroupId"],
                    IpPermissions=ip_permissions,
                )
                revoked.append(sg["GroupId"])

        display = (
            f"✅ Port {port} disabled\n\n"
            f"  Revoked from:\n" +
            "\n".join(f"  ▸ {sg_id}" for sg_id in revoked)
        )
        voice = f"Port {port} has been successfully disabled in {len(revoked)} security group{'s' if len(revoked) != 1 else ''}."
        return {"display": display, "voice_summary": voice}

    # ──────────────────────────── WRITE: ADD RULE ────────────────────────────

    async def preview_add_rule(self, params: dict) -> dict:
        port = params.get("port", "?")
        protocol = params.get("protocol", "tcp").upper()
        cidr = params.get("cidr", "0.0.0.0/0")
        sg_id = params.get("sg_id", "not specified")

        display = (
            f"➕ ADD INBOUND RULE\n\n"
            f"  Security Group: {sg_id}\n"
            f"  Rule:  {cidr} → {protocol} {port}\n\n"
            f"  ⚡ Impact:\n"
            f"  • Traffic from {cidr} on {protocol} port {port} will be allowed\n"
        )
        if cidr == "0.0.0.0/0":
            display += "  • ⚠️  This opens the port to the entire internet\n"

        display += "\n  Do you want to proceed?"
        voice = f"You are about to open port {port} to {cidr}. This will allow inbound traffic. Do you want to proceed?"
        return {"display": display, "voice_summary": voice}

    async def add_rule(self, params: dict) -> dict:
        port = int(params.get("port", 0))
        protocol = params.get("protocol", "tcp").lower()
        cidr = params.get("cidr", "0.0.0.0/0")
        sg_id = params.get("sg_id")

        if not sg_id:
            return {"display": "⚠️  Security group ID required.", "voice_summary": "Security group ID required."}

        self.client.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{
                "IpProtocol": protocol,
                "FromPort": port,
                "ToPort": port,
                "IpRanges": [{"CidrIp": cidr}],
            }]
        )
        display = f"✅ Rule added\n\n  {sg_id}: {cidr} → {protocol.upper()} {port}"
        voice = f"Successfully added inbound rule for port {port} to security group {sg_id}."
        return {"display": display, "voice_summary": voice}

    # ──────────────────────────── HELPERS ────────────────────────────

    async def _find_sgs_with_port(self, port: int) -> list[dict]:
        response = self.client.describe_security_groups()
        results = []
        for sg in response["SecurityGroups"]:
            matching_rules = []
            for rule in sg.get("IpPermissions", []):
                proto = rule.get("IpProtocol", "-1")
                from_port = rule.get("FromPort")
                to_port = rule.get("ToPort")
                if proto in ("-1", "tcp") and (
                    from_port is None or (from_port <= port <= (to_port or port))
                ):
                    cidrs = [r["CidrIp"] for r in rule.get("IpRanges", [])]
                    if cidrs:
                        matching_rules.append({"cidrs": cidrs})
            if matching_rules:
                results.append({**sg, "matching_rules": matching_rules})
        return results
