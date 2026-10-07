"""Optional bounded arXiv topic search; source abstracts stay separate from AI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import time
import xml.etree.ElementTree as ET

import requests

ATOM = {"a": "http://www.w3.org/2005/Atom"}


def parse_arxiv(content: str, retrieved_at: datetime) -> list[dict]:
    root = ET.fromstring(content)
    items = []
    for entry in root.findall("a:entry", ATOM):
        url = entry.findtext("a:id", "", ATOM).strip()
        match = re.search(
            r"arxiv\.org/abs/((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7}))(?:v\d+)?$",
            url,
        )
        if not match:
            # arXiv also returns Atom error entries; never treat them as papers.
            raise ValueError("arXiv returned an invalid paper id or API error")
        ident = match[1]
        title = " ".join(entry.findtext("a:title", "", ATOM).split())
        summary = " ".join(entry.findtext("a:summary", "", ATOM).split())
        published = entry.findtext("a:published", "", ATOM)
        if not title or not summary or not published:
            raise ValueError("arXiv returned an incomplete paper")
        datetime.fromisoformat(published.replace("Z", "+00:00"))
        items.append(
            {
                "id": "arxiv:" + ident,
                "category": "arxiv",
                "title": title,
                "summary": summary,
                "source": {
                    "name": "arxiv",
                    "url": "https://arxiv.org/abs/" + ident,
                    "external_id": "arxiv:" + ident,
                },
                "authors": [
                    author.findtext("a:name", "", ATOM)
                    for author in entry.findall("a:author", ATOM)
                ],
                "source_published_at": published,
                "retrieved_at": retrieved_at.isoformat(),
                "tags": [
                    cat.attrib["term"]
                    for cat in entry.findall("a:category", ATOM)
                    if "term" in cat.attrib
                ],
                "why_it_matters": None,
            }
        )
    return items


def search_topics(topics, target_date: str, *, get=None, sleep=None) -> list[dict]:
    queries = list(
        dict.fromkeys(query for topic in topics for query in topic.arxiv_queries)
    )
    if len(queries) > 10:
        raise ValueError("at most 10 distinct arxiv_queries are allowed per run")
    items = {}
    end = datetime.fromisoformat(target_date).replace(
        tzinfo=timezone(timedelta(hours=8))
    ) + timedelta(days=1)
    window = max((topic.lookback_hours for topic in topics), default=72)
    start = end - timedelta(hours=window)
    span = f"submittedDate:[{start.astimezone(timezone.utc):%Y%m%d%H%M} TO {end.astimezone(timezone.utc):%Y%m%d%H%M}]"
    for index, query in enumerate(queries):
        if index:
            (sleep or time.sleep)(3)
        response = (get or requests.get)(
            "https://export.arxiv.org/api/query",
            params={
                "search_query": f"({query}) AND {span}",
                "start": 0,
                "max_results": 50,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
            headers={"User-Agent": "DailyInfo/0.2 (topic digest)"},
            timeout=30,
        )
        response.raise_for_status()
        for item in parse_arxiv(response.text, datetime.now(timezone.utc)):
            items[item["id"]] = item
    return list(items.values())
