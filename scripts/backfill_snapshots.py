"""Backfill the daily issue snapshots that the cumulative flow diagram reads.

    python -m scripts.backfill_snapshots --days 90                  # every workspace
    python -m scripts.backfill_snapshots --workspace <id> --days 30

The report worker writes yesterday's snapshot every night (workers/report_worker.py). This
fills the days before it was running — once after deploying reports, or after a long outage.
Each day is replayed from `issue_events`, so it is as accurate as the history is complete.
Re-running is safe: a day already written is replaced with the same rows.
"""
from __future__ import annotations

import argparse
from datetime import timedelta

import db.session as db_session
from models.workspace import Workspace
from services.reports import settings as report_settings
from services.reports import snapshot
from utils import report_math as rm
from utils import time_tracking as tt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=90, help="days back from yesterday")
    parser.add_argument("--workspace", help="one workspace id; default: all of them")
    args = parser.parse_args()
    if not 1 <= args.days <= 366:
        parser.error("--days must be between 1 and 366")
    with db_session.SessionLocal() as db:
        query = db.query(Workspace).filter(Workspace.deleted == 0)
        if args.workspace:
            query = query.filter(Workspace.id == args.workspace)
        for workspace in query.all():
            zone_name = report_settings.settings_for(workspace).timezone
            end = rm.local_today(tt.utc_now(), rm.zone(zone_name)) - timedelta(days=1)
            start = end - timedelta(days=args.days - 1)
            written = snapshot.backfill(db, workspace.id, start, end, zone_name)
            db.commit()
            print(f"{workspace.name}: {written} day(s) written ({start} to {end})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
