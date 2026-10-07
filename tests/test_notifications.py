"""Transport rendering, secret isolation, idempotency and ambiguous delivery recovery."""

from copy import deepcopy
import json
import sqlite3

from click.testing import CliRunner
import pytest

from notifications import (
    DeliveryRejected,
    Outbox,
    feishu_payload,
    load_routes,
    report_html,
    route_value,
    send_email,
    send_feishu,
)
from recommendations import atomic_json


@pytest.fixture
def report():
    return {
        "version": 1,
        "date": "2026-10-07",
        "mode": "ai",
        "topics": [
            {
                "id": "water",
                "name": "水文",
                "items": [
                    {
                        "id": "p1",
                        "category": "papers",
                        "title": "<script>alert(1)</script>",
                        "summary": "A useful summary",
                        "reason": "相关方法",
                        "relevance": 80,
                        "source": {"name": "Nature", "url": "https://example.com/p1"},
                        "source_published_at": "2026-10-07T01:00:00Z",
                        "retrieved_at": "2026-10-07T02:00:00Z",
                    }
                ],
            }
        ],
    }


def route(sink="email"):
    return {
        "id": "test",
        "sink": sink,
        "categories": ["papers"],
        "target_env": "TARGET",
    }


def test_html_escapes_source_content_and_keeps_original_link(report):
    output = report_html(report)
    assert "<script>" not in output
    assert "&lt;script&gt;" in output
    assert "href='https://example.com/p1'" in output


def test_dev_target_never_falls_back_to_prod():
    env = {"DAILYINFO_ENV": "dev", "TARGET": "prod-secret"}
    assert route_value(env, "TARGET") == ""
    env["TARGET_DEV"] = "dev-target"
    assert route_value(env, "TARGET") == "dev-target"


def test_feishu_signing_and_business_error_handling(report, monkeypatch):
    import base64
    import hashlib
    import hmac

    monkeypatch.setattr("notifications.time.time", lambda: 100)
    payload = feishu_payload(report, "secret")
    assert (
        payload["sign"]
        == base64.b64encode(
            hmac.new(b"100\nsecret", b"", hashlib.sha256).digest()
        ).decode()
    )

    class Response:
        status_code = 200

        def json(self):
            return {"code": 19021}

    with pytest.raises(DeliveryRejected, match="19021"):
        send_feishu(
            report,
            "https://open.feishu.cn/open-apis/bot/v2/hook/test",
            post=lambda *a, **k: Response(),
        )


def test_feishu_rejects_oversized_payload_before_network(report):
    report["topics"][0]["items"][0]["summary"] = "中" * 10000

    def forbidden(*args, **kwargs):
        pytest.fail("oversized digest reached network")

    with pytest.raises(ValueError, match="19 KB"):
        send_feishu(
            report, "https://open.feishu.cn/open-apis/bot/v2/hook/test", post=forbidden
        )


def test_email_uses_starttls_and_alternative_html(report, monkeypatch):
    calls = []

    class SMTP:
        def __init__(self, *args, **kwargs):
            calls.append("connect")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def starttls(self, **kwargs):
            calls.append("tls")

        def login(self, user, password):
            calls.append("login")

        def send_message(self, message):
            calls.append(message)
            return {}

    monkeypatch.setattr("notifications.smtplib.SMTP", SMTP)
    send_email(
        report,
        "reader@example.com",
        {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USER": "sender@example.com",
            "SMTP_PASSWORD": "secret",
        },
    )
    assert calls[:3] == ["connect", "tls", "login"]
    assert calls[3].is_multipart()
    assert calls[3].get_body(preferencelist=("html",))


def test_outbox_independent_destinations_content_and_channel_retry(report, tmp_path):
    outbox = Outbox(tmp_path / "outbox.db")
    calls = []

    def sent():
        calls.append(True)

    assert outbox.deliver(report, route(), "one@example.com", sent) == "success"
    assert outbox.deliver(report, route(), "one@example.com", sent) == "skipped"
    assert outbox.deliver(report, route(), "two@example.com", sent) == "success"

    def rejected():
        raise DeliveryRejected("do not persist secret")

    assert (
        outbox.deliver(report, route("feishu"), "secret-webhook", rejected) == "failed"
    )
    assert outbox.deliver(report, route("feishu"), "secret-webhook", sent) == "success"
    changed = deepcopy(report)
    changed["topics"][0]["items"][0]["reason"] = "New reason"
    assert outbox.deliver(changed, route(), "one@example.com", sent) == "success"
    assert len(calls) == 4
    contents = json.dumps(outbox.status())
    assert "secret-webhook" not in contents and "one@example.com" not in contents


def test_repeated_arxiv_fetch_does_not_resend_known_paper(report, tmp_path):
    outbox = Outbox(tmp_path / "outbox.db")
    assert (
        outbox.deliver(report, route(), "reader@example.com", lambda: None) == "success"
    )
    report["topics"][0]["items"][0]["retrieved_at"] = "2026-10-07T03:00:00Z"
    report["topics"][0]["candidate_count"] = 999
    assert (
        outbox.deliver(
            report,
            route(),
            "reader@example.com",
            lambda: pytest.fail("resent unchanged paper"),
        )
        == "skipped"
    )


def test_timeout_stays_pending_and_blocks_even_force_until_reset(report, tmp_path):
    outbox = Outbox(tmp_path / "outbox.db")

    def timed_out():
        raise TimeoutError("secret URL")

    assert outbox.deliver(report, route(), "reader@example.com", timed_out) == "pending"
    with pytest.raises(RuntimeError, match="pending"):
        outbox.deliver(report, route(), "reader@example.com", lambda: None, force=True)
    key = outbox.status()[0]["key"]
    outbox.reset(key)
    assert (
        outbox.deliver(report, route(), "reader@example.com", lambda: None) == "success"
    )


def test_concurrent_claim_cannot_send_twice(report, tmp_path):
    first = Outbox(tmp_path / "outbox.db")
    second = Outbox(tmp_path / "outbox.db")

    def transport():
        with pytest.raises(RuntimeError, match="pending"):
            second.deliver(
                report,
                route(),
                "reader@example.com",
                lambda: pytest.fail("concurrent send"),
            )

    assert first.deliver(report, route(), "reader@example.com", transport) == "success"


def test_cross_day_item_receipts_are_per_destination(report, tmp_path):
    outbox = Outbox(tmp_path / "outbox.db")
    assert (
        outbox.deliver(report, route(), "reader@example.com", lambda: None) == "success"
    )
    tomorrow = deepcopy(report)
    tomorrow["date"] = "2026-10-08"
    assert outbox.unseen_report(tomorrow, route(), "reader@example.com")["topics"] == []
    assert outbox.unseen_report(tomorrow, route(), "other@example.com")["topics"]
    tomorrow["topics"][0]["items"][0]["summary"] = "Updated research finding"
    assert outbox.unseen_report(tomorrow, route(), "reader@example.com")["topics"]


def test_pending_destination_blocks_different_digest(report, tmp_path):
    outbox = Outbox(tmp_path / "outbox.db")

    def uncertain():
        raise TimeoutError()

    outbox.deliver(report, route(), "reader@example.com", uncertain)
    changed = deepcopy(report)
    changed["date"] = "2026-10-08"
    with pytest.raises(RuntimeError, match="destination"):
        outbox.deliver(
            changed,
            route(),
            "reader@example.com",
            lambda: pytest.fail("overlapping send"),
        )


def test_confirm_uncertain_delivery_preserves_item_receipts_and_releases_target(
    report, tmp_path
):
    outbox = Outbox(tmp_path / "outbox.db")

    def uncertain():
        raise TimeoutError()

    assert outbox.deliver(report, route(), "reader@example.com", uncertain) == "pending"
    key = outbox.status()[0]["key"]
    outbox.confirm(key)
    assert outbox.status()[0]["status"] == "success"
    tomorrow = deepcopy(report)
    tomorrow["date"] = "2026-10-08"
    assert not outbox.unseen_report(tomorrow, route(), "reader@example.com")["topics"]
    tomorrow["topics"][0]["items"][0]["summary"] = "New research finding"
    assert (
        outbox.deliver(tomorrow, route(), "reader@example.com", lambda: None)
        == "success"
    )


def test_routed_dry_run_does_not_write_outbox_or_need_credentials(
    report, tmp_path, monkeypatch, capsys
):
    import paths
    import deliver

    monkeypatch.setattr(paths, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(deliver, "read_environment", lambda: {})
    atomic_json(tmp_path / "recommendations/2026-10-07/daily.json", report)
    assert deliver.main(date_value="2026-10-07", dry_run=True) == 0
    assert "email_papers" in capsys.readouterr().out
    assert not (tmp_path / "state/notifications.sqlite3").exists()


def test_rule_preview_cannot_be_sent(report, tmp_path, monkeypatch):
    import paths
    import deliver

    monkeypatch.setattr(paths, "WORKSPACE_ROOT", tmp_path)
    report["mode"] = "rules"
    atomic_json(tmp_path / "recommendations/2026-10-07/daily.json", report)
    assert deliver.main(date_value="2026-10-07", dry_run=True) == 1


def test_failed_route_does_not_block_other_route(report, tmp_path, monkeypatch):
    import paths
    import deliver

    monkeypatch.setattr(paths, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(
        deliver,
        "read_environment",
        lambda: {
            "TARGET": "reader@example.com",
            "FEISHU": "https://open.feishu.cn/open-apis/bot/v2/hook/test",
        },
    )
    calls = []

    def rejected(*args):
        raise DeliveryRejected("error")

    monkeypatch.setattr(deliver, "send_email", rejected)
    monkeypatch.setattr(deliver, "send_feishu", lambda *args: calls.append("feishu"))
    routes = tmp_path / "routes.json"
    atomic_json(
        routes,
        {
            "version": 1,
            "routes": [
                route(),
                {**route("feishu"), "id": "second", "target_env": "FEISHU"},
            ],
        },
    )
    atomic_json(tmp_path / "recommendations/2026-10-07/daily.json", report)
    assert deliver.main(date_value="2026-10-07", routes_path=routes) == 1
    assert calls == ["feishu"]


def test_cli_forwards_new_commands_and_email_publish(monkeypatch):
    import cli

    calls = []

    class Completed:
        returncode = 0

    monkeypatch.setattr(
        cli.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd) or Completed()
    )
    runner = CliRunner()
    assert (
        runner.invoke(cli.cli, ["recommend", "--no-ai", "--search-arxiv"]).exit_code
        == 0
    )
    assert "--no-ai" in calls[-1] and "--search-arxiv" in calls[-1]
    assert (
        runner.invoke(cli.cli, ["deliver", "--sink", "feishu", "--dry-run"]).exit_code
        == 0
    )
    assert calls[-1][-1] == "--dry-run"
    assert runner.invoke(cli.cli, ["publish", "--sink", "email"]).exit_code == 0
    assert "publications" in calls[-1]


def test_windows_or_posix_lock_blocks_other_process(tmp_path):
    import os
    import subprocess
    import sys
    from publication.filelock import fcntl

    lock_path = tmp_path / "lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        code = """import sys
from publication.filelock import fcntl
with open(sys.argv[1], 'a+') as stream:
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
    sys.exit(1)
"""
        result = subprocess.run(
            [sys.executable, "-c", code, str(lock_path)],
            env={
                **os.environ,
                "PYTHONPATH": str(
                    __import__("pathlib").Path(__file__).parents[1] / "scripts"
                ),
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def test_install_does_not_require_discord_for_email_users(tmp_path, monkeypatch):
    import cli

    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_API_KEY=sk-test-real\n", encoding="utf-8")
    monkeypatch.setattr(cli, "ENV_FILE", env)

    class Completed:
        returncode = 0

    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: Completed())
    result = CliRunner().invoke(cli.cli, ["install"])
    assert result.exit_code == 0, result.output
    assert "DISCORD_BOT_TOKEN" not in result.output


def test_ordinary_rss_fetch_exposes_source_abstract(rss_db):
    from datasource import RSSDataSource, build_feed_url_map

    rss_db.execute(
        "UPDATE entry SET content='<p>Original hydrology abstract</p>' WHERE id_feed=1"
    )
    full_map, base_map = build_feed_url_map(rss_db)
    source = RSSDataSource(
        {"name": "test", "url": "https://example.com/feed.xml", "type": "rss"},
        {},
        db=rss_db,
        full_map=full_map,
        base_map=base_map,
    )
    items = source.fetch()
    assert items[0].content == "Original hydrology abstract"
    assert "来源摘要：Original hydrology abstract" in source.format_items(items)
