"""用同一份 eval/questions.yaml 驗收 n8n 版（走聊天 webhook）。

n8n 版看不到工具呼叫細節，所以只檢查：引用的檔名、答案關鍵字、拒答、找不到。
用法：python eval/run_n8n_eval.py
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from n8n_admin import chat  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


def grade(item: dict, ans: str) -> tuple[bool, str]:
    if item.get("refuse"):
        ok = "只能回答" in ans
        return ok, "拒答" if ok else "應拒答"
    if item.get("not_found"):
        ok = "人資" in ans and bool(re.search(r"找不到|沒有.{0,4}提到|未提及|沒有相關|沒有.{0,4}規定", ans))
        return ok, "回答找不到" if ok else "應回答找不到"
    cited = f"[[{item['expect_file']}]]" in ans
    norm = re.sub(r"\s", "", ans)
    content = any(all(k.replace(" ", "") in norm for k in g) for g in item["must_contain"])
    return cited and content, f"引用正確={cited} 內容正確={content}"


def main() -> None:
    items = yaml.safe_load((ROOT / "eval" / "questions.yaml").read_text(encoding="utf-8"))
    passed, results = 0, []
    for it in items:
        sid = f"eval-{uuid.uuid4().hex[:8]}"
        for prev in it.get("before", []):
            chat(prev, sid)
        t0 = time.time()
        ans = chat(it["q"], sid)
        dt = time.time() - t0
        ok, why = grade(it, ans)
        passed += ok
        print(f"{'✅' if ok else '❌'} {it['q']}  ({dt:.1f}s)  {why}", flush=True)
        results.append({"q": it["q"], "ok": ok, "why": why, "seconds": round(dt, 1), "answer": ans})
    print(f"\n通過 {passed}/{len(items)}")
    (ROOT / "eval" / "results_n8n.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
