"""Recommendation ranking, cache invalidation, source-time and safe preview tests."""

from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from recommendations import (
    AnalysisCache,
    Topic,
    analyze_candidates,
    atomic_json,
    filter_report,
    generate_report,
    load_topics,
    parse_analysis,
    select_candidates,
    term_matches,
)


@pytest.fixture
def topic():
    return Topic(
        "water",
        "水文 AI",
        "机器学习与水文预测",
        ["hydrology", "AI"],
        ["营销"],
        ["papers", "arxiv", "ai_news"],
        72,
        60,
        10,
        {"papers": 1, "arxiv": 1, "ai_news": 1},
        [],
    )


def candidate(ident="p1", *, title="AI for hydrology", category="papers", url=None):
    return {
        "id": ident,
        "title": title,
        "category": category,
        "summary": "公开数据上的预测方法",
        "source": {
            "name": "journal",
            "url": url or f"https://example.com/{ident}",
            "external_id": None,
        },
        "source_published_at": "2026-10-07T01:00:00Z",
        "retrieved_at": "2026-10-07T02:00:00Z",
        "tags": [],
    }


def fake_ai(prompt, **kwargs):
    payload = json.loads(prompt[prompt.index('\n{"topic"') + 1 :])
    return json.dumps(
        {
            "items": [
                {
                    "id": item["id"],
                    "relevance": 90 if item["id"] == "p2" else 75,
                    "importance": 80,
                    "reason": "与水文预测的方法研究直接相关",
                }
                for item in payload["candidates"]
            ]
        }
    )


def test_topic_config_rejects_paths_duplicates_and_bad_limits(tmp_path):
    config = {
        "version": 1,
        "topics": [{"id": "../unsafe", "name": "x", "description": "y"}],
    }
    path = tmp_path / "topics.json"
    atomic_json(path, config)
    with pytest.raises(ValueError, match="ids"):
        load_topics(path)
    config["topics"][0].update(id="safe", daily_limits={"papers": -1})
    atomic_json(path, config)
    with pytest.raises(ValueError, match="limit"):
        load_topics(path)


def test_term_boundaries_do_not_match_ai_inside_rain():
    assert not term_matches("rainfall said", "AI")
    assert term_matches("AI-based forecasting", "AI")
    assert term_matches("水文预测模型", "水文")


def test_freshness_exclusions_unknown_date_and_arxiv_revision_dedup(topic):
    old = {**candidate("old"), "source_published_at": "2026-09-01T00:00:00Z"}
    unknown = {**candidate("unknown"), "source_published_at": None}
    v1 = candidate("a1", url="https://arxiv.org/abs/2610.12345v1")
    v2 = candidate("a2", url="https://arxiv.org/pdf/2610.12345v2.pdf")
    result = select_candidates(
        [old, unknown, candidate("ad", title="AI 营销"), v1, v2], topic, "2026-10-07"
    )
    assert len(result) == 2
    assert any(item["id"] == "unknown" for item in result)


def test_semantically_related_items_need_not_have_keyword_and_limits_apply(
    topic, tmp_path
):
    cache = AnalysisCache(tmp_path / "cache.db")
    report = generate_report(
        [candidate(), candidate("p2", title="River discharge forecasting")],
        [topic],
        "2026-10-07",
        "model",
        cache,
        fake_ai,
    )
    items = report["topics"][0]["items"]
    assert [item["id"] for item in items] == ["p2"]
    assert items[0]["reason"]


def test_ai_cache_reuses_scores_and_invalidates_on_topic_or_content(topic, tmp_path):
    calls = []

    def ai(prompt, **kwargs):
        calls.append(prompt)
        return fake_ai(prompt, **kwargs)

    cache = AnalysisCache(tmp_path / "cache.db")
    item = candidate()
    for _ in range(2):
        analyze_candidates([item], topic, "model", cache, ai)
    assert len(calls) == 1
    analyze_candidates([{**item, "summary": "更新摘要"}], topic, "model", cache, ai)
    analyze_candidates([item], replace(topic, description="新方向"), "model", cache, ai)
    assert len(calls) == 3


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [{"id": "alien", "relevance": 70, "importance": 50, "reason": "x"}],
        [{"id": "p1", "relevance": True, "importance": 50, "reason": "x"}],
    ],
)
def test_invalid_ai_results_fail_closed(entries):
    with pytest.raises(ValueError):
        parse_analysis(json.dumps({"items": entries}), ["p1"])


def test_no_ai_preview_does_not_call_ai_or_pollute_cache(topic, tmp_path):
    cache = AnalysisCache(tmp_path / "cache.db")

    def forbidden(*args, **kwargs):
        pytest.fail("preview called AI")

    report = generate_report(
        [candidate()], [topic], "2026-10-07", "model", cache, forbidden, no_ai=True
    )
    assert report["mode"] == "rules"
    assert report["topics"][0]["items"][0]["mode"] == "rules"
    assert not filter_report(report, ["code"])["topics"]


def test_rule_preview_never_overwrites_ai_report(tmp_path, monkeypatch):
    import paths
    import recommend

    monkeypatch.setattr(paths, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(recommend, "collect_publications", lambda *args: [candidate()])
    daily = tmp_path / "recommendations/2026-10-07/daily.json"
    atomic_json(daily, {"preserved": True})
    assert recommend.main("2026-10-07", no_ai=True) == 0
    assert json.loads(daily.read_text()) == {"preserved": True}
    assert daily.with_name("preview.json").exists()


def test_arxiv_parser_keeps_abstract_and_rejects_error_entry():
    from topic_search import parse_arxiv

    xml = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <id>http://arxiv.org/abs/2610.12345v2</id><title>A physics-informed model</title>
    <summary>Original abstract.</summary><published>2026-10-07T01:00:00Z</published>
    <author><name>Alice</name></author><category term="cs.LG"/></entry></feed>"""
    items = parse_arxiv(xml, datetime.now(timezone.utc))
    assert items[0]["source"]["external_id"] == "arxiv:2610.12345"
    assert items[0]["summary"] == "Original abstract."
    with pytest.raises(ValueError, match="API error"):
        parse_arxiv(
            xml.replace(
                "http://arxiv.org/abs/2610.12345v2",
                "http://arxiv.org/api/errors#bad_query",
            ),
            datetime.now(timezone.utc),
        )


def test_arxiv_queries_use_date_window_and_rate_limit(topic):
    from topic_search import search_topics

    requests, waits = [], []

    class Response:
        text = '<feed xmlns="http://www.w3.org/2005/Atom"/>'

        def raise_for_status(self):
            pass

    def get(url, **kwargs):
        requests.append(kwargs["params"])
        return Response()

    search_topics(
        [replace(topic, arxiv_queries=['all:"hydrology"', 'all:"physics-informed"'])],
        "2026-10-07",
        get=get,
        sleep=waits.append,
    )
    assert waits == [3]
    assert "submittedDate:" in requests[0]["search_query"]
    assert requests[0]["max_results"] == 50
