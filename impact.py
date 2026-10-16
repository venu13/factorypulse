"""Illustrative downtime-avoidance estimate. Every input is an editable ASSUMPTION; nothing here is measured."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass
class ImpactAssumptions:
    downtime_cost_per_hour: float = 8000.0  # USD, plant-specific
    unplanned_failure_downtime_h: float = 12.0  # hours lost if the asset runs to failure
    planned_repair_downtime_h: float = 3.0  # hours lost for a planned intervention
    detection_lead_time_h: float = 36.0  # how early the system flags the issue before functional failure
    required_planning_window_h: float = 8.0  # lead time a crew needs to plan parts + LOTO
    p_failure_prevented: float = 0.8  # share of acted-on alerts that prevent the failure


def estimate_impact(statuses: Dict[str, str], a: ImpactAssumptions) -> Dict:
    """Per-asset and total avoided-downtime estimate for assets currently in WARNING/CRITICAL."""
    acted = a.detection_lead_time_h >= a.required_planning_window_h
    saved_h_each = max(a.unplanned_failure_downtime_h - a.planned_repair_downtime_h, 0.0) * a.p_failure_prevented
    rows: List[Dict] = []
    for machine, status in statuses.items():
        if status == "HEALTHY":
            continue
        hours = saved_h_each if acted else 0.0
        rows.append({"machine": machine, "status": status, "downtime_avoided_h": round(hours, 1),
                     "cost_avoided_usd": round(hours * a.downtime_cost_per_hour, 0)})
    return {
        "acted_on": acted, "rows": rows,
        "total_hours": round(sum(r["downtime_avoided_h"] for r in rows), 1),
        "total_cost": round(sum(r["cost_avoided_usd"] for r in rows), 0),
    }
