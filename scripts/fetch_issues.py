"""禅道官方仓库历史 Bug 采集 —— 匿名 GitHub API，零凭据。

语料定位（冻结）：closed 且标题含 bug 特征的 issue = 已修复的历史缺陷。
质量分级：title 命中强特征 → quality=high（金标准）；其余 closed → quality=low。
用法：python -m scripts.fetch_issues
产物：data/knowledge/issues_raw.json（原始）→ 清洗后由 retrieve 切块。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from core.config import settings

REPO = "easysoft/zentaopms"
OUT = Path(settings.data_dir) / "knowledge" / "issues_raw.json"
API = "https://api.github.com/search/issues"

BUG_PAT = re.compile(r"bug|缺陷|错误|修复|fix|无法|失败|异常", re.I)
STRONG_PAT = re.compile(r"^\[?bug|修复|fix[:：]|缺陷", re.I)


def _get(url: str, params: dict) -> dict:
    r = requests_get(url, params)
    if r.status_code == 403:
        rate_left = r.headers.get("X-RateLimit-Remaining")
        raise RuntimeError(f"GitHub 限流（剩余 {rate_left}），稍后再跑")
    r.raise_for_status()
    return r.json()


def requests_get(url, params):
    import requests
    return requests.get(url, params=params,
                        headers={"Accept": "application/vnd.github+json"}, timeout=20)


QUERIES = ["bug in:title", "缺陷", "无法", "修复", "异常", "错误"]

def fetch(pages_per_q: int = 2, per_page: int = 50) -> list[dict]:
    """多查询词分批搜，按 issue_id 去重。"""
    out, seen = [], set()
    for q in QUERIES:
        for page in range(1, pages_per_q + 1):
            try:
                data = _get(API, {"q": f"repo:{REPO} is:issue is:closed {q}",
                                  "per_page": per_page, "page": page})
            except RuntimeError as e:
                print(f"[{q}] p{page} 跳过: {e}")
                break
            items = data.get("items", [])
            if not items:
                break
            fresh = 0
            for it in items:
                if it["number"] in seen:
                    continue
                seen.add(it["number"])
                title = it.get("title", "")
                body = (it.get("body") or "")[:1500]
                out.append({
                    "issue_id": it["number"], "title": title,
                    "body": re.sub(r"!\[[^\]]*\]\([^)]*\)|<[^>]+>", "", body),
                    "url": it["html_url"],
                    "labels": [l["name"] for l in it.get("labels", [])],
                    "closed_at": it.get("closed_at"),
                    "quality": "high" if STRONG_PAT.search(title) else "low",
                })
                fresh += 1
            print(f"[{q}] p{page}: +{fresh} 累计 {len(out)}")
            time.sleep(7)
    return out


def main() -> None:
    issues = fetch()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(issues, ensure_ascii=False, indent=1), "utf-8")
    hi = sum(1 for i in issues if i["quality"] == "high")
    print(f"完成: {len(issues)} 条 → {OUT}（high {hi} / low {len(issues)-hi}）")


if __name__ == "__main__":
    main()
