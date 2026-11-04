"""金标准混淆矩阵 v2 —— 按补丁组（patch group）组织。

教训：禅道校验是双层的（API form 层 + model 层），单补丁注入无效——
"注入无效"≠"漏报"，必须一组补丁同时打穿才构成一次有效的缺陷注入。

对每个组：
  ① 全局 clean run → 非原生缺陷的 catching_case 必须通过（否则用例基线错）
  ② apply 组内全部补丁 → run → catching_case 失败且归因 product_bug → 检出
  ③ revert 全部
误报口径：clean run 中被判 product_bug 的失败 − 原生缺陷数（DEF_PRE）。
产物：report/golden-matrix.json

用法：python -m scripts.golden_run
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.config import settings

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_CASES = str(ROOT / "data" / "golden" / "golden_cases.json")

GROUPS = [
    {"id": "G_PRE_LEN", "level": "L2", "patches": [],
     "catching_case": "BUG_LEN_001", "preexisting": True,
     "name": "任务名称无长度上限（禅道原生）"},
    {"id": "G_TASK_EMPTY", "level": "L2",
     "patches": ["DEF_L2_001", "DEF_L2_002"],
     "catching_case": "BUG_EMPTY_001", "preexisting": False,
     "name": "任务名称非空校验失效（双层补丁）"},
    {"id": "G_BUG_EMPTY", "level": "L2",
     "patches": ["DEF_L2_003", "DEF_L2_004"],
     "catching_case": "BUG_TITLE_001", "preexisting": False,
     "name": "Bug标题非空校验失效（双层补丁）"},
    {"id": "G_L3_PRIV", "level": "L3",
     "patches": ["DEF_L3_001", "DEF_L3_002"],
     "catching_case": "BUG_PRIV_001", "preexisting": False,
     "name": "框架层越权：普通用户可创建用户"},
]


def _run_orchestrator(task: str) -> dict:
    r = subprocess.run([sys.executable, "-m", "agent.orchestrator",
                        "--task", task, "--cases", GOLDEN_CASES],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"orchestrator 失败: {r.stdout[-200:]} {r.stderr[-200:]}")
    reports = sorted((settings.report_dir).glob(f"report-{task}-*.json"))
    return json.loads(reports[-1].read_text(encoding="utf-8"))


def _inject(cmd: str, defect_id: str) -> None:
    subprocess.run([sys.executable, "-m", "scripts.inject", cmd, defect_id],
                   cwd=ROOT, check=True, capture_output=True, text=True)


def main() -> int:
    matrix, false_positives = [], 0

    # ① 全局 clean run：所有 catching_case 必须通过（原生缺陷除外）
    clean = _run_orchestrator("golden-clean")
    clean_failed = {f.get("case_id") for f in clean.get("failures", [])}
    for g in GROUPS:
        if not g["preexisting"] and g["catching_case"] in clean_failed:
            raise RuntimeError(f"{g['id']}: clean 版 {g['catching_case']} 竟然失败，用例基线错误")
    pre_fails = sum(1 for g in GROUPS if g["preexisting"]
                    and g["catching_case"] in clean_failed)
    false_positives = len([c for c in clean_failed
                           if c not in {g["catching_case"] for g in GROUPS if g["preexisting"]}])
    print(f"[clean] 失败={sorted(clean_failed)} 原生缺陷检出={pre_fails} 误报={false_positives}")

    # ② 逐组注入 → run → 判定
    for g in GROUPS:
        if not g["patches"]:
            # 原生缺陷：clean run 的结果就是它的检测结果
            detected = g["catching_case"] in clean_failed
            tri_file = settings.report_dir / f"triaged-{clean['run_id']}.json"
            cat = next((t["category"] for t in json.loads(tri_file.read_text("utf-8"))
                        if t["case_id"] == g["catching_case"]), "not_failed")
            matrix.append({"group": g["id"], "level": g["level"], "name": g["name"],
                           "detected": detected, "triage_category": cat,
                           "hit": detected and cat == "product_bug"})
            print(f"[{g['id']}] 原生: detected={detected} triage={cat}")
            continue

        for pid in g["patches"]:
            _inject("apply", pid)
        try:
            run = _run_orchestrator(f"golden-{g['id']}")
            failed_ids = {f.get("case_id") for f in run.get("failures", [])}
            detected = g["catching_case"] in failed_ids
            tri_file = settings.report_dir / f"triaged-{run['run_id']}.json"
            cat = next((t["category"] for t in json.loads(tri_file.read_text("utf-8"))
                        if t["case_id"] == g["catching_case"]), "not_failed")
            matrix.append({"group": g["id"], "level": g["level"], "name": g["name"],
                           "patches": g["patches"], "detected": detected,
                           "triage_category": cat,
                           "hit": detected and cat == "product_bug"})
            print(f"[{g['id']}] detected={detected} triage={cat}")
        finally:
            for pid in g["patches"]:
                _inject("revert", pid)

    by_level = {}
    for m in matrix:
        by_level.setdefault(m["level"], {"total": 0, "hit": 0})
        by_level[m["level"]]["total"] += 1
        by_level[m["level"]]["hit"] += 1 if m["hit"] else 0

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "matrix": matrix,
        "detection_by_level": {k: f"{v['hit']}/{v['total']}" for k, v in by_level.items()},
        "false_positives_clean_run": false_positives,
        "note": "分类不是 product_bug 但 detected=True 的 = 归因类别偏差（另计）",
    }
    out = settings.report_dir / "golden-matrix.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    print("检出率:", result["detection_by_level"], f"误报={false_positives}")
    print(f"矩阵已写 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
