"""故障注入开关 —— apply / revert / status，全部可逆（备份 + 校验和）。

实现方式：补丁逻辑以 PHP 脚本 docker cp 进容器执行（容器是 PHP 环境），
find/replace 走 base64 传参，绕开 shell 转义地狱。

用法：
    python -m scripts.inject apply  DEF_L2_001
    python -m scripts.inject revert DEF_L2_001
    python -m scripts.inject status
状态写 report/patches_state.json（orchestrator 读它算 patches_hash，
保证金标准实验「同 patches_hash 才算同条件对照」）。
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

from core.config import settings

MANIFEST = Path(__file__).resolve().parent.parent / "data" / "golden" / "manifest.json"
STATE = settings.report_dir / "patches_state.json"
PHP_PATCH = Path(__file__).resolve().parent / "patch.php"

_PHP_SOURCE = '''<?php
// patch.php <file> <apply|revert> <find_b64> <replace_b64>
// apply: 首次备份 .orig，替换第一处 find -> replace
// revert: 用 .orig 还原
$file = $argv[1]; $mode = $argv[2];
$orig = $file . ".orig";
if ($mode === "revert") {
    if (!file_exists($orig)) { fwrite(STDERR, "no backup\\n"); exit(1); }
    copy($orig, $file);
    exit(0);
}
$find = base64_decode($argv[3]); $repl = base64_decode($argv[4]);
if (!file_exists($orig)) copy($file, $orig);
$s = file_get_contents($file);
$pos = strpos($s, $find);
if ($pos === false) { fwrite(STDERR, "find-not-found\\n"); exit(2); }
file_put_contents($file, substr_replace($s, $repl, $pos, strlen($find)));
echo "patched\\n";
'''


def _load_manifest() -> dict:
    return json.loads(MANIFEST.read_text("utf-8"))


def _defect(defect_id: str) -> dict:
    for d in _load_manifest()["defects"]:
        if d["id"] == defect_id:
            return d
    raise KeyError(f"金标准里没有 {defect_id}")


def _active() -> list[str]:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text("utf-8")).get("active", [])
        except json.JSONDecodeError:
            return []
    return []


def _set_active(active: list[str]) -> None:
    STATE.write_text(json.dumps({"active": active}, ensure_ascii=False), "utf-8")


def _docker(*args: str, check: bool = True) -> str:
    r = subprocess.run(["docker", "exec", "zentao", *args],
                       capture_output=True, text=True, timeout=60)
    if check and r.returncode != 0:
        raise RuntimeError(f"docker exec 失败: {r.stderr[:300]}")
    return r.stdout


def _ensure_patch_script() -> None:
    PHP_PATCH.write_text(_PHP_SOURCE, encoding="utf-8")
    subprocess.run(["docker", "cp", str(PHP_PATCH), "zentao:/tmp/patch.php"],
                   capture_output=True, text=True, timeout=30, check=True)


def apply(defect_id: str) -> None:
    d = _defect(defect_id)
    if d["kind"] != "source_patch":
        print(f"{defect_id}: {d['kind']} 类不适用 apply（由 golden_run 驱动）")
        return
    if defect_id in _active():
        print(f"{defect_id}: 已是生效状态")
        return
    _ensure_patch_script()
    fb64 = base64.b64encode(d["find"].encode()).decode()
    rb64 = base64.b64encode(d["replace"].encode()).decode()
    out = _docker("php", "/tmp/patch.php", d["file"], "apply", fb64, rb64)
    active = _active() + [defect_id]
    _set_active(active)
    print(f"{defect_id}: 已注入 → {out.strip()}（备份 {d['file']}.orig）")


def revert(defect_id: str) -> None:
    d = _defect(defect_id)
    if d["kind"] != "source_patch":
        print(f"{defect_id}: {d['kind']} 类不适用 revert")
        return
    _ensure_patch_script()
    _docker("php", "/tmp/patch.php", d["file"], "revert", "", "")
    _set_active([x for x in _active() if x != defect_id])
    print(f"{defect_id}: 已还原")


def revert_all() -> None:
    for defect_id in list(_active()):
        revert(defect_id)


def status() -> None:
    print(f"生效补丁: {_active() or '无（clean）'}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "apply":
        apply(sys.argv[2])
    elif cmd == "revert":
        revert(sys.argv[2])
    elif cmd == "revert_all":
        revert_all()
    else:
        status()
