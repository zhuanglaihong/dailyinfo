#!/usr/bin/env python3
"""Deliver daily recommendations or canonical publications to configured routes."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import paths
from notifications import (
    Outbox,
    load_routes,
    read_environment,
    route_value,
    send_discord,
    send_email,
    send_feishu,
)
from publication import PublicationStore, sanitize_error
from recommendations import (
    collect_publications,
    filter_report,
    report_markdown,
    validate_report,
)


def publication_report(target_date):
    items = collect_publications(PublicationStore(), target_date)
    return {
        "version": 1,
        "date": target_date,
        "mode": "publication",
        "topics": [
            {
                "id": "publications",
                "name": "每日科研情报",
                "items": [
                    {
                        **item,
                        "relevance": 0,
                        "reason": item.get("why_it_matters")
                        or "订阅来源更新，尚未进行主题相关性评分",
                    }
                    for item in items
                ],
            }
        ],
    }


def main(
    sink="all",
    date_value=None,
    categories=None,
    *,
    routes_path=None,
    source="recommendations",
    dry_run=False,
    force=False,
):
    try:
        target_date = (
            date_value or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        )
        if datetime.strptime(target_date, "%Y-%m-%d").date().isoformat() != target_date:
            raise ValueError("date must be YYYY-MM-DD")
        routes = load_routes(
            Path(routes_path or paths.PROJECT_ROOT / "config/delivery.json")
        )
        routes = [route for route in routes if sink == "all" or route["sink"] == sink]
        if not routes:
            raise ValueError(f"no enabled routes for {sink}; edit config/delivery.json")
        if source == "publications":
            report = publication_report(target_date)
        else:
            report_path = (
                paths.WORKSPACE_ROOT / "recommendations" / target_date / "daily.json"
            )
            if not report_path.exists():
                raise ValueError("no AI daily digest; run dailyinfo recommend first")
            report = validate_report(
                json.loads(report_path.read_text(encoding="utf-8"))
            )
            if report["date"] != target_date or report.get("mode") != "ai":
                raise ValueError(
                    "digest date/mode mismatch; generate an AI digest first"
                )
        env = read_environment()
    except Exception as exc:
        print(f"Delivery setup failed: {sanitize_error(exc)}")
        return 1
    failures = 0
    for route in routes:
        try:
            selected = route["categories"]
            if categories:
                chosen = categories.split(",")
                from publication.models import CANONICAL_CATEGORIES

                if any(cat not in CANONICAL_CATEGORIES for cat in chosen):
                    raise ValueError("unsupported --categories")
                selected = [cat for cat in selected if cat in chosen]
            filtered = filter_report(report, selected)
            if not filtered["topics"]:
                print(f"{route['id']}: no selected items, skipped")
                continue
            if dry_run:
                print(f"[{route['id']} / {route['sink']}]\n{report_markdown(filtered)}")
                continue
            target = route_value(env, route["target_env"])
            if not target:
                raise ValueError(
                    f"configure {route['target_env']} (use _DEV/_STAGING in those environments)"
                )
            kind = route["sink"]
            outbox = Outbox(paths.STATE_DIR / "notifications.sqlite3")
            if not force:
                filtered = outbox.unseen_report(filtered, route, target)
                if not filtered["topics"]:
                    print(
                        f"{route['id']}: unchanged items already sent on an earlier date, skipped"
                    )
                    continue
            secret = (
                route_value(env, route["secret_env"]) if route.get("secret_env") else ""
            )
            transport = {
                "email": lambda: send_email(filtered, target, env),
                "feishu": lambda: send_feishu(filtered, target, secret),
                "discord": lambda: send_discord(filtered, target, env),
            }[kind]
            status = outbox.deliver(filtered, route, target, transport, force=force)
            print(f"{route['id']}: {status}")
            if status in {"failed", "pending"}:
                failures += 1
                print(
                    "Check configuration/recipient, then dailyinfo delivery-status for recovery details."
                )
        except Exception as exc:
            failures += 1
            print(f"{route['id']}: {sanitize_error(exc)}")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sink", choices=["email", "feishu", "discord", "all"], default="all"
    )
    parser.add_argument("--date")
    parser.add_argument("--categories")
    parser.add_argument("--routes")
    parser.add_argument(
        "--source",
        choices=["recommendations", "publications"],
        default="recommendations",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    raise SystemExit(
        main(
            args.sink,
            args.date,
            args.categories,
            routes_path=args.routes,
            source=args.source,
            dry_run=args.dry_run,
            force=args.force,
        )
    )
