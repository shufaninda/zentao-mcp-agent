"""输出可自证数字汇总到 report/eval-metrics.json。"""
import json
from core.config import settings
from eval.metrics import summarize

def main():
    r = summarize()
    out = settings.report_dir / "eval-metrics.json"
    out.write_text(json.dumps(r, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("已写", out)

if __name__ == "__main__":
    main()
