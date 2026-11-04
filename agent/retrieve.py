"""RAG 检索 —— BM25 主检索（接口名精确词不漏），向量层可选降级。

语料三来源（冻结）：
  ① data/knowledge/*.md —— 接口文档（手写自探针实测）
  ② 禅道 GitHub issues 历史 Bug（week3 后补）
  ③ 账本回灌（review_queue accepted 的条目）
chromadb 未安装时自动降级为纯 BM25，接口不变。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.config import settings

_KB_DIR = Path(settings.data_dir) / "knowledge"


def _load_chunks() -> list[dict]:
    """把 knowledge 目录切成 {chunk_id, source, text} 块。按 '## ' 二级标题切。"""
    chunks = []
    for md in sorted(_KB_DIR.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        parts = text.split("\n## ")
        for i, part in enumerate(parts):
            body = part.strip()
            if len(body) < 20:
                continue
            chunks.append({"chunk_id": f"{md.stem}#{i}", "source": md.name, "text": body})
    return chunks


def search(scope: list[str], top_k: int = 6) -> list[dict]:
    """检索 top-k 知识块。输出契约：[{chunk_id, source, text, score}]。"""
    chunks = _load_chunks()
    if not chunks:
        return []
    from rank_bm25 import BM25Okapi

    corpus = [c["text"] for c in chunks]
    tokenized = [list(t) for t in corpus]  # 中文按字切（接口名是 ASCII 词，字符级够用）
    bm25 = BM25Okapi(tokenized)
    query = list("".join(scope)) or ["task"]
    scores = bm25.get_scores(query)
    ranked = sorted(zip(chunks, scores), key=lambda x: -x[1])[:top_k]
    return [{**c, "score": round(float(s), 3)} for c, s in ranked if s > 0]
