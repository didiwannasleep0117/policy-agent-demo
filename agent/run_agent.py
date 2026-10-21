"""最小 agent：Ollama（工具呼叫）＋ MCP 伺服器。用來驗收，也可以直接當 CLI 問答。

用法：
  uv run python agent/run_agent.py "婚假怎麼請？"
  uv run python agent/run_agent.py --eval            # 跑 eval/questions.yaml
環境變數：MCP_URL（預設 http://127.0.0.1:8000/mcp）、OLLAMA_URL、CHAT_MODEL（預設 qwen3:8b）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parents[1]
MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:8000/mcp")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.getenv("CHAT_MODEL", "qwen3:8b")
SYSTEM = (ROOT / "agent" / "system_prompt.md").read_text(encoding="utf-8")
MAX_STEPS = 6
THINK = os.getenv("THINK", "0") == "1"  # qwen3 思考模式：較準但較慢
CALC_CHECK = os.getenv("CALC_CHECK", "1") == "1"  # 答案有算式卻沒用計算機 → 退回驗算
# 「數字 運算子 數字」，例如 40,000 × 1.2、200元 × 3
_ARITH = re.compile(r"\d[\d,.]*\s*(?:元|小時|個月|倍|日)?\s*[×xX*÷/+]\s*\(?\s*\d")


async def ask(session: ClientSession, question: str, verbose: bool = True, history: list | None = None) -> dict:
    """history：先前對話（[{role, content}, ...]），用來測追問。"""
    tools = (await session.list_tools()).tools
    ollama_tools = [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.inputSchema}}
        for t in tools
    ]
    msgs = [{"role": "system", "content": SYSTEM}, *(history or []), {"role": "user", "content": question}]
    calls: list[tuple[str, dict]] = []
    outputs: list[str] = []
    nudged = False
    calc_nudged = False
    async with httpx.AsyncClient(timeout=300) as http:
        for _ in range(MAX_STEPS + 2):
            r = await http.post(f"{OLLAMA_URL}/api/chat", json={
                "model": MODEL, "messages": msgs, "tools": ollama_tools, "stream": False,
                "think": THINK, "options": {"temperature": 0},
            })
            r.raise_for_status()
            msg = r.json()["message"]
            msgs.append(msg)
            if not msg.get("tool_calls"):
                ans = msg.get("content", "").strip()
                # Harness 檢查 1：沒查任何文件就下結論（又不是拒答）→ 退回去要求先查一次
                if not calls and not nudged and "只能回答" not in ans:
                    nudged = True
                    msgs.append({"role": "user", "content": "（系統檢查）你還沒有查詢任何文件。請先用 search_related_policies 查詢後再回答。"})
                    continue
                # Harness 檢查 2：答案裡有心算的算式，卻沒用計算機 → 要求用 calculate 驗算
                used_calc = any(n.endswith("calculate") for n, _ in calls)
                if CALC_CHECK and not used_calc and not calc_nudged and _ARITH.search(ans):
                    calc_nudged = True
                    msgs.append({"role": "user", "content": "（系統檢查）你的回答裡有計算，但沒有使用 calculate 工具。請先重新確認條文裡的公式（基準、倍數、分段），再用 calculate 算出每一個數字，最後重寫答案。"})
                    continue
                return {"answer": ans, "calls": calls, "outputs": outputs, "nudged": nudged, "calc_nudged": calc_nudged}
            for tc in msg["tool_calls"]:
                name, args = tc["function"]["name"], tc["function"].get("arguments") or {}
                calls.append((name, args))
                if verbose:
                    print(f"  🔧 {name}({json.dumps(args, ensure_ascii=False)})", file=sys.stderr)
                res = await session.call_tool(name, args)
                text = "\n".join(c.text for c in res.content if getattr(c, "text", None))
                outputs.append(text)
                msgs.append({"role": "tool", "content": text, "tool_name": name})
    return {"answer": "（超過工具呼叫上限）", "calls": calls, "outputs": outputs, "nudged": nudged}


def grade(item: dict, out: dict) -> tuple[bool, str]:
    ans, calls = out["answer"], out["calls"]
    if item.get("refuse"):
        ok = not calls and "只能回答" in ans
        return ok, "拒答且未查工具" if ok else "應拒答"
    if item.get("not_found"):
        ok = bool(calls) and ("人資" in ans) and any(k in ans for k in ("找不到", "沒有提到", "未提及", "沒有相關"))
        return ok, "查過且回答找不到" if ok else "應查詢後回答找不到"
    # 「看過全文」：搜尋附了該文件全文，或有呼叫 read_specific_document 讀它
    read = any(f"[[{item['expect_file']}]]\n" in o and ("《全文》" in o or o.startswith("[[")) for o in out.get("outputs", []))
    cited = f"[[{item['expect_file']}]]" in ans
    norm = re.sub(r"\s", "", ans)
    content = any(all(k.replace(" ", "") in norm for k in group) for group in item["must_contain"])
    ok = read and cited and content
    return ok, f"看過全文={read} 引用正確={cited} 內容正確={content}" + ("（被提醒先查）" if out.get("nudged") else "") + ("（被要求驗算）" if out.get("calc_nudged") else "")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?")
    ap.add_argument("--eval", action="store_true")
    a = ap.parse_args()
    async with streamablehttp_client(MCP_URL) as (r, w, _), ClientSession(r, w) as session:
        await session.initialize()
        if not a.eval:
            out = await ask(session, a.question or "婚假怎麼請？")
            print(out["answer"])
            return
        items = yaml.safe_load((ROOT / "eval" / "questions.yaml").read_text(encoding="utf-8"))
        results, passed = [], 0
        for it in items:
            t0 = time.time()
            history = []
            for prev in it.get("before", []):  # 追問題：先問前一題，把問答放進歷史
                p = await ask(session, prev, verbose=False)
                history += [{"role": "user", "content": prev}, {"role": "assistant", "content": p["answer"]}]
            out = await ask(session, it["q"], verbose=False, history=history)
            ok, why = grade(it, out)
            passed += ok
            dt = time.time() - t0
            print(f"{'✅' if ok else '❌'} {it['q']}  ({dt:.1f}s, {len(out['calls'])} 次工具)  {why}")
            results.append({"q": it["q"], "ok": ok, "why": why, "seconds": round(dt, 1),
                            "calls": out["calls"], "answer": out["answer"]})
        print(f"\n通過 {passed}/{len(items)}")
        (ROOT / "eval" / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
