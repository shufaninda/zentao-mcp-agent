"""finalize —— 三道幂等防线走完，建 Bug（或按开关只记录）+ 写 RunReport。

流程（顺序锁死 triage → dedup → finalize 之后的最后一环）：
  对每个 action=new 的指纹：
    ① claim_for_creation（write-ahead 抢占，防崩溃重复）
    ② zentao_search_bugs（禅道侧查重，第三道防线）
    ③ auto_create_bug=true → create_bug → mark_created
       auto_create_bug=false → mark_recorded（只记录不建单）
  所有终止路径（含全部通过、无 bug）都写 RunReport。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core import ledger, trace
from core.config import settings
from tools import zentao


def finalize(run_id: str, bugs_to_create: list[dict],
             summary: dict, bugs_linked: list[dict] | None = None) -> dict:
    """bugs_to_create: dedup 产出的 [{'action':'new', 'fp','row','triage'}...]"""
    created, recorded, errors = [], [], []
    linked = list(bugs_linked or [])

    for item in bugs_to_create:
        fp, row = item["fp"], item["row"]
        tri = item["triage"]
        if item["action"] != "new":
            linked.append({"fp": fp, "bug_id": row.get("bug_id"), "state": row["state"]})
            continue

        attempt_id = uuid.uuid4().hex[:8]
        if not ledger.claim_for_creation(fp, attempt_id):
            current = ledger.get_fingerprint(fp)
            linked.append({"fp": fp, "bug_id": current.get("bug_id"),
                           "state": current["state"], "note": "已被其他尝试认领"})
            continue

        # 第三道防线：禅道侧按签名关键词查重
        try:
            hits = zentao.zentao_search_bugs(row["signature"][:40])
        except zentao.ZentaoError as e:
            ledger.mark_failed(fp, f"search_bugs 失败: {e.message}")
            errors.append({"fp": fp, "stage": "search", "error": e.to_dict()})
            continue

        if hits:
            bug_id = hits[0]["id"]
            ledger.mark_created(fp, bug_id)
            linked.append({"fp": fp, "bug_id": bug_id, "note": "禅道已有同题 Bug，关联"})
            continue

        if not settings.auto_create_bug:
            ledger.mark_recorded(fp, "AUTO_CREATE_BUG=false，只记录不建单")
            recorded.append({"fp": fp, "signature": row["signature"]})
            continue

        try:
            res = zentao.zentao_create_bug(
                product_id=_product(), module_id=_module(row["module"]),
                title=_title(tri, row), severity=tri.severity,
                pri=_pri(row["module"]), steps=row["signature"],
            )
            ledger.mark_created(fp, res["bug_id"])
            created.append({"fp": fp, "bug_id": res["bug_id"], "url": res["url"]})
        except zentao.ZentaoError as e:
            ledger.mark_failed(fp, e.to_dict()["message"])
            errors.append({"fp": fp, "stage": "create", "error": e.to_dict()})

    report = {
        "run_id": run_id,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **summary,
        "triage": summary.get("triage", {}),
        "bugs_created": created,
        "bugs_linked": linked,
        "bugs_recorded_only": recorded,
        "errors": errors,
        "feature_flags": {"auto_create_bug": settings.auto_create_bug},
    }
    report_path = settings.report_dir / f"report-{run_id}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    ledger.finish_run(run_id, summary.get("total", 0), summary.get("passed", 0),
                      summary.get("failed", 0), str(report_path))
    return report


def _product() -> int:
    import yaml
    m = yaml.safe_load((settings.report_dir.parent / "mapping.yaml").read_text("utf-8"))
    return int(m["product"]["default"])


def _module(module_key: str) -> int:
    import yaml
    m = yaml.safe_load((settings.report_dir.parent / "mapping.yaml").read_text("utf-8"))
    return int(m["modules"].get(module_key.lower(), {}).get("module_id", 0))


def _pri(module_key: str) -> int:
    return 1 if module_key.lower() == "auth" else 3


def _title(tri, row: dict) -> str:
    """Bug 标题公式：【模块】+ 条件 + 现象。签名里已含条件与现象。"""
    module = row["module"]
    return f"【{module}】{row['signature'][:80]}"
