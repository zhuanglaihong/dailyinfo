"""Digest rendering, email/Feishu/Discord transports and a durable outbox.

Delivery identity includes content and destination. Ambiguous sends stay pending
until an operator explicitly retries them, rather than blindly sending twice.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses
import hashlib
import hmac
import html
import json
from pathlib import Path
import re
import smtplib
import sqlite3
import ssl
import time
from urllib.parse import urlsplit

import requests

from publication.models import CANONICAL_CATEGORIES
from publication.delivery import sanitize_error
from recommendations import (
    dedup_key,
    fingerprint,
    ID_RE,
    report_markdown,
    validate_report,
)


class DeliveryRejected(RuntimeError):
    """Remote service explicitly refused the message; retrying is safe."""


def load_routes(path: Path) -> list[dict]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(cfg, dict)
        or cfg.get("version") != 1
        or not isinstance(cfg.get("routes"), list)
    ):
        raise ValueError("delivery configuration must have version 1 and routes[]")
    ids, routes = set(), []
    for route in cfg["routes"]:
        if not isinstance(route, dict):
            raise ValueError("each route must be an object")
        ident = route.get("id", "")
        if not isinstance(ident, str) or not ID_RE.fullmatch(ident) or ident in ids:
            raise ValueError("route ids must be unique machine names")
        ids.add(ident)
        if type(route.get("enabled", True)) is not bool:
            raise ValueError(f"{ident}.enabled must be boolean")
        if not route.get("enabled", True):
            continue
        if route.get("sink") not in {"email", "feishu", "discord"}:
            raise ValueError(f"{ident}: unsupported sink")
        categories = route.get("categories", list(CANONICAL_CATEGORIES))
        if (
            not isinstance(categories, list)
            or not categories
            or any(cat not in CANONICAL_CATEGORIES for cat in categories)
        ):
            raise ValueError(f"{ident}: invalid categories")
        for key in ("target_env", "secret_env"):
            if key == "secret_env" and key not in route:
                continue
            value = route.get(key, "")
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Z][A-Z0-9_]*", value
            ):
                raise ValueError(f"{ident}: {key} must be an environment variable name")
        routes.append(route)
    return routes


def read_environment() -> dict[str, str]:
    import os
    import paths
    from dotenv import dotenv_values

    values = {
        key: value
        for key, value in dotenv_values(paths.ENV_FILE).items()
        if value is not None
    }
    values.update(os.environ)
    return values


def route_value(env: dict, key: str) -> str:
    # A dev/staging push must never silently fall back to a production target.
    mode = env.get("DAILYINFO_ENV", "prod")
    if mode not in {"prod", "dev", "staging"}:
        raise ValueError("DAILYINFO_ENV must be prod, dev or staging")
    name = key if mode == "prod" else f"{key}_{mode.upper()}"
    return env.get(name, "").strip()


def report_html(report: dict) -> str:
    validate_report(report)
    esc = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'></head><body>",
        f"<h1>DailyInfo 每日精选 · {esc(report['date'])}</h1>",
    ]
    if report.get("mode") == "rules":
        parts.append("<p>关键词规则预览，尚未进行 AI 相关性分析。</p>")
    for topic in report["topics"]:
        parts.append(f"<h2>{esc(topic['name'])}</h2>")
        for item in topic["items"]:
            when = item.get("source_published_at") or (
                "发布时间未知；首次采集：" + item["retrieved_at"]
            )
            parts += [
                f"<h3><a href='{esc(item['source']['url'], quote=True)}'>{esc(item['title'])}</a></h3>",
                f"<p>{esc(item['category'])} · {esc(item['source']['name'])} · 相关性 {item['relevance']}/100</p>",
                f"<p>{esc(when)}</p>",
                f"<p>{esc(item['summary']).replace(chr(10), '<br>')}</p>",
                f"<p><strong>推荐理由：</strong>{esc(item['reason'])}</p>",
            ]
    return "\n".join(parts) + "\n</body></html>"


def email_addresses(target: str) -> list[str]:
    if "\r" in target or "\n" in target:
        raise ValueError("email addresses must not contain line breaks")
    addresses = [address for _, address in getaddresses([target])]
    if not addresses or any(
        not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", address)
        for address in addresses
    ):
        raise ValueError("email target must contain valid comma-separated addresses")
    return sorted(set(address.lower() for address in addresses))


def send_email(report: dict, target: str, env: dict) -> None:
    host, user, password = (
        env.get(key, "").strip() for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")
    )
    sender = env.get("SMTP_FROM", "").strip() or user
    if not host or not user or not password:
        raise ValueError("configure SMTP_HOST, SMTP_USER and SMTP_PASSWORD")
    addresses = email_addresses(target)
    email_addresses(sender)
    security = env.get("SMTP_SECURITY", "starttls").lower()
    if security not in {"starttls", "ssl"}:
        raise ValueError("SMTP_SECURITY must be starttls or ssl")
    port = int(env.get("SMTP_PORT", "") or (465 if security == "ssl" else 587))
    message = EmailMessage()
    message["From"], message["To"] = sender, ", ".join(addresses)
    message["Subject"] = f"DailyInfo 每日精选 · {report['date']}"
    message["Message-ID"] = (
        f"<{fingerprint([report_markdown(report), addresses])}@dailyinfo.local>"
    )
    message.set_content(report_markdown(report))
    message.add_alternative(report_html(report), subtype="html")
    context = ssl.create_default_context()
    factory = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    kwargs = {"context": context} if security == "ssl" else {}
    try:
        with factory(host, port, timeout=30, **kwargs) as server:
            if security == "starttls":
                server.starttls(context=context)
            server.login(user, password)
            refused = server.send_message(message)
            if refused:
                # Some recipients may have accepted it; retry requires review.
                raise RuntimeError(
                    "SMTP partially accepted recipients; delivery needs review"
                )
    except (
        smtplib.SMTPAuthenticationError,
        smtplib.SMTPSenderRefused,
        smtplib.SMTPRecipientsRefused,
    ) as exc:
        raise DeliveryRejected(
            "SMTP rejected authentication, sender or recipients"
        ) from exc


def feishu_payload(report: dict, secret: str = "") -> dict:
    validate_report(report)
    content = []
    if report.get("mode") == "rules":
        content.append([{"tag": "text", "text": "关键词规则预览，尚未进行 AI 分析"}])
    for topic in report["topics"]:
        content.append([{"tag": "text", "text": topic["name"]}])
        for item in topic["items"]:
            content += [
                [{"tag": "a", "text": item["title"], "href": item["source"]["url"]}],
                [
                    {
                        "tag": "text",
                        "text": f"{item['category']} · {item['source']['name']} · 相关性 {item['relevance']}/100",
                    }
                ],
                [{"tag": "text", "text": item["summary"]}],
                [{"tag": "text", "text": "推荐理由：" + item["reason"]}],
                [
                    {
                        "tag": "text",
                        "text": item.get("source_published_at")
                        or "发布时间未知；首次采集：" + item["retrieved_at"],
                    }
                ],
            ]
    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": f"DailyInfo 每日精选 · {report['date']}",
                    "content": content,
                }
            }
        },
    }
    if secret:
        timestamp = str(int(time.time()))
        signing_key = f"{timestamp}\n{secret}".encode()
        payload.update(
            timestamp=timestamp,
            sign=base64.b64encode(
                hmac.new(signing_key, b"", hashlib.sha256).digest()
            ).decode(),
        )
    return payload


def send_feishu(report: dict, target: str, secret: str = "", post=None) -> None:
    parsed = urlsplit(target)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "open.feishu.cn"
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or not parsed.path.startswith("/open-apis/bot/v2/hook/")
        or not parsed.path.removeprefix("/open-apis/bot/v2/hook/")
    ):
        raise ValueError("FEISHU_WEBHOOK_URL must be a Feishu HTTPS custom-bot webhook")
    payload = feishu_payload(report, secret)
    # Send one bounded rich-text message; fail locally rather than partially send.
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 19000:
        raise ValueError(
            "Feishu digest exceeds 19 KB; reduce daily_limits or route categories"
        )
    response = (post or requests.post)(
        target, json=payload, timeout=30, allow_redirects=False
    )
    if 400 <= response.status_code < 500:
        raise DeliveryRejected(f"Feishu rejected HTTP {response.status_code}")
    if response.status_code != 200:
        raise RuntimeError("Feishu HTTP result uncertain; delivery needs review")
    data = response.json()
    if not isinstance(data, dict) or type(data.get("code")) is not int:
        raise RuntimeError("Feishu response has no valid result code")
    if data["code"] != 0:
        raise DeliveryRejected(f"Feishu rejected message, code={data['code']}")


def send_discord(report: dict, target: str, env: dict, post=None) -> None:
    if not re.fullmatch(r"[0-9]+", target):
        raise ValueError("DISCORD_DIGEST_CHANNEL must be a numeric channel id")
    token = env.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("configure DISCORD_BOT_TOKEN")
    content = report_markdown(report)
    chunks = [content[start : start + 1900] for start in range(0, len(content), 1900)]
    sent = 0
    for chunk in chunks:
        response = (post or requests.post)(
            f"https://discord.com/api/v10/channels/{target}/messages",
            headers={"Authorization": f"Bot {token}"},
            json={"content": chunk, "allowed_mentions": {"parse": []}},
            timeout=30,
        )
        if response.status_code not in (200, 201):
            if not sent and 400 <= response.status_code < 500:
                raise DeliveryRejected(f"Discord rejected HTTP {response.status_code}")
            raise RuntimeError(
                "Discord delivery uncertain or partially sent; review before retry"
            )
        sent += 1


class Outbox:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                key TEXT PRIMARY KEY, sink TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL, updated_at TEXT NOT NULL, error TEXT)""")
            db.execute(
                "CREATE TABLE IF NOT EXISTS claims (destination TEXT PRIMARY KEY, delivery_key TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS item_receipts (key TEXT PRIMARY KEY, sent_date TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS delivery_items (delivery_key TEXT NOT NULL, receipt_key TEXT NOT NULL, sent_date TEXT NOT NULL, PRIMARY KEY(delivery_key, receipt_key))"
            )

    @staticmethod
    def destination_key(route, target):
        normalized = email_addresses(target) if route["sink"] == "email" else target
        return fingerprint([route["sink"], normalized])

    @staticmethod
    def receipt_key(item, destination):
        return fingerprint(
            [destination, dedup_key(item), item["title"], item["summary"]]
        )

    def unseen_report(self, report, route, target):
        """Keep today's content, but suppress unchanged items sent on older days."""
        destination = self.destination_key(route, target)
        topics = []
        with sqlite3.connect(self.path) as db:
            for topic in report["topics"]:
                items = []
                for item in topic["items"]:
                    row = db.execute(
                        "SELECT sent_date FROM item_receipts WHERE key=?",
                        (self.receipt_key(item, destination),),
                    ).fetchone()
                    if not row or row[0] == report["date"]:
                        items.append(item)
                if items:
                    topics.append({**topic, "items": items})
        return {**report, "topics": topics}

    def deliver(
        self, report: dict, route: dict, target: str, transport, *, force=False
    ) -> str:
        # No plaintext destination, token or webhook is persisted in the ledger.
        normalized_target = (
            email_addresses(target) if route["sink"] == "email" else target
        )
        destination = self.destination_key(route, target)
        key = fingerprint(
            {
                "content": report_markdown(report),
                "sink": route["sink"],
                "target": normalized_target,
            }
        )
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status, attempts FROM deliveries WHERE key=?", (key,)
            ).fetchone()
            if row and row[0] == "success" and not force:
                return "skipped"
            if row and row[0] == "pending":
                # Force is deliberately insufficient: it could be a live worker.
                raise RuntimeError(
                    "delivery is pending/uncertain; inspect it and use delivery-reset before retry"
                )
            if db.execute(
                "SELECT delivery_key FROM claims WHERE destination=?", (destination,)
            ).fetchone():
                raise RuntimeError(
                    "this destination has a pending/uncertain delivery; inspect delivery-status before sending another digest"
                )
            db.execute(
                "INSERT OR REPLACE INTO deliveries VALUES (?,?,?,?,?,NULL)",
                (key, route["sink"], "pending", (row[1] if row else 0) + 1, now),
            )
            db.execute("INSERT INTO claims VALUES (?,?)", (destination, key))
            for topic in report["topics"]:
                for item in topic["items"]:
                    db.execute(
                        "INSERT OR REPLACE INTO delivery_items VALUES (?,?,?)",
                        (key, self.receipt_key(item, destination), report["date"]),
                    )
        status, error = "success", None
        try:
            transport()
        except (DeliveryRejected, ValueError) as exc:
            status, error = (
                "failed",
                sanitize_error(exc),
            )
        except Exception:
            status, error = (
                "pending",
                "Delivery result uncertain; inspect recipient before explicit reset",
            )
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE deliveries SET status=?, updated_at=?, error=? WHERE key=?",
                (status, datetime.now(timezone.utc).isoformat(), error, key),
            )
            if status != "pending":
                db.execute("DELETE FROM claims WHERE delivery_key=?", (key,))
            if status == "success":
                for topic in report["topics"]:
                    for item in topic["items"]:
                        db.execute(
                            "INSERT OR REPLACE INTO item_receipts VALUES (?,?)",
                            (self.receipt_key(item, destination), report["date"]),
                        )
        return status

    def status(self) -> list[dict]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM deliveries ORDER BY updated_at DESC"
                )
            ]

    def reset(self, key: str):
        # Explicit operator recovery only; never done by a scheduled run.
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("delivery key must be a SHA-256 digest")
        with sqlite3.connect(self.path) as db:
            result = db.execute(
                "UPDATE deliveries SET status='failed', error='Explicit operator reset' WHERE key=? AND status='pending'",
                (key,),
            )
            if result.rowcount != 1:
                raise ValueError("no pending delivery found for this key")
            db.execute("DELETE FROM claims WHERE delivery_key=?", (key,))

    def confirm(self, key: str):
        """Resolve an uncertain send as delivered after an operator checks it."""
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("delivery key must be a SHA-256 digest")
        with sqlite3.connect(self.path) as db:
            result = db.execute(
                "UPDATE deliveries SET status='success', error=NULL, updated_at=? WHERE key=? AND status='pending'",
                (datetime.now(timezone.utc).isoformat(), key),
            )
            if result.rowcount != 1:
                raise ValueError("no pending delivery found for this key")
            db.execute(
                "INSERT OR REPLACE INTO item_receipts SELECT receipt_key, sent_date FROM delivery_items WHERE delivery_key=?",
                (key,),
            )
            db.execute("DELETE FROM claims WHERE delivery_key=?", (key,))
