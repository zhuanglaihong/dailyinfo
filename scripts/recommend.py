#!/usr/bin/env python3
"""Generate a daily topic digest; never send messages."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import paths
from publication import PublicationStore, sanitize_error
from recommendations import (
    AnalysisCache,
    atomic_json,
    collect_publications,
    generate_report,
    load_topics,
    report_markdown,
)


def main(
    date_value=None, topics_path=None, *, no_ai=False, force=False, search_arxiv=False
):
    try:
        target_date = (
            date_value or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        )
        if datetime.strptime(target_date, "%Y-%m-%d").date().isoformat() != target_date:
            raise ValueError("date must be YYYY-MM-DD")
        model, topics = load_topics(
            Path(topics_path or paths.PROJECT_ROOT / "config/topics.json")
        )
        if not topics:
            raise ValueError("enable at least one topic in config/topics.json")
        items = collect_publications(PublicationStore(), target_date)
        if search_arxiv:
            from topic_search import search_topics

            items.extend(search_topics(topics, target_date))
        if not items:
            print(
                f"No canonical candidates for {target_date}; run dailyinfo run or use --search-arxiv."
            )
            return 0
        from run_pipelines import call_ai

        report = generate_report(
            items,
            topics,
            target_date,
            model,
            AnalysisCache(paths.STATE_DIR / "recommendations.sqlite3"),
            call_ai,
            no_ai=no_ai,
            force=force,
        )
        root = paths.WORKSPACE_ROOT / "recommendations" / target_date
        # Rule previews can never overwrite the AI report used for delivery.
        name = "preview" if no_ai else "daily"
        atomic_json(root / f"{name}.json", report)
        (root / f"{name}.md").write_text(report_markdown(report), encoding="utf-8")
        count = sum(len(topic["items"]) for topic in report["topics"])
        print(f"{count} recommendations: {root / f'{name}.md'}")
        return 0
    except Exception as exc:
        print(f"Recommendation failed: {sanitize_error(exc)}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date")
    parser.add_argument("--topics")
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--search-arxiv", action="store_true")
    args = parser.parse_args()
    raise SystemExit(
        main(
            args.date,
            args.topics,
            no_ai=args.no_ai,
            force=args.force,
            search_arxiv=args.search_arxiv,
        )
    )
