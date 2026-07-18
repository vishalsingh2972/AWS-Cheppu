"""
Cost Service – fetches AWS cost reports via Cost Explorer.
"""
import boto3
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class CostService:
    def __init__(self):
        self.client = boto3.client("ce", region_name="us-east-1")  # Cost Explorer only in us-east-1

    async def get_cost_report(self, params: dict) -> dict:
        period = params.get("period", "month").lower()

        today = datetime.utcnow().date()
        if period == "week":
            start = (today - timedelta(days=7)).isoformat()
        else:
            # Month-to-date
            start = today.replace(day=1).isoformat()
        end = today.isoformat()

        try:
            # Total cost
            total_resp = self.client.get_cost_and_usage(
                TimePeriod={"Start": start, "End": end},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
            )

            # By service
            service_resp = self.client.get_cost_and_usage(
                TimePeriod={"Start": start, "End": end},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )

            total = sum(
                float(r["Total"]["UnblendedCost"]["Amount"])
                for r in total_resp["ResultsByTime"]
            )
            currency = "USD"
            if total_resp["ResultsByTime"]:
                currency = total_resp["ResultsByTime"][0]["Total"]["UnblendedCost"].get("Unit", "USD")

            # Aggregate by service
            service_costs: dict[str, float] = {}
            for result in service_resp["ResultsByTime"]:
                for group in result["Groups"]:
                    svc = group["Keys"][0]
                    amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                    service_costs[svc] = service_costs.get(svc, 0) + amount

            # Sort by cost descending
            sorted_services = sorted(service_costs.items(), key=lambda x: x[1], reverse=True)

            period_label = "Month-to-date" if period == "month" else "Last 7 days"
            lines = [
                f"{'Cost Report':─<50}",
                f"",
                f"  Period:  {period_label} ({start} → {end})",
                f"  Total:   {currency} {total:.2f}",
                f"",
                f"  Top Services:",
            ]

            for svc, cost in sorted_services[:10]:
                pct = (cost / total * 100) if total > 0 else 0
                bar = "█" * int(pct / 5)
                lines.append(f"  {svc[:35]:<35}  ${cost:>8.2f}  {bar}")

            if len(sorted_services) > 10:
                others = sum(c for _, c in sorted_services[10:])
                lines.append(f"  {'Other':<35}  ${others:>8.2f}")

            display = "\n".join(lines)

            top = sorted_services[:3]
            top_str = ", ".join(
                f"{svc.split(' ')[0]} at ${cost:.2f}"
                for svc, cost in top
            )
            voice = (
                f"Your AWS cost for the {period_label.lower()} is ${total:.2f}. "
                + (f"Your top services are {top_str}." if top_str else "")
            )
            return {"display": display, "voice_summary": voice}

        except self.client.exceptions.DataUnavailableException:
            msg = "Cost data is not yet available. It may take up to 24 hours for new data to appear."
            return {"display": f"ℹ️  {msg}", "voice_summary": msg}
        except Exception as e:
            logger.error(f"Cost Explorer error: {e}")
            raise
