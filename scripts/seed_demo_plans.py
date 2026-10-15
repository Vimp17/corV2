"""Give the demo wells some planned dates so the ППР (ДДО) chart is not empty.

    python scripts/seed_demo_plans.py --api http://localhost:8000 [--api-key KEY]

For demonstrations only: it sets operator fields (plan date, Промайн document) of the
DEMO-* wells through the public PATCH /api/v1/rating/wells/{id} endpoint, the same call the
dashboard makes. Dates are relative to each well's deadline, so the chart shows a mix of
plans on time, a plan later than the deadline and wells still waiting for a decision.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

import requests

# well -> (days relative to the deadline, Промайн document status or None)
PLANS = {
    "DEMO-001": (-9, "created"),   # planned well before the deadline, document ready
    "DEMO-003": (5, None),         # planned after the deadline -> "Позже срока"
    "DEMO-004": (-16, None),
    "DEMO-005": (-4, None),
    "DEMO-009": (-25, None),
    "DEMO-008": (-10, None),
    "DEMO-014": (3, None),         # late
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--api-key", default="")
    args = parser.parse_args()
    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    wells = requests.get(f"{args.api}/api/v1/rating", headers=headers, timeout=30).json()["wells"]
    deadlines = {well["well_id"]: well.get("deadline") for well in wells}
    for well_id, (shift, document) in PLANS.items():
        deadline = deadlines.get(well_id)
        if not deadline:
            print(f"{well_id}: нет в рейтинге или нет срока — пропущено")
            continue
        body = {"plan_date": (date.fromisoformat(deadline) + timedelta(days=shift)).isoformat()}
        if document:
            body["promine_status"] = document
        response = requests.patch(f"{args.api}/api/v1/rating/wells/{well_id}", json=body, headers=headers, timeout=30)
        print(f"{well_id}: {body} -> HTTP {response.status_code}")


if __name__ == "__main__":
    main()
