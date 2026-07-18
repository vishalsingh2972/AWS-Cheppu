"""
EC2 Service – handles EC2 instance and EBS volume operations via boto3.
"""
import boto3
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class EC2Service:
    def __init__(self):
        self.client = boto3.client("ec2")

    # ──────────────────────────── READ ────────────────────────────

    async def list_instances(self, params: dict) -> dict:
        filter_type = params.get("filter", "idle").lower()

        filters = []
        if filter_type in ("idle", "running"):
            filters = [{"Name": "instance-state-name", "Values": ["running"]}]
        elif filter_type == "stopped":
            filters = [{"Name": "instance-state-name", "Values": ["stopped"]}]
        # filter_type == "all" → no filter, returns every state

        response = self.client.describe_instances(Filters=filters)

        instances = []
        for reservation in response["Reservations"]:
            for inst in reservation["Instances"]:
                name = next(
                    (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"),
                    inst["InstanceId"]
                )
                # Get CPU utilization from CloudWatch for idle detection
                cpu = self._get_avg_cpu(inst["InstanceId"]) if filter_type == "idle" else None

                instance_data = {
                    "id": inst["InstanceId"],
                    "name": name,
                    "type": inst["InstanceType"],
                    "state": inst["State"]["Name"],
                    "az": inst["Placement"]["AvailabilityZone"],
                    "cpu": cpu,
                    "launch_time": inst.get("LaunchTime", "").isoformat() if inst.get("LaunchTime") else "N/A",
                }
                if filter_type == "idle" and cpu is not None and cpu > 10:
                    continue  # Skip non-idle
                instances.append(instance_data)

        if not instances:
            label = f"{filter_type} " if filter_type != "all" else ""
            msg = f"You don't have any {label}EC2 instances in your account right now."
            return {"display": f"ℹ️  {msg}", "voice_summary": msg}

        count = len(instances)
        total_savings = 0

        # Build structured display
        lines = [f"{'EC2 Instances':─<50}", ""]
        for i in instances:
            cpu_str = f"  CPU avg: {i['cpu']:.1f}%" if i["cpu"] is not None else ""
            lines.append(f"  ▸ {i['id']}")
            lines.append(f"    Name:  {i['name']}")
            lines.append(f"    Type:  {i['type']}  |  State: {i['state']}")
            lines.append(f"    AZ:    {i['az']}")
            if cpu_str:
                lines.append(f"    {cpu_str.strip()}")
            lines.append("")
            total_savings += self._estimate_monthly_cost(i["type"])

        if filter_type == "idle" and total_savings > 0:
            lines.append(f"💰 Potential savings: ~${total_savings:.0f}/month")
        display = "\n".join(lines)

        # Build conversational reply
        label = "idle " if filter_type == "idle" else ("stopped " if filter_type == "stopped" else "")
        if count == 1:
            i = instances[0]
            voice = (
                f"You have 1 {label}EC2 instance. "
                f"It's named {i['name']}, a {i['type']} currently {i['state']} in {i['az']}."
            )
        elif count <= 4:
            parts = [
                f"{i['name']} — a {i['type']}, {i['state']} in {i['az']}"
                for i in instances
            ]
            voice = (
                f"You have {count} {label}EC2 instances. "
                + " | ".join(parts) + "."
            )
        else:
            parts = [
                f"{i['name']} ({i['type']}, {i['state']})"
                for i in instances[:3]
            ]
            voice = (
                f"You have {count} {label}EC2 instances. "
                f"The first few are: {', '.join(parts)}, and {count - 3} more."
            )

        if filter_type == "idle" and total_savings > 0:
            voice += f" Stopping idle instances could save around ${total_savings:.0f} per month."

        return {"display": display, "voice_summary": voice}

    async def list_ebs_volumes(self, params: dict) -> dict:
        filter_type = params.get("filter", "unattached").lower()

        filters = []
        if filter_type == "unattached":
            filters = [{"Name": "status", "Values": ["available"]}]

        response = self.client.describe_volumes(Filters=filters)
        volumes = response["Volumes"]

        if not volumes:
            label = "unattached " if filter_type == "unattached" else ""
            msg = f"You don't have any {label}EBS volumes right now."
            return {"display": f"ℹ️  {msg}", "voice_summary": msg}

        lines = [f"{'EBS Volumes':─<50}", ""]
        total_gb = 0
        for vol in volumes:
            name = next((t["Value"] for t in vol.get("Tags", []) if t["Key"] == "Name"), "—")
            lines.append(f"  ▸ {vol['VolumeId']}")
            lines.append(f"    Name:  {name}")
            lines.append(f"    Size:  {vol['Size']} GiB  |  Type: {vol['VolumeType']}")
            lines.append(f"    State: {vol['State']}")
            lines.append(f"    AZ:    {vol['AvailabilityZone']}")
            lines.append("")
            total_gb += vol["Size"]

        monthly_cost = total_gb * 0.08
        lines.append(f"💰 Total: {total_gb} GiB  (~${monthly_cost:.0f}/month wasted on unattached)")
        display = "\n".join(lines)

        count = len(volumes)
        label = "unattached " if filter_type == "unattached" else ""
        if count == 1:
            vol = volumes[0]
            name = next((t["Value"] for t in vol.get("Tags", []) if t["Key"] == "Name"), vol["VolumeId"])
            voice = (
                f"You have 1 {label}EBS volume: {name}, "
                f"{vol['Size']} gigabytes of type {vol['VolumeType']} in {vol['AvailabilityZone']}. "
                f"It's costing about ${monthly_cost:.0f} per month."
            )
        else:
            voice = (
                f"You have {count} {label}EBS volumes totaling {total_gb} gigabytes. "
                f"That's approximately ${monthly_cost:.0f} per month in storage costs. "
                f"Consider deleting unneeded volumes to reduce your bill."
            )
        return {"display": display, "voice_summary": voice}

    # ──────────────────────────── WRITE ────────────────────────────

    async def preview_stop(self, params: dict) -> dict:
        ids = params.get("instance_ids", [])
        if not ids:
            return {
                "display": "⚠️  No instance IDs specified. Example: 'Stop i-1234567890abcdef0'",
                "voice_summary": "No instance IDs were provided."
            }
        ids_str = ", ".join(ids)
        display = (
            f"⚠️  STOP EC2 INSTANCES\n\n"
            f"  Instances: {ids_str}\n\n"
            f"  Impact:\n"
            f"  • Running workloads will be interrupted\n"
            f"  • EBS data is preserved\n"
            f"  • Instance store data will be lost\n"
            f"  • Public IP may change on restart\n\n"
            f"  Do you want to stop these instances?"
        )
        voice = f"You are about to stop {len(ids)} instance{'s' if len(ids) != 1 else ''}. Do you want to proceed?"
        return {"display": display, "voice_summary": voice}

    async def stop_instances(self, params: dict) -> dict:
        ids = params.get("instance_ids", [])
        if not ids:
            return {"display": "⚠️  No instance IDs provided.", "voice_summary": "No instance IDs provided."}

        response = self.client.stop_instances(InstanceIds=ids)
        stopped = [s["InstanceId"] for s in response["StoppingInstances"]]
        display = f"✅ Stopping instances:\n" + "\n".join(f"  ▸ {i}" for i in stopped)
        voice = f"Successfully initiated stop for {len(stopped)} instance{'s' if len(stopped) != 1 else ''}."
        return {"display": display, "voice_summary": voice}

    async def preview_start(self, params: dict) -> dict:
        ids = params.get("instance_ids", [])
        ids_str = ", ".join(ids) if ids else "none specified"
        display = (
            f"▶  START EC2 INSTANCES\n\n"
            f"  Instances: {ids_str}\n\n"
            f"  Do you want to start these instances?"
        )
        voice = f"You are about to start {len(ids)} instance{'s' if len(ids) != 1 else ''}. Proceed?"
        return {"display": display, "voice_summary": voice}

    async def start_instances(self, params: dict) -> dict:
        ids = params.get("instance_ids", [])
        response = self.client.start_instances(InstanceIds=ids)
        started = [s["InstanceId"] for s in response["StartingInstances"]]
        display = f"✅ Starting instances:\n" + "\n".join(f"  ▸ {i}" for i in started)
        voice = f"Successfully started {len(started)} instance{'s' if len(started) != 1 else ''}."
        return {"display": display, "voice_summary": voice}

    # ──────────────────────────── HELPERS ────────────────────────────

    def _get_avg_cpu(self, instance_id: str) -> float | None:
        try:
            cw = boto3.client("cloudwatch")
            now = datetime.now(timezone.utc)
            from datetime import timedelta
            response = cw.get_metric_statistics(
                Namespace="AWS/EC2",
                MetricName="CPUUtilization",
                Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                StartTime=now - timedelta(hours=24),
                EndTime=now,
                Period=86400,
                Statistics=["Average"],
            )
            points = response.get("Datapoints", [])
            return points[0]["Average"] if points else None
        except Exception:
            return None

    def _estimate_monthly_cost(self, instance_type: str) -> float:
        # Rough on-demand pricing (us-east-1, Linux)
        pricing = {
            "t2.micro": 8.47, "t2.small": 16.79, "t2.medium": 33.41,
            "t3.micro": 7.59, "t3.small": 15.18, "t3.medium": 30.37,
            "m5.large": 69.12, "m5.xlarge": 138.24,
            "c5.large": 61.20, "c5.xlarge": 122.40,
        }
        return pricing.get(instance_type, 30.0)
