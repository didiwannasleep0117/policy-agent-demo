"""分流工作流評測：分流準確率（主要）＋ 各分支有沒有正常回答（次要）。

用法：python eval/run_router_eval.py [--questions eval/router.yaml]
需要：n8n、policy MCP（8000）、text2sql-demo 的 text2sql-mcp（8002）都在跑。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")
IDS = json.loads((ROOT / "n8n" / "router_ids.json").read_text(encoding="utf-8"))
URL = f"http://localhost:5678/webhook/{IDS['webhook']}/chat"


def chat(q: str) -> dict:
    r = urllib.request.Request(URL, method="POST", headers={"Content-Type": "application/json"},
                               data=json.dumps({"action": "sendMessage", "sessionId": f"eval-{uuid.uuid4().hex[:8]}",
                                                "chatInput": q}).encode())
    with urllib.request.urlopen(r, timeout=600) as f:
        return json.loads(f.read())


def answered(route: str, out: dict) -> bool:
    txt = out.get("output", "")
    if route == "規章":
        return "/docs/" in txt or "人資" in txt  # 有引用卡片，或照規定說找不到
    if route == "數據":
        return out.get("action") in ("sql", "empty", "clarify") and bool(txt)
    return "只能回答" in txt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(ROOT / "eval" / "router.yaml"))
    ap.add_argument("--out", default=str(ROOT / "eval" / "results_router.json"))
    a = ap.parse_args()
    items = yaml.safe_load(Path(a.questions).read_text(encoding="utf-8"))
    res, confusion = [], Counter()
    for it in items:
        t0 = time.time()
        try:
            out = chat(it["q"])
        except Exception as e:  # noqa: BLE001
            out = {"output": f"錯誤 {e!r}", "route": "（失敗）"}
        dt = time.time() - t0
        got = out.get("route", "（無）")
        ok_route = got == it["route"]
        ok_ans = ok_route and answered(got, out)
        confusion[(it["route"], got)] += 1
        print(f"{'✅' if ok_route else '❌'} 應走 {it['route']}，實際 {got}  答覆{'正常' if ok_ans else '異常'}  ({dt:.1f}s)  {it['q']}",
              flush=True)
        res.append({"q": it["q"], "want": it["route"], "got": got, "route_ok": ok_route, "answer_ok": ok_ans,
                    "seconds": round(dt, 1), "output": out.get("output", "")[:600]})
    n = len(res)
    print(f"\n分流正確 {sum(r['route_ok'] for r in res)}/{n}；分流正確且有正常回答 {sum(r['answer_ok'] for r in res)}/{n}；"
          f"平均 {sum(r['seconds'] for r in res) / n:.1f} 秒")
    print("混淆（應走 → 實際）：", {f"{k[0]}→{k[1]}": v for k, v in confusion.items() if k[0] != k[1]} or "無")
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
