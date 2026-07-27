"""Tool definitions for the Claude agent + boto3 executors."""
import json
import logging
import os
import asyncio
from datetime import datetime, timezone, timedelta
from functools import partial

import boto3

logger = logging.getLogger(__name__)

# ─── Tool schemas for Bedrock converse API ────────────────────────────────────

TOOLS = [
    {
        "toolSpec": {
            "name": "list_ec2_instances",
            "description": (
                "List EC2 instances. Filter by state: 'all', 'running', 'stopped', "
                "or 'idle' (running instances with <10% average CPU over 24h, likely wasting money). "
                "Set region='all' to scan every AWS region at once."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "enum": ["all", "running", "stopped", "idle"],
                        "description": "Which instances to return"
                    },
                    "region": {
                        "type": "string",
                        "description": "AWS region name, or 'all' to scan every region simultaneously"
                    },
                },
            }},
        }
    },
    {
        "toolSpec": {
            "name": "stop_ec2_instance",
            "description": (
                "Stop one or more EC2 instances. DESTRUCTIVE — EBS data is preserved but "
                "instance store data is lost and the public IP may change. Always warn the "
                "user and get explicit confirmation before calling this tool."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "instance_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of EC2 instance IDs to stop (e.g. ['i-0abc123'])"
                    },
                },
                "required": ["instance_ids"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "start_ec2_instance",
            "description": "Start one or more stopped EC2 instances.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "instance_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of EC2 instance IDs to start"
                    },
                },
                "required": ["instance_ids"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "list_ebs_volumes",
            "description": "List EBS volumes. 'unattached' shows only available volumes not mounted to any instance (these are wasting money).",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "enum": ["all", "unattached"],
                        "description": "Return all volumes or only unattached ones"
                    },
                },
            }},
        }
    },
    {
        "toolSpec": {
            "name": "describe_security_groups",
            "description": "List security groups and their inbound rules. Optionally filter by name.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "name_filter": {
                        "type": "string",
                        "description": "Optional partial name match to filter results"
                    },
                },
            }},
        }
    },
    {
        "toolSpec": {
            "name": "modify_security_group",
            "description": (
                "Add or remove an inbound rule on a security group. "
                "DESTRUCTIVE for remove action — always warn and confirm before removing rules."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "group_id":  {"type": "string", "description": "Security group ID (sg-xxxxxxxx)"},
                    "action":    {"type": "string", "enum": ["add", "remove"]},
                    "port":      {"type": "integer", "description": "Port number"},
                    "protocol":  {"type": "string", "default": "tcp", "description": "tcp or udp"},
                    "cidr":      {"type": "string", "default": "0.0.0.0/0", "description": "CIDR range"},
                },
                "required": ["group_id", "action", "port"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "get_cost_report",
            "description": "Get AWS cost and usage for the last week or current month, broken down by service.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["week", "month"],
                        "description": "Time period for the report"
                    },
                },
            }},
        }
    },
    {
        "toolSpec": {
            "name": "list_ecr_repositories",
            "description": "List all ECR (Elastic Container Registry) repositories in the account.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "describe_ecr_images",
            "description": "List container images in an ECR repository with their tags, sizes, and push dates.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "repository_name": {"type": "string", "description": "ECR repository name"},
                },
                "required": ["repository_name"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "list_eks_clusters",
            "description": "List all EKS (Elastic Kubernetes Service) clusters with their status and version.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "describe_eks_cluster",
            "description": "Get detailed info about an EKS cluster: status, Kubernetes version, node groups, endpoint.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "cluster_name": {"type": "string", "description": "EKS cluster name"},
                },
                "required": ["cluster_name"],
            }},
        }
    },

    # ── EC2 create / terminate ──────────────────────────────────────────────────
    {
        "toolSpec": {
            "name": "create_ec2_instance",
            "description": (
                "Launch a new EC2 instance. Requires AMI ID and instance type. "
                "Optionally accepts a name tag, key pair, security group IDs, and subnet ID. "
                "Always confirm with the user before calling — this incurs cost."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "ami_id":            {"type": "string", "description": "AMI ID (e.g. ami-0abcdef1234567890)"},
                    "instance_type":     {"type": "string", "description": "e.g. t3.micro, t3.small"},
                    "name":              {"type": "string", "description": "Name tag for the instance"},
                    "key_name":          {"type": "string", "description": "EC2 key pair name for SSH access"},
                    "security_group_ids":{"type": "array", "items": {"type": "string"}, "description": "List of security group IDs"},
                    "subnet_id":         {"type": "string", "description": "Subnet ID to launch into"},
                },
                "required": ["ami_id", "instance_type"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "terminate_ec2_instance",
            "description": (
                "PERMANENTLY DELETE one or more EC2 instances. All data on instance store is lost. "
                "EBS root volume may also be deleted. This is IRREVERSIBLE. "
                "Always warn the user clearly and require explicit confirmation before calling."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "instance_ids": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Instance IDs to permanently terminate"
                    },
                },
                "required": ["instance_ids"],
            }},
        }
    },

    # ── S3 ──────────────────────────────────────────────────────────────────────
    {
        "toolSpec": {
            "name": "list_s3_buckets",
            "description": "List all S3 buckets in the account with their region and approximate object count.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "s3_bucket_info",
            "description": "List objects inside an S3 bucket (first 100). Shows key, size, last modified.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "bucket":  {"type": "string", "description": "S3 bucket name"},
                    "prefix":  {"type": "string", "description": "Optional key prefix to filter objects"},
                },
                "required": ["bucket"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "create_s3_bucket",
            "description": "Create a new S3 bucket. Bucket names must be globally unique and lowercase.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "bucket": {"type": "string", "description": "Globally unique bucket name (lowercase, no spaces)"},
                    "region": {"type": "string", "description": "AWS region (default: account default)"},
                },
                "required": ["bucket"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "delete_s3_bucket",
            "description": (
                "Delete an S3 bucket. The bucket must be empty first. DESTRUCTIVE and irreversible. "
                "Always confirm with the user before calling."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "bucket":     {"type": "string", "description": "Bucket name to delete"},
                    "force_empty":{"type": "boolean", "description": "If true, delete all objects first then delete the bucket"},
                },
                "required": ["bucket"],
            }},
        }
    },

    # ── IAM ─────────────────────────────────────────────────────────────────────
    {
        "toolSpec": {
            "name": "list_iam_users",
            "description": "List all IAM users in the account with their creation date, last login, groups, and attached policies.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "get_iam_user",
            "description": "Get full details for a specific IAM user: groups, inline and managed policies, access keys and their age.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "username": {"type": "string", "description": "IAM username"},
                },
                "required": ["username"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "list_iam_roles",
            "description": "List all IAM roles with their creation date, trust principals, and attached policies.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "list_iam_groups",
            "description": "List all IAM groups with their member count and attached policies.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "list_iam_policies",
            "description": "List customer-managed IAM policies (policies you created, not AWS-managed).",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "get_iam_account_summary",
            "description": "Get a high-level IAM account summary: total users, roles, groups, policies, MFA status.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "create_iam_user",
            "description": (
                "Create a new IAM user. Optionally add to a group. "
                "Always confirm with the user before calling — this grants AWS access."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "username":   {"type": "string", "description": "New IAM username"},
                    "group_name": {"type": "string", "description": "Optional group to add the user to"},
                },
                "required": ["username"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "delete_iam_user",
            "description": (
                "PERMANENTLY delete an IAM user, removing their access keys, group memberships, "
                "and attached policies. IRREVERSIBLE — always confirm before calling."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "username": {"type": "string", "description": "IAM username to delete"},
                },
                "required": ["username"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "list_iam_access_keys",
            "description": "List access keys for an IAM user, including their status and age in days.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "username": {"type": "string", "description": "IAM username"},
                },
                "required": ["username"],
            }},
        }
    },

    # ── RDS ─────────────────────────────────────────────────────────────────────
    {
        "toolSpec": {
            "name": "list_rds_instances",
            "description": "List all RDS database instances with their engine, class, status, and endpoint.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "describe_rds_instance",
            "description": "Get full details of a specific RDS instance: engine version, storage, backups, parameter groups.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "db_instance_id": {"type": "string", "description": "RDS DB instance identifier"},
                },
                "required": ["db_instance_id"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "create_rds_snapshot",
            "description": "Create a manual snapshot of an RDS instance. Safe — does not affect the running database.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "db_instance_id":  {"type": "string", "description": "RDS DB instance identifier"},
                    "snapshot_id":     {"type": "string", "description": "Name for the snapshot"},
                },
                "required": ["db_instance_id", "snapshot_id"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "stop_rds_instance",
            "description": (
                "Stop an RDS instance to save cost. The DB is not deleted — it can be restarted. "
                "Note: RDS auto-starts after 7 days. Confirm with user before calling."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "db_instance_id": {"type": "string", "description": "RDS DB instance identifier"},
                },
                "required": ["db_instance_id"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "start_rds_instance",
            "description": "Start a stopped RDS instance.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "db_instance_id": {"type": "string", "description": "RDS DB instance identifier"},
                },
                "required": ["db_instance_id"],
            }},
        }
    },

    # ── VPC / networking ─────────────────────────────────────────────────────────
    {
        "toolSpec": {
            "name": "create_vpc",
            "description": (
                "Create a simple VPC with one public subnet, an internet gateway, and a route "
                "table (single availability zone -- no NAT gateway or private subnet). Suitable "
                "for basic workloads that need internet access. Always confirm the CIDR and name "
                "with the user before calling -- this creates billable networking resources."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "name":              {"type": "string", "description": "Name tag to apply to the VPC and its resources"},
                    "cidr_block":        {"type": "string", "description": "VPC CIDR block, default 10.0.0.0/16"},
                    "subnet_cidr":       {"type": "string", "description": "Public subnet CIDR block, default 10.0.1.0/24"},
                    "availability_zone": {"type": "string", "description": "AZ for the subnet, e.g. us-east-1a (default: first AZ in region)"},
                    "region":            {"type": "string", "description": "AWS region (default: account default)"},
                },
            }},
        }
    },
    {
        "toolSpec": {
            "name": "list_vpcs",
            "description": "List VPCs in the account/region with their CIDR block, state, and whether they are the default VPC.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "region": {"type": "string", "description": "AWS region (default: account default)"},
                },
            }},
        }
    },
    {
        "toolSpec": {
            "name": "delete_vpc",
            "description": (
                "PERMANENTLY delete a VPC created by create_vpc, including its internet gateway, "
                "subnet(s), and route table(s). IRREVERSIBLE. Fails if other resources (e.g. EC2 "
                "instances, ENIs, NAT gateways) still depend on the VPC -- those must be removed "
                "first. Always warn the user clearly and require explicit confirmation before calling."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "vpc_id": {"type": "string", "description": "VPC ID to delete (vpc-xxxxxxxx)"},
                    "region": {"type": "string", "description": "AWS region (default: account default)"},
                },
                "required": ["vpc_id"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "create_security_group",
            "description": (
                "Create a new security group inside a VPC. Created with no inbound rules by default "
                "(add rules afterward with modify_security_group). Confirm the VPC and name with the "
                "user before calling."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "name":        {"type": "string", "description": "Security group name"},
                    "description": {"type": "string", "description": "Security group description"},
                    "vpc_id":      {"type": "string", "description": "VPC ID to create the security group in"},
                    "region":      {"type": "string", "description": "AWS region (default: account default)"},
                },
                "required": ["name", "vpc_id"],
            }},
        }
    },

    # ── ECR create ────────────────────────────────────────────────────────────────
    {
        "toolSpec": {
            "name": "create_ecr_repository",
            "description": (
                "Create a new ECR (Elastic Container Registry) repository. Returns the repository "
                "URI to use for docker push/pull. Confirm the repository name with the user before "
                "calling."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "name":           {"type": "string", "description": "Repository name (lowercase, may include slashes, e.g. 'my-app' or 'team/my-app')"},
                    "scan_on_push":   {"type": "boolean", "description": "Automatically scan images for vulnerabilities on push (default true)"},
                    "tag_mutability": {"type": "string", "enum": ["MUTABLE", "IMMUTABLE"], "description": "Whether image tags can be overwritten (default IMMUTABLE)"},
                },
                "required": ["name"],
            }},
        }
    },

    # ── Security audit ───────────────────────────────────────────────────────────
    {
        "toolSpec": {
            "name": "security_audit",
            "description": (
                "Run a read-only security scan of the AWS account: open security groups (0.0.0.0/0 "
                "or ::/0 on sensitive ports or all traffic), publicly accessible S3 buckets, IAM users "
                "with console access but no MFA, IAM access keys older than 90 days, publicly "
                "accessible RDS instances, and root account MFA status. Returns findings with "
                "severity. Safe to call anytime -- makes no changes."
            ),
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "call_user",
            "description": (
                "Call the user's phone number to deliver an important AWS alert or message. "
                "Use this when the user asks you to call them, or when you find a critical "
                "issue (security vulnerability, high costs, etc.) that warrants a phone call. "
                "The user's phone number is already configured — just provide the message to speak."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The message to speak during the call. Keep it concise (2-4 sentences)."
                    },
                },
                "required": ["message"],
            }},
        }
    },
]


# ─── Dispatcher ───────────────────────────────────────────────────────────────

async def execute_tool(name: str, params: dict) -> dict:
    handlers = {
        "list_ec2_instances":     _list_ec2_instances,
        "stop_ec2_instance":      _stop_ec2_instances,
        "start_ec2_instance":     _start_ec2_instances,
        "list_ebs_volumes":       _list_ebs_volumes,
        "describe_security_groups": _describe_security_groups,
        "modify_security_group":  _modify_security_group,
        "get_cost_report":        _get_cost_report,
        "list_ecr_repositories":  _list_ecr_repositories,
        "describe_ecr_images":    _describe_ecr_images,
        "list_eks_clusters":        _list_eks_clusters,
        "describe_eks_cluster":     _describe_eks_cluster,
        # EC2 extended
        "create_ec2_instance":      _create_ec2_instance,
        "terminate_ec2_instance":   _terminate_ec2_instance,
        # IAM
        "list_iam_users":           _list_iam_users,
        "get_iam_user":             _get_iam_user,
        "list_iam_roles":           _list_iam_roles,
        "list_iam_groups":          _list_iam_groups,
        "list_iam_policies":        _list_iam_policies,
        "get_iam_account_summary":  _get_iam_account_summary,
        "create_iam_user":          _create_iam_user,
        "delete_iam_user":          _delete_iam_user,
        "list_iam_access_keys":     _list_iam_access_keys,
        # S3
        "list_s3_buckets":          _list_s3_buckets,
        "s3_bucket_info":           _s3_bucket_info,
        "create_s3_bucket":         _create_s3_bucket,
        "delete_s3_bucket":         _delete_s3_bucket,
        # RDS
        "list_rds_instances":       _list_rds_instances,
        "describe_rds_instance":    _describe_rds_instance,
        "create_rds_snapshot":      _create_rds_snapshot,
        "stop_rds_instance":        _stop_rds_instance,
        "start_rds_instance":       _start_rds_instance,
        # VPC / networking
        "create_vpc":               _create_vpc,
        "list_vpcs":                _list_vpcs,
        "delete_vpc":               _delete_vpc,
        "create_security_group":    _create_security_group,
        # ECR
        "create_ecr_repository":    _create_ecr_repository,
        # Security
        "security_audit":           _security_audit,
        # Bolna phone call
        "call_user":                _call_user,
    }
    fn = handlers.get(name)
    if not fn:
        return {"error": f"Unknown tool: {name}"}
    try:
        return await fn(params)
    except Exception as e:
        logger.error(f"Tool [{name}] error: {e}")
        return {"error": str(e)}


# ─── Implementations ──────────────────────────────────────────────────────────

def _query_ec2_region(region_name: str, filter_type: str) -> list:
    """Scan one region for EC2 instances. Returns list of instance dicts."""
    try:
        ec2 = boto3.client("ec2", region_name=region_name)
        filters = []
        if filter_type in ("running", "idle"):
            filters = [{"Name": "instance-state-name", "Values": ["running"]}]
        elif filter_type == "stopped":
            filters = [{"Name": "instance-state-name", "Values": ["stopped"]}]

        resp = ec2.describe_instances(Filters=filters)
        instances = []
        for reservation in resp["Reservations"]:
            for inst in reservation["Instances"]:
                name = next(
                    (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"),
                    inst["InstanceId"],
                )
                cpu = None
                if filter_type == "idle":
                    cpu = _get_avg_cpu(inst["InstanceId"])
                    if cpu is not None and cpu > 10:
                        continue
                instances.append({
                    "id":               inst["InstanceId"],
                    "name":             name,
                    "type":             inst["InstanceType"],
                    "state":            inst["State"]["Name"],
                    "region":           region_name,
                    "az":               inst["Placement"]["AvailabilityZone"],
                    "public_ip":        inst.get("PublicIpAddress"),
                    "private_ip":       inst.get("PrivateIpAddress"),
                    "launch_time":      inst["LaunchTime"].isoformat() if inst.get("LaunchTime") else None,
                    "cpu_24h_avg_pct":  round(cpu, 1) if cpu is not None else None,
                })
        return instances
    except Exception as e:
        logger.warning(f"EC2 scan skipped for {region_name}: {e}")
        return []


async def _list_ec2_instances(params: dict) -> dict:
    filter_type = params.get("filter", "all")
    region = params.get("region") or None

    if region and region.lower() != "all":
        # Single region
        loop = asyncio.get_event_loop()
        instances = await loop.run_in_executor(None, _query_ec2_region, region, filter_type)
        return {"instances": instances, "count": len(instances), "filter": filter_type, "regions_scanned": [region]}

    # All regions — fetch region list then scan in parallel
    def _get_all_regions():
        ec2 = boto3.client("ec2")
        resp = ec2.describe_regions(Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}])
        return [r["RegionName"] for r in resp["Regions"]]

    loop = asyncio.get_event_loop()
    all_regions = await loop.run_in_executor(None, _get_all_regions)

    tasks = [loop.run_in_executor(None, _query_ec2_region, r, filter_type) for r in all_regions]
    results = await asyncio.gather(*tasks)

    instances = [inst for region_list in results for inst in region_list]
    by_region = {}
    for inst in instances:
        by_region.setdefault(inst["region"], []).append(inst)

    return {
        "instances":       instances,
        "count":           len(instances),
        "filter":          filter_type,
        "regions_scanned": len(all_regions),
        "by_region":       {r: len(v) for r, v in by_region.items()},
    }


async def _stop_ec2_instances(params: dict) -> dict:
    ids = params.get("instance_ids", [])

    def _run():
        ec2 = boto3.client("ec2")
        resp = ec2.stop_instances(InstanceIds=ids)
        return {
            "stopped": [s["InstanceId"] for s in resp["StoppingInstances"]],
            "status": "stopping",
        }

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


# ─── Bolna phone call ─────────────────────────────────────────────────────────

async def _call_user(params: dict) -> dict:
    """Call the user's phone with a message using Bolna AI."""
    from voice.bolna_caller import trigger_call

    message = params.get("message", "")
    if not message:
        return {"success": False, "detail": "No message provided"}

    # Read the saved phone number from settings file
    phone_number = ""
    settings_path = os.path.join(os.path.dirname(__file__), "..", "data", "settings.json")
    try:
        with open(settings_path) as f:
            settings = json.load(f)
            phone_number = settings.get("phone_number", "")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    result = await trigger_call(phone_number, message)
    return result


async def _start_ec2_instances(params: dict) -> dict:
    ids = params.get("instance_ids", [])

    def _run():
        ec2 = boto3.client("ec2")
        resp = ec2.start_instances(InstanceIds=ids)
        return {
            "started": [s["InstanceId"] for s in resp["StartingInstances"]],
            "status": "pending",
        }

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _list_ebs_volumes(params: dict) -> dict:
    filter_type = params.get("filter", "all")

    def _run():
        ec2 = boto3.client("ec2")
        filters = [{"Name": "status", "Values": ["available"]}] if filter_type == "unattached" else []
        resp = ec2.describe_volumes(Filters=filters)
        volumes = []
        for v in resp["Volumes"]:
            name = next((t["Value"] for t in v.get("Tags", []) if t["Key"] == "Name"), None)
            volumes.append({
                "id": v["VolumeId"],
                "name": name,
                "size_gb": v["Size"],
                "type": v["VolumeType"],
                "state": v["State"],
                "az": v["AvailabilityZone"],
                "attached_to": [a["InstanceId"] for a in v.get("Attachments", [])],
                "monthly_cost_usd": round(v["Size"] * 0.08, 2),
            })
        return {
            "volumes": volumes,
            "count": len(volumes),
            "total_gb": sum(v["size_gb"] for v in volumes),
            "filter": filter_type,
        }

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _describe_security_groups(params: dict) -> dict:
    name_filter = params.get("name_filter", "")

    def _run():
        ec2 = boto3.client("ec2")
        kwargs = {}
        if name_filter:
            kwargs["Filters"] = [{"Name": "group-name", "Values": [f"*{name_filter}*"]}]
        resp = ec2.describe_security_groups(**kwargs)
        groups = []
        for sg in resp["SecurityGroups"][:25]:
            inbound = []
            for rule in sg.get("IpPermissions", []):
                for ip_range in rule.get("IpRanges", []):
                    inbound.append({
                        "protocol": rule.get("IpProtocol", "all"),
                        "from_port": rule.get("FromPort"),
                        "to_port": rule.get("ToPort"),
                        "cidr": ip_range.get("CidrIp"),
                        "description": ip_range.get("Description", ""),
                    })
            groups.append({
                "id": sg["GroupId"],
                "name": sg["GroupName"],
                "description": sg.get("Description", ""),
                "vpc_id": sg.get("VpcId"),
                "inbound_rules": inbound,
            })
        return {"security_groups": groups, "count": len(groups)}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _modify_security_group(params: dict) -> dict:
    group_id = params["group_id"]
    action   = params["action"]
    port     = params["port"]
    protocol = params.get("protocol", "tcp")
    cidr     = params.get("cidr", "0.0.0.0/0")

    def _run():
        ec2 = boto3.client("ec2")
        rule = {
            "IpProtocol": protocol,
            "FromPort": port,
            "ToPort": port,
            "IpRanges": [{"CidrIp": cidr}],
        }
        if action == "add":
            ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=[rule])
        else:
            ec2.revoke_security_group_ingress(GroupId=group_id, IpPermissions=[rule])
        return {"success": True, "action": action, "group_id": group_id,
                "port": port, "protocol": protocol, "cidr": cidr}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _get_cost_report(params: dict) -> dict:
    from datetime import date
    period = params.get("period", "month")

    def _run():
        ce = boto3.client("ce", region_name="us-east-1")
        today = date.today()
        if period == "week":
            start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
            label = "last 7 days"
        else:
            start = today.replace(day=1).strftime("%Y-%m-%d")
            label = "month-to-date"
        end = today.strftime("%Y-%m-%d")

        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        breakdown = []
        total = 0.0
        for result in resp["ResultsByTime"]:
            for group in result["Groups"]:
                amt = float(group["Metrics"]["UnblendedCost"]["Amount"])
                if amt > 0.01:
                    breakdown.append({"service": group["Keys"][0], "cost_usd": round(amt, 2)})
                    total += amt
        breakdown.sort(key=lambda x: x["cost_usd"], reverse=True)
        return {"period": label, "total_usd": round(total, 2), "breakdown": breakdown[:15]}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _list_ecr_repositories(params: dict) -> dict:
    def _run():
        ecr = boto3.client("ecr")
        resp = ecr.describe_repositories()
        repos = []
        for r in resp.get("repositories", []):
            repos.append({
                "name": r["repositoryName"],
                "uri": r["repositoryUri"],
                "created_at": r["createdAt"].isoformat() if r.get("createdAt") else None,
                "tag_mutability": r.get("imageTagMutability"),
                "scan_on_push": r.get("imageScanningConfiguration", {}).get("scanOnPush", False),
            })
        return {"repositories": repos, "count": len(repos)}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _describe_ecr_images(params: dict) -> dict:
    repo = params["repository_name"]

    def _run():
        ecr = boto3.client("ecr")
        resp = ecr.describe_images(repositoryName=repo, filter={"tagStatus": "ANY"})
        images = sorted(
            resp.get("imageDetails", []),
            key=lambda x: x.get("imagePushedAt", datetime.min.replace(tzinfo=timezone.utc)),
            reverse=True,
        )[:20]
        result = []
        for img in images:
            result.append({
                "tags": img.get("imageTags", ["<untagged>"]),
                "digest_short": img.get("imageDigest", "")[:19] + "...",
                "size_mb": round(img.get("imageSizeInBytes", 0) / 1024 / 1024, 1),
                "pushed_at": img["imagePushedAt"].isoformat() if img.get("imagePushedAt") else None,
            })
        return {"repository": repo, "images": result, "count": len(result)}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _list_eks_clusters(params: dict) -> dict:
    def _run():
        eks = boto3.client("eks")
        names = eks.list_clusters().get("clusters", [])
        clusters = []
        for name in names:
            try:
                d = eks.describe_cluster(name=name)["cluster"]
                clusters.append({
                    "name": name,
                    "status": d.get("status"),
                    "kubernetes_version": d.get("version"),
                    "endpoint": d.get("endpoint"),
                })
            except Exception:
                clusters.append({"name": name, "status": "unknown"})
        return {"clusters": clusters, "count": len(clusters)}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _describe_eks_cluster(params: dict) -> dict:
    cluster_name = params["cluster_name"]

    def _run():
        eks = boto3.client("eks")
        d = eks.describe_cluster(name=cluster_name)["cluster"]
        try:
            node_groups = eks.list_nodegroups(clusterName=cluster_name).get("nodegroups", [])
        except Exception:
            node_groups = []
        return {
            "name": d.get("name"),
            "status": d.get("status"),
            "kubernetes_version": d.get("version"),
            "endpoint": d.get("endpoint"),
            "created_at": d["createdAt"].isoformat() if d.get("createdAt") else None,
            "node_groups": node_groups,
            "role_arn": d.get("roleArn"),
            "tags": d.get("tags", {}),
        }

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


# ─── EC2 create / terminate ──────────────────────────────────────────────────

async def _create_ec2_instance(params: dict) -> dict:
    def _run():
        ec2 = boto3.client("ec2")
        kwargs = {
            "ImageId":      params["ami_id"],
            "InstanceType": params["instance_type"],
            "MinCount": 1, "MaxCount": 1,
        }
        if params.get("key_name"):
            kwargs["KeyName"] = params["key_name"]
        if params.get("security_group_ids"):
            kwargs["SecurityGroupIds"] = params["security_group_ids"]
        if params.get("subnet_id"):
            kwargs["SubnetId"] = params["subnet_id"]
        if params.get("name"):
            kwargs["TagSpecifications"] = [{
                "ResourceType": "instance",
                "Tags": [{"Key": "Name", "Value": params["name"]}],
            }]
        resp = ec2.run_instances(**kwargs)
        inst = resp["Instances"][0]
        return {
            "instance_id":   inst["InstanceId"],
            "instance_type": inst["InstanceType"],
            "state":         inst["State"]["Name"],
            "ami_id":        inst["ImageId"],
            "private_ip":    inst.get("PrivateIpAddress"),
        }
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _terminate_ec2_instance(params: dict) -> dict:
    ids = params.get("instance_ids", [])
    def _run():
        ec2 = boto3.client("ec2")
        resp = ec2.terminate_instances(InstanceIds=ids)
        return {
            "terminated": [i["InstanceId"] for i in resp["TerminatingInstances"]],
            "status": "shutting-down",
            "warning": "This action is PERMANENT and cannot be undone.",
        }
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


# ─── S3 ───────────────────────────────────────────────────────────────────────

async def _list_s3_buckets(params: dict) -> dict:
    def _run():
        s3 = boto3.client("s3")
        resp = s3.list_buckets()
        buckets = []
        for b in resp.get("Buckets", []):
            name = b["Name"]
            region = "unknown"
            try:
                loc = s3.get_bucket_location(Bucket=name)
                region = loc["LocationConstraint"] or "us-east-1"
            except Exception:
                pass
            buckets.append({
                "name":         name,
                "region":       region,
                "created":      b["CreationDate"].isoformat() if b.get("CreationDate") else None,
            })
        return {"buckets": buckets, "count": len(buckets)}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _s3_bucket_info(params: dict) -> dict:
    bucket = params["bucket"]
    prefix = params.get("prefix", "")
    def _run():
        s3 = boto3.client("s3")
        kwargs = {"Bucket": bucket, "MaxKeys": 100}
        if prefix:
            kwargs["Prefix"] = prefix
        resp = s3.list_objects_v2(**kwargs)
        objects = []
        for obj in resp.get("Contents", []):
            objects.append({
                "key":           obj["Key"],
                "size_kb":       round(obj["Size"] / 1024, 1),
                "last_modified": obj["LastModified"].isoformat(),
            })
        return {
            "bucket":       bucket,
            "objects":      objects,
            "count":        len(objects),
            "truncated":    resp.get("IsTruncated", False),
            "total_size_mb": round(sum(o["size_kb"] for o in objects) / 1024, 2),
        }
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _create_s3_bucket(params: dict) -> dict:
    bucket = params["bucket"]
    region = params.get("region") or boto3.session.Session().region_name or "us-east-1"
    def _run():
        s3 = boto3.client("s3", region_name=region)
        kwargs = {"Bucket": bucket}
        if region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**kwargs)
        return {"created": True, "bucket": bucket, "region": region}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _delete_s3_bucket(params: dict) -> dict:
    bucket = params["bucket"]
    force  = params.get("force_empty", False)
    def _run():
        s3 = boto3.resource("s3")
        b  = s3.Bucket(bucket)
        if force:
            deleted = b.objects.all().delete()
            count = sum(len(d.get("Deleted", [])) for d in (deleted if isinstance(deleted, list) else [deleted]))
        else:
            count = 0
        boto3.client("s3").delete_bucket(Bucket=bucket)
        return {"deleted": True, "bucket": bucket, "objects_removed": count}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


# ─── RDS ──────────────────────────────────────────────────────────────────────

async def _list_rds_instances(params: dict) -> dict:
    def _run():
        rds = boto3.client("rds")
        resp = rds.describe_db_instances()
        instances = []
        for db in resp.get("DBInstances", []):
            instances.append({
                "id":             db["DBInstanceIdentifier"],
                "engine":         db["Engine"] + " " + db.get("EngineVersion", ""),
                "class":          db["DBInstanceClass"],
                "status":         db["DBInstanceStatus"],
                "storage_gb":     db.get("AllocatedStorage"),
                "multi_az":       db.get("MultiAZ", False),
                "endpoint":       db.get("Endpoint", {}).get("Address"),
                "port":           db.get("Endpoint", {}).get("Port"),
            })
        return {"instances": instances, "count": len(instances)}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _describe_rds_instance(params: dict) -> dict:
    db_id = params["db_instance_id"]
    def _run():
        rds = boto3.client("rds")
        resp = rds.describe_db_instances(DBInstanceIdentifier=db_id)
        db = resp["DBInstances"][0]
        return {
            "id":                   db["DBInstanceIdentifier"],
            "engine":               db["Engine"],
            "engine_version":       db.get("EngineVersion"),
            "class":                db["DBInstanceClass"],
            "status":               db["DBInstanceStatus"],
            "storage_gb":           db.get("AllocatedStorage"),
            "storage_type":         db.get("StorageType"),
            "multi_az":             db.get("MultiAZ"),
            "endpoint":             db.get("Endpoint", {}).get("Address"),
            "port":                 db.get("Endpoint", {}).get("Port"),
            "db_name":              db.get("DBName"),
            "master_username":      db.get("MasterUsername"),
            "backup_retention":     db.get("BackupRetentionPeriod"),
            "publicly_accessible":  db.get("PubliclyAccessible"),
            "created":              db["InstanceCreateTime"].isoformat() if db.get("InstanceCreateTime") else None,
        }
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _create_rds_snapshot(params: dict) -> dict:
    db_id       = params["db_instance_id"]
    snapshot_id = params["snapshot_id"]
    def _run():
        rds = boto3.client("rds")
        resp = rds.create_db_snapshot(
            DBInstanceIdentifier=db_id,
            DBSnapshotIdentifier=snapshot_id,
        )
        snap = resp["DBSnapshot"]
        return {
            "snapshot_id": snap["DBSnapshotIdentifier"],
            "db_instance":  snap["DBInstanceIdentifier"],
            "status":       snap["Status"],
            "created":      snap["SnapshotCreateTime"].isoformat() if snap.get("SnapshotCreateTime") else None,
        }
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _stop_rds_instance(params: dict) -> dict:
    db_id = params["db_instance_id"]
    def _run():
        rds = boto3.client("rds")
        rds.stop_db_instance(DBInstanceIdentifier=db_id)
        return {"db_instance": db_id, "status": "stopping", "note": "RDS auto-restarts after 7 days."}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _start_rds_instance(params: dict) -> dict:
    db_id = params["db_instance_id"]
    def _run():
        rds = boto3.client("rds")
        rds.start_db_instance(DBInstanceIdentifier=db_id)
        return {"db_instance": db_id, "status": "starting"}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


# ─── IAM ──────────────────────────────────────────────────────────────────────

async def _list_iam_users(params: dict) -> dict:
    def _run():
        iam = boto3.client("iam")
        paginator = iam.get_paginator("list_users")
        users = []
        for page in paginator.paginate():
            for u in page["Users"]:
                username = u["UserName"]
                # Groups
                groups = [g["GroupName"] for g in iam.list_groups_for_user(UserName=username).get("Groups", [])]
                # Attached managed policies
                policies = [p["PolicyName"] for p in iam.list_attached_user_policies(UserName=username).get("AttachedPolicies", [])]
                # Access keys count
                keys = iam.list_access_keys(UserName=username).get("AccessKeyMetadata", [])
                # Last login
                last_login = None
                try:
                    pw = iam.get_login_profile(UserName=username)
                    last_login = u.get("PasswordLastUsed")
                except iam.exceptions.NoSuchEntityException:
                    pass
                users.append({
                    "username":       username,
                    "user_id":        u["UserId"],
                    "created":        u["CreateDate"].isoformat(),
                    "last_login":     last_login.isoformat() if last_login else "never",
                    "groups":         groups,
                    "policies":       policies,
                    "access_key_count": len(keys),
                    "has_console_access": last_login is not None or _has_login_profile(iam, username),
                })
        return {"users": users, "count": len(users)}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


def _has_login_profile(iam_client, username: str) -> bool:
    try:
        iam_client.get_login_profile(UserName=username)
        return True
    except Exception:
        return False


async def _get_iam_user(params: dict) -> dict:
    username = params["username"]
    def _run():
        iam = boto3.client("iam")
        u = iam.get_user(UserName=username)["User"]
        groups  = [g["GroupName"] for g in iam.list_groups_for_user(UserName=username).get("Groups", [])]
        managed = [p["PolicyName"] for p in iam.list_attached_user_policies(UserName=username).get("AttachedPolicies", [])]
        inline  = iam.list_user_policies(UserName=username).get("PolicyNames", [])
        keys    = []
        for k in iam.list_access_keys(UserName=username).get("AccessKeyMetadata", []):
            age = (datetime.now(timezone.utc) - k["CreateDate"]).days
            keys.append({"key_id": k["AccessKeyId"], "status": k["Status"], "age_days": age})
        return {
            "username":         username,
            "user_id":          u["UserId"],
            "arn":              u["Arn"],
            "created":          u["CreateDate"].isoformat(),
            "last_login":       u["PasswordLastUsed"].isoformat() if u.get("PasswordLastUsed") else "never",
            "groups":           groups,
            "managed_policies": managed,
            "inline_policies":  inline,
            "access_keys":      keys,
        }
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _list_iam_roles(params: dict) -> dict:
    def _run():
        iam = boto3.client("iam")
        paginator = iam.get_paginator("list_roles")
        roles = []
        for page in paginator.paginate():
            for r in page["Roles"]:
                policies = [p["PolicyName"] for p in iam.list_attached_role_policies(RoleName=r["RoleName"]).get("AttachedPolicies", [])]
                trust = []
                for stmt in r.get("AssumeRolePolicyDocument", {}).get("Statement", []):
                    principal = stmt.get("Principal", {})
                    if isinstance(principal, dict):
                        for v in principal.values():
                            trust += (v if isinstance(v, list) else [v])
                    elif isinstance(principal, str):
                        trust.append(principal)
                roles.append({
                    "name":             r["RoleName"],
                    "role_id":          r["RoleId"],
                    "created":          r["CreateDate"].isoformat(),
                    "description":      r.get("Description", ""),
                    "trust_principals": trust[:5],
                    "attached_policies": policies,
                })
        return {"roles": roles, "count": len(roles)}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _list_iam_groups(params: dict) -> dict:
    def _run():
        iam = boto3.client("iam")
        paginator = iam.get_paginator("list_groups")
        groups = []
        for page in paginator.paginate():
            for g in page["Groups"]:
                policies = [p["PolicyName"] for p in iam.list_attached_group_policies(GroupName=g["GroupName"]).get("AttachedPolicies", [])]
                members = iam.get_group(GroupName=g["GroupName"]).get("Users", [])
                groups.append({
                    "name":             g["GroupName"],
                    "group_id":         g["GroupId"],
                    "created":          g["CreateDate"].isoformat(),
                    "member_count":     len(members),
                    "members":          [m["UserName"] for m in members],
                    "attached_policies": policies,
                })
        return {"groups": groups, "count": len(groups)}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _list_iam_policies(params: dict) -> dict:
    def _run():
        iam = boto3.client("iam")
        paginator = iam.get_paginator("list_policies")
        policies = []
        for page in paginator.paginate(Scope="Local"):  # Local = customer-managed only
            for p in page["Policies"]:
                policies.append({
                    "name":           p["PolicyName"],
                    "policy_id":      p["PolicyId"],
                    "arn":            p["Arn"],
                    "created":        p["CreateDate"].isoformat(),
                    "updated":        p["UpdateDate"].isoformat(),
                    "attachment_count": p.get("AttachmentCount", 0),
                    "description":    p.get("Description", ""),
                })
        return {"policies": policies, "count": len(policies)}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _get_iam_account_summary(params: dict) -> dict:
    def _run():
        iam = boto3.client("iam")
        s = iam.get_account_summary()["SummaryMap"]
        return {
            "users":                s.get("Users", 0),
            "users_quota":          s.get("UsersQuota", 0),
            "groups":               s.get("Groups", 0),
            "roles":                s.get("Roles", 0),
            "policies":             s.get("Policies", 0),
            "mfa_devices":          s.get("MFADevices", 0),
            "mfa_devices_in_use":   s.get("MFADevicesInUse", 0),
            "account_mfa_enabled":  s.get("AccountMFAEnabled", 0) == 1,
            "access_keys_per_user_quota": s.get("AccessKeysPerUserQuota", 2),
            "attached_policies_per_user": s.get("AttachedPoliciesPerUserQuota", 0),
        }
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _create_iam_user(params: dict) -> dict:
    username   = params["username"]
    group_name = params.get("group_name")
    def _run():
        iam = boto3.client("iam")
        u = iam.create_user(UserName=username)["User"]
        result = {
            "created":  True,
            "username": username,
            "user_id":  u["UserId"],
            "arn":      u["Arn"],
        }
        if group_name:
            iam.add_user_to_group(GroupName=group_name, UserName=username)
            result["added_to_group"] = group_name
        return result
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _delete_iam_user(params: dict) -> dict:
    username = params["username"]
    def _run():
        iam = boto3.client("iam")
        # Must clean up everything before deleting
        for key in iam.list_access_keys(UserName=username).get("AccessKeyMetadata", []):
            iam.delete_access_key(UserName=username, AccessKeyId=key["AccessKeyId"])
        for p in iam.list_attached_user_policies(UserName=username).get("AttachedPolicies", []):
            iam.detach_user_policy(UserName=username, PolicyArn=p["PolicyArn"])
        for p in iam.list_user_policies(UserName=username).get("PolicyNames", []):
            iam.delete_user_policy(UserName=username, PolicyName=p)
        for g in iam.list_groups_for_user(UserName=username).get("Groups", []):
            iam.remove_user_from_group(GroupName=g["GroupName"], UserName=username)
        try:
            iam.delete_login_profile(UserName=username)
        except Exception:
            pass
        iam.delete_user(UserName=username)
        return {"deleted": True, "username": username}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _list_iam_access_keys(params: dict) -> dict:
    username = params["username"]
    def _run():
        iam = boto3.client("iam")
        keys = []
        for k in iam.list_access_keys(UserName=username).get("AccessKeyMetadata", []):
            age = (datetime.now(timezone.utc) - k["CreateDate"]).days
            # Last used
            last_used_info = iam.get_access_key_last_used(AccessKeyId=k["AccessKeyId"])
            last_used = last_used_info.get("AccessKeyLastUsed", {})
            keys.append({
                "key_id":        k["AccessKeyId"],
                "status":        k["Status"],
                "created":       k["CreateDate"].isoformat(),
                "age_days":      age,
                "last_used_date": last_used.get("LastUsedDate", "never"),
                "last_used_service": last_used.get("ServiceName", "—"),
                "last_used_region":  last_used.get("Region", "—"),
            })
        return {"username": username, "access_keys": keys, "count": len(keys)}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


# ─── CloudWatch helper ────────────────────────────────────────────────────────

def _get_avg_cpu(instance_id: str) -> float | None:
    try:
        cw = boto3.client("cloudwatch")
        now = datetime.now(timezone.utc)
        resp = cw.get_metric_statistics(
            Namespace="AWS/EC2",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=now - timedelta(hours=24),
            EndTime=now,
            Period=86400,
            Statistics=["Average"],
        )
        pts = resp.get("Datapoints", [])
        return pts[0]["Average"] if pts else None
    except Exception:
        return None


# ─── VPC / networking ─────────────────────────────────────────────────────────

async def _create_vpc(params: dict) -> dict:
    name        = params.get("name") or "awscheppu-vpc"
    cidr        = params.get("cidr_block") or "10.0.0.0/16"
    subnet_cidr = params.get("subnet_cidr") or "10.0.1.0/24"
    az          = params.get("availability_zone")
    region      = params.get("region") or None

    def _run():
        ec2 = boto3.client("ec2", region_name=region) if region else boto3.client("ec2")
        az_use = az
        if not az_use:
            azs = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])
            az_use = azs["AvailabilityZones"][0]["ZoneName"]

        vpc = ec2.create_vpc(CidrBlock=cidr)["Vpc"]
        vpc_id = vpc["VpcId"]
        ec2.get_waiter("vpc_available").wait(VpcIds=[vpc_id])
        ec2.create_tags(Resources=[vpc_id], Tags=[{"Key": "Name", "Value": name}])
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

        subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=subnet_cidr, AvailabilityZone=az_use)["Subnet"]
        subnet_id = subnet["SubnetId"]
        ec2.create_tags(Resources=[subnet_id], Tags=[{"Key": "Name", "Value": f"{name}-public-subnet"}])
        ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})

        igw_id = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
        ec2.create_tags(Resources=[igw_id], Tags=[{"Key": "Name", "Value": f"{name}-igw"}])
        ec2.attach_internet_gateway(VpcId=vpc_id, InternetGatewayId=igw_id)

        rt_id = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
        ec2.create_tags(Resources=[rt_id], Tags=[{"Key": "Name", "Value": f"{name}-rt"}])
        ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
        ec2.associate_route_table(RouteTableId=rt_id, SubnetId=subnet_id)

        return {
            "created": True, "vpc_id": vpc_id, "name": name, "cidr_block": cidr,
            "subnet_id": subnet_id, "subnet_cidr": subnet_cidr, "availability_zone": az_use,
            "internet_gateway_id": igw_id, "route_table_id": rt_id,
        }
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _list_vpcs(params: dict) -> dict:
    region = params.get("region") or None

    def _run():
        ec2 = boto3.client("ec2", region_name=region) if region else boto3.client("ec2")
        resp = ec2.describe_vpcs()
        vpcs = []
        for v in resp["Vpcs"]:
            name = next((t["Value"] for t in v.get("Tags", []) if t["Key"] == "Name"), None)
            vpcs.append({
                "id": v["VpcId"], "name": name, "cidr_block": v.get("CidrBlock"),
                "state": v.get("State"), "is_default": v.get("IsDefault", False),
            })
        return {"vpcs": vpcs, "count": len(vpcs)}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _delete_vpc(params: dict) -> dict:
    vpc_id = params["vpc_id"]
    region = params.get("region") or None

    def _run():
        ec2 = boto3.client("ec2", region_name=region) if region else boto3.client("ec2")

        # 1. Detach + delete internet gateway(s)
        igws = ec2.describe_internet_gateways(
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
        )["InternetGateways"]
        for igw in igws:
            ec2.detach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id)
            ec2.delete_internet_gateway(InternetGatewayId=igw["InternetGatewayId"])

        # 2. Delete subnets
        subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
        for sn in subnets:
            ec2.delete_subnet(SubnetId=sn["SubnetId"])

        # 3. Disassociate + delete non-main route tables (main RT deletes implicitly with the VPC
        #    and cannot be targeted directly -- must filter it out via Associations[].Main)
        rts = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["RouteTables"]
        for rt in rts:
            is_main = any(a.get("Main") for a in rt.get("Associations", []))
            for assoc in rt.get("Associations", []):
                if not assoc.get("Main") and assoc.get("RouteTableAssociationId"):
                    ec2.disassociate_route_table(AssociationId=assoc["RouteTableAssociationId"])
            if not is_main:
                ec2.delete_route_table(RouteTableId=rt["RouteTableId"])

        # 4. Finally delete the VPC itself
        ec2.delete_vpc(VpcId=vpc_id)
        return {"deleted": True, "vpc_id": vpc_id}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _create_security_group(params: dict) -> dict:
    name        = params["name"]
    description = params.get("description") or f"{name} security group"
    vpc_id      = params["vpc_id"]
    region      = params.get("region") or None

    def _run():
        ec2 = boto3.client("ec2", region_name=region) if region else boto3.client("ec2")
        resp = ec2.create_security_group(GroupName=name, Description=description, VpcId=vpc_id)
        group_id = resp["GroupId"]
        ec2.create_tags(Resources=[group_id], Tags=[{"Key": "Name", "Value": name}])
        return {"created": True, "group_id": group_id, "name": name, "vpc_id": vpc_id}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


# ─── ECR create ───────────────────────────────────────────────────────────────

async def _create_ecr_repository(params: dict) -> dict:
    name           = params["name"]
    scan_on_push   = params.get("scan_on_push", True)
    tag_mutability = params.get("tag_mutability", "IMMUTABLE")

    def _run():
        ecr = boto3.client("ecr")
        resp = ecr.create_repository(
            repositoryName=name,
            imageScanningConfiguration={"scanOnPush": scan_on_push},
            imageTagMutability=tag_mutability,
        )
        repo = resp["repository"]
        return {
            "created": True,
            "name": repo["repositoryName"],
            "uri": repo["repositoryUri"],
            "scan_on_push": scan_on_push,
            "tag_mutability": tag_mutability,
        }
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


# ─── Security audit ───────────────────────────────────────────────────────────

_SENSITIVE_PORTS = {22, 3389, 3306, 5432, 6379, 27017}


def _finding(severity: str, resource_type: str, resource_id: str, description: str, remediation: str) -> dict:
    return {
        "severity": severity, "resource_type": resource_type, "resource_id": resource_id,
        "description": description, "remediation": remediation,
    }


def _check_open_security_groups(ec2) -> list:
    findings = []
    for sg in ec2.describe_security_groups()["SecurityGroups"]:
        for rule in sg.get("IpPermissions", []):
            proto = rule.get("IpProtocol")
            from_p, to_p = rule.get("FromPort"), rule.get("ToPort")
            is_all_traffic = proto == "-1"
            is_sensitive = is_all_traffic or (
                from_p is not None and to_p is not None
                and any(from_p <= p <= to_p for p in _SENSITIVE_PORTS)
            )
            if not is_sensitive:
                continue
            open_cidrs  = [r["CidrIp"] for r in rule.get("IpRanges", []) if r.get("CidrIp") == "0.0.0.0/0"]
            open_cidrs += [r["CidrIpv6"] for r in rule.get("Ipv6Ranges", []) if r.get("CidrIpv6") == "::/0"]
            if open_cidrs:
                port_desc = "all traffic" if is_all_traffic else f"port(s) {from_p}-{to_p}"
                findings.append(_finding(
                    "HIGH", "SecurityGroup", sg["GroupId"],
                    f"Security group {sg.get('GroupName', sg['GroupId'])} allows {port_desc} from {', '.join(open_cidrs)}",
                    "Restrict the CIDR range to known IPs or a VPN/bastion; remove the 0.0.0.0/0 or ::/0 rule.",
                ))
    return findings


def _check_public_s3_buckets(s3) -> list:
    findings = []
    for b in s3.list_buckets().get("Buckets", []):
        name = b["Name"]
        is_public, reason = False, None
        try:
            status = s3.get_bucket_policy_status(Bucket=name)
            if status["PolicyStatus"]["IsPublic"]:
                is_public, reason = True, "bucket policy allows public access"
        except Exception:
            pass  # No bucket policy is the normal/expected case for most buckets
        try:
            pab = s3.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]
            if not all(pab.values()) and not reason:
                is_public, reason = True, "public access block is not fully enabled"
        except Exception:
            pass  # No public access block configuration is also normal/expected
        if is_public:
            findings.append(_finding(
                "HIGH", "S3Bucket", name,
                f"Bucket {name} may be publicly accessible ({reason}).",
                "Enable S3 Block Public Access and review the bucket policy/ACLs.",
            ))
    return findings


def _check_iam_mfa_and_keys(iam) -> list:
    findings = []
    for page in iam.get_paginator("list_users").paginate():
        for u in page["Users"]:
            username = u["UserName"]
            if _has_login_profile(iam, username):
                if not iam.list_mfa_devices(UserName=username).get("MFADevices", []):
                    findings.append(_finding(
                        "HIGH", "IAMUser", username,
                        f"User {username} has console login enabled but no MFA device.",
                        "Enable an MFA device for this user immediately.",
                    ))
            for k in iam.list_access_keys(UserName=username).get("AccessKeyMetadata", []):
                if k["Status"] != "Active":
                    continue
                age = (datetime.now(timezone.utc) - k["CreateDate"]).days
                if age > 90:
                    findings.append(_finding(
                        "MEDIUM", "IAMAccessKey", k["AccessKeyId"],
                        f"Access key {k['AccessKeyId']} for user {username} is {age} days old.",
                        "Rotate this access key; delete it if unused.",
                    ))
    return findings


def _check_public_rds(rds) -> list:
    findings = []
    for db in rds.describe_db_instances().get("DBInstances", []):
        if db.get("PubliclyAccessible"):
            findings.append(_finding(
                "HIGH", "RDSInstance", db["DBInstanceIdentifier"],
                f"RDS instance {db['DBInstanceIdentifier']} is publicly accessible.",
                "Set PubliclyAccessible to false and use a bastion/VPN or VPC peering instead.",
            ))
    return findings


def _check_root_mfa(iam) -> list:
    s = iam.get_account_summary()["SummaryMap"]
    if s.get("AccountMFAEnabled", 0) != 1:
        return [_finding(
            "HIGH", "RootAccount", "root",
            "The AWS account root user does not have MFA enabled.",
            "Enable MFA on the root account immediately -- it has unrestricted access.",
        )]
    return []


async def _security_audit(params: dict) -> dict:
    def _run():
        ec2, s3, iam, rds = boto3.client("ec2"), boto3.client("s3"), boto3.client("iam"), boto3.client("rds")
        checks = [
            ("open_security_groups", lambda: _check_open_security_groups(ec2)),
            ("public_s3_buckets",     lambda: _check_public_s3_buckets(s3)),
            ("iam_mfa_and_keys",      lambda: _check_iam_mfa_and_keys(iam)),
            ("public_rds_instances",  lambda: _check_public_rds(rds)),
            ("root_account_mfa",      lambda: _check_root_mfa(iam)),
        ]
        findings, skipped = [], []
        for check_name, fn in checks:
            try:
                findings.extend(fn())
            except Exception as e:
                logger.warning(f"security_audit check '{check_name}' failed: {e}")
                skipped.append({"check": check_name, "reason": str(e)})

        severity_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        findings.sort(key=lambda f: severity_rank.get(f["severity"], 3))
        counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for f in findings:
            counts[f["severity"]] = counts.get(f["severity"], 0) + 1

        return {
            "total_findings": len(findings),
            "counts_by_severity": counts,
            "findings": findings[:30],
            "checks_skipped": skipped,
        }
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)
