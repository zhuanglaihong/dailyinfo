"""Create a local preview with explicitly synthetic data; never call AI or send.

Run: uv run python examples/preview_digest.py
"""

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from notifications import report_html
from recommendations import report_markdown


def main():
    report = {
        "version": 1,
        "date": "2026-10-07",
        "mode": "rules",
        "topics": [
            {
                "id": "ai_for_science",
                "name": "AI for Science · 演示数据",
                "items": [
                    {
                        "id": "demo-paper",
                        "category": "papers",
                        "title": "示例：物理约束的水文预测（非真实论文）",
                        "summary": "这里展示来源摘要的位置：研究问题、核心方法和主要发现。实际内容由采集与摘要流程提供。",
                        "reason": "这里展示推荐理由的位置：与关注的水文预测、物理约束机器学习主题相关。实际理由由 AI 根据已有内容生成。",
                        "relevance": 80,
                        "source": {
                            "name": "示例来源",
                            "url": "https://example.com/demo-paper",
                        },
                        "source_published_at": None,
                        "retrieved_at": "2026-10-07T00:00:00Z",
                    }
                ],
            }
        ],
    }
    output = Path(__file__).parent
    (output / "daily_digest.preview.md").write_text(
        "<!-- 演示数据，非真实论文；未调用 AI，未发送消息。 -->\n"
        + report_markdown(report),
        encoding="utf-8",
    )
    (output / "daily_digest.preview.html").write_text(
        report_html(report), encoding="utf-8"
    )
    print(output / "daily_digest.preview.md")


if __name__ == "__main__":
    main()
