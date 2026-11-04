"""RAG 检索 v2 —— 三语料三切法 + 锚点加权 + BM25/向量混合（RRF 融合）。

切分单元由检索目的决定（冻结）：
  ① 接口文档 data/knowledge/*.md   → 一个接口一块（## 标题切），锚点=method+path
  ② 历史 Bug issues_raw.json       → 一条 issue 一块，quality 分层（high 排前）
  ③ 账本回灌 fingerprints          → 一条指纹一块（signature+note+module）

token 化：ASCII 词/路径整体成 token（/api.php/v1 不拆），中文 jieba 分词。
混合：BM25 + chromadb 向量（未装则纯 BM25 降级），RRF 融合（k=60）。
锚点加权：命中 scope 锚点词的块 ×(1+2×命中数)（软加权，不硬过滤防结果为空）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from core import ledger
from core.config import settings

_KB_DIR = Path(settings.data_dir) / "knowledge"
_ISSUES = _KB_DIR / "issues_raw.json"

_ANCHOR_ALIAS = {
    "task": ["task", "任务", "execution"],
    "bug": ["bug", "缺陷"],
    "user": ["user", "用户", "权限", "token"],
}

# ---------------------------------------------------------------- 语料
def _interface_chunks() -> list[dict]:
    chunks = []
    for md in sorted(_KB_DIR.glob("*.md")):
        for i, part in enumerate(md.read_text(encoding="utf-8").split("\n## ")):
            body = part.strip()
            if len(body) < 20:
                continue
            anchors = re.findall(r"((?:GET|POST|PUT|DELETE))\s+(/api\.php[^\s（(]*)", body)
            chunks.append({"chunk_id": f"doc:{md.stem}#{i}", "source": f"接口文档/{md.stem}",
                           "text": body, "quality": "high",
                           "anchors": [f"{m} {p}".lower() for m, p in anchors]})
    return chunks


def _issue_chunks() -> list[dict]:
    if not _ISSUES.exists():
        return []
    issues = json.loads(_ISSUES.read_text("utf-8"))
    out = []
    for it in issues:
        body = (it.get("body") or "").strip()[:600]
        if len(body) < 30:
            continue
        text = f"# {it['title']}\n{body}\n(来源: {it['url']})"
        out.append({"chunk_id": f"issue:{it['issue_id']}", "source": "禅道历史Issue",
                    "text": text, "quality": it.get("quality", "low"), "anchors": []})
    return out


def _ledger_chunks() -> list[dict]:
    out = []
    try:
        rows = ledger.db().execute(
            "SELECT fp, module, signature, note FROM fingerprints WHERE state='created'"
        ).fetchall()
    except Exception:  # noqa: BLE001 —— 账本未初始化时检索仍可用
        return out
    for r in rows:
        text = (f"# 已确认缺陷 {r['fp']}\n模块 {r['module']}: {r['signature']}\n"
                f"备注: {r['note'] or ''}")
        out.append({"chunk_id": f"fp:{r['fp']}", "source": "账本回灌",
                    "text": text, "quality": "high",
                    "anchors": [r["module"].lower()]})
    return out


def _all_chunks() -> list[dict]:
    return _interface_chunks() + _issue_chunks() + _ledger_chunks()


# ---------------------------------------------------------------- token 化
_ascii_re = re.compile(r"[a-zA-Z_][\w./:-]*")
_cjk_re = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    """ASCII 词/路径整体成 token（/api.php/v1 不拆）；中文 jieba 分词。"""
    import jieba
    ascii_tokens = [t.lower() for t in _ascii_re.findall(text)]
    zh = []
    for run in _cjk_re.findall(text):
        zh += [w.strip() for w in jieba.lcut(run) if w.strip()]
    return ascii_tokens + zh


# ---------------------------------------------------------------- 检索
def search(scope: list[str], top_k: int = 6) -> list[dict]:
    """输出契约：[{chunk_id, source, text, score, quality}]。BM25+向量 RRF。"""
    chunks = _all_chunks()
    if not chunks:
        return []

    anchors: list[str] = []
    for s in scope:
        anchors += _ANCHOR_ALIAS.get(s.lower(), [s.lower()])

    # BM25 主检索
    from rank_bm25 import BM25Okapi
    corpus = [c["text"] for c in chunks]
    bm25 = BM25Okapi([tokenize(t) for t in corpus])
    q_tokens = tokenize(" ".join(scope) + " " + " ".join(anchors))
    bm_scores = bm25.get_scores(q_tokens)
    order = sorted(range(len(chunks)), key=lambda i: -bm_scores[i])
    bm_ranks = {chunks[i]["chunk_id"]: rk for rk, i in enumerate(order)}

    # 向量（可选）：chromadb 未装 → 降级纯 BM25
    vec_ranks: dict[str, int] = {}
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(settings.report_dir / "chroma"))
        col = client.get_or_create_collection("kb")
        if col.count() != len(chunks):
            col.upsert(ids=[c["chunk_id"] for c in chunks],
                       documents=[c["text"] for c in chunks])
        res = col.query(query_documents=[" ".join(scope) + " " + " ".join(anchors)],
                        n_results=min(top_k, len(chunks)))
        vec_ranks = {cid: i for i, cid in enumerate(res["ids"][0])}
    except Exception:  # noqa: BLE001 —— 向量层可选，失败降级
        vec_ranks = {}

    # RRF 融合 + 锚点软加权 + 质量分层
    def rrf(cid: str) -> float:
        s = 0.0
        if cid in bm_ranks:
            s += 1.0 / (60 + bm_ranks[cid])
        if cid in vec_ranks:
            s += 1.0 / (60 + vec_ranks[cid])
        return s

    scored = []
    for c in chunks:
        s = rrf(c["chunk_id"])
        text_lower = c["text"].lower()
        anchor_hits = sum(1 for a in anchors if a in text_lower)
        s *= (1 + 2 * anchor_hits)
        if c.get("quality") == "low":
            s *= 0.5
        scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    return [{**c, "score": round(s, 4)} for s, c in scored[:top_k] if s > 0]
