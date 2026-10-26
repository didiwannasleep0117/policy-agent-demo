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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import checks  # noqa: E402
MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:8000/mcp")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.getenv("CHAT_MODEL", "qwen3:8b")
SYSTEM = Path(os.getenv("PROMPT_FILE", ROOT / "agent" / "system_prompt.md")).read_text(encoding="utf-8")
MAX_STEPS = 6
THINK = os.getenv("THINK", "0") == "1"  # qwen3 思考模式：較準但較慢
CALC_CHECK = os.getenv("CALC_CHECK", "1") == "1"  # 答案有算式卻沒用計算機 → 退回驗算
# 「數字 運算子 數字」，例如 40,000 × 1.2、200元 × 3
_ARITH = re.compile(r"\d[\d,.]*\s*(?:萬|千)?\s*(?:元|小時|個月|倍|日)?\s*[×xX*÷/+]\s*\(?\s*\d")


# ---- 驗算（recheck）的開關：由改進循環的候選 env.yaml 控制 ----
# RECHECK_MODE：inline＝在同一段對話裡退回重算（舊做法）；fresh＝開新對話，只給「問題＋查到的文件」，不給草稿
RECHECK_MODE = os.getenv("RECHECK_MODE", "inline")
RECHECK_THINK = os.getenv("RECHECK_THINK", "0") == "1"   # 驗算時才開思考模式
NUMERIC_TRIGGER = os.getenv("NUMERIC_TRIGGER", "0") == "1"  # 問題有數字或問「幾／多少」也觸發驗算
# DOC_CHECK：any＝有呼叫任何工具就算查過（舊）；strict＝一定要呼叫過查文件的工具（只用計算機不算）
DOC_CHECK = os.getenv("DOC_CHECK", "any")
_DOC_TOOLS = ("search_related_policies", "read_specific_document", "list_all_filenames")
# REFUSE_CHECK：模型沒查文件就拒答時，harness 自己用問題搜一次；最相關文件的相關度 ≥ 門檻就退回去要它回答
REFUSE_CHECK = os.getenv("REFUSE_CHECK", "0") == "1"
REFUSE_THRESHOLD = float(os.getenv("REFUSE_THRESHOLD", "0.5"))
_SIM = re.compile(r"相關度 ([0-9.]+)")
# 問題提到這些字，就算相關度不到門檻也不准不查就拒答（例：「公司今年有尾牙嗎？」相關度只有 0.449）
_COMPANY_WORDS = re.compile(os.getenv("REFUSE_KEYWORDS", r"$^"))
# KIN_CHECK：親屬稱謂由程式對照（像 Text-to-SQL 的代碼對照）。問題提到的親屬不在任何條文用語裡（舅舅、姑姑…），
# 回答又不是找不到 → 改成找不到並說明條文列了哪些親屬。動機：v6 prompt 規則、v7 模型自我核對都擋不住「套用最接近的一列」
KIN_CHECK = os.getenv("KIN_CHECK", "0") == "1"
KIN_MAP = {  # 口語 → 條文用語
    "太太": "配偶", "老婆": "配偶", "妻子": "配偶", "先生": "配偶", "老公": "配偶", "丈夫": "配偶", "配偶": "配偶",
    "爸爸": "父母", "媽媽": "父母", "父親": "父母", "母親": "父母", "父母": "父母",
    "爺爺": "祖父母", "奶奶": "祖父母", "外公": "祖父母", "外婆": "祖父母", "祖父": "祖父母", "祖母": "祖父母",
    "兒子": "子女", "女兒": "子女", "小孩": "子女", "子女": "子女",
    "哥哥": "兄弟姊妹", "姐姐": "兄弟姊妹", "姊姊": "兄弟姊妹", "弟弟": "兄弟姊妹", "妹妹": "兄弟姊妹", "兄弟": "兄弟姊妹",
    "岳父": "配偶之父母", "岳母": "配偶之父母", "公公": "配偶之父母", "婆婆": "配偶之父母",
    "曾祖": "曾祖父母",
}
_KIN_OTHER = re.compile(r"舅舅|舅媽|姑姑|姑丈|叔叔|嬸嬸|伯父|伯母|阿姨|姨丈|表[哥姐姊弟妹]|堂[哥姐姊弟妹]|姪[子女]|外甥|孫[子女]|乾[爹媽]")
_KIN_TOPIC = re.compile(r"喪|奠儀|過世|去世|往生|婚|生育|補助|慰問")
# SUPPORT_CHECK：是非題（可以嗎／有沒有／會不會）回答「可以／有」之前，另開對話用思考模式做一次「條文有沒有明確涵蓋」的核對。
# 動機：8B 模型會把名單外的對象套進最接近的一列（舅舅→兄弟姊妹、姑姑→祖父母），prompt 規則擋不住（v6 實測）
SUPPORT_CHECK = os.getenv("SUPPORT_CHECK", "0") == "1"
_YESNO_Q = re.compile(r"可以|可不可以|能不能|會不會|有沒有|嗎[？?]?$")
# 第 6 輪：程式化檢查（agent/checks.py）
SHOW_WORK = os.getenv("SHOW_WORK", "0") == "1"  # 數字題的答案沒寫出算式（就算用過計算機）也送驗算
ARITH_VERIFY = os.getenv("ARITH_VERIFY", "0") == "1"  # 答案裡的「算式＝數字」用計算機重算，算錯就附正確值退回
CITE_READ = os.getenv("CITE_READ", "0") == "1"        # 引用的文件只看過片段 → 程式讀全文給模型核對
TERM_CHECK = os.getenv("TERM_CHECK", "0") == "1"      # 「可以…嗎」問的動作在條文裡完全沒出現 → 找不到
_PERMIT_Q = re.compile(r"(可以|可不可以|能不能|能否|可否|能).{0,12}(嗎|？|\?)$")
ACTION_MAP = [  # (問題裡的口語, 條文裡會出現的寫法)
    (("預支", "預借", "借支", "先借"), ("預支", "預借", "借支", "暫支", "預付")),
    (("分期",), ("分期",)),
    (("代領", "幫我領", "代為申請"), ("代領", "代為申請", "代理人")),
    (("轉讓", "讓給"), ("轉讓", "讓與")),
]
TERM_PROMPT = (
    "使用者問：「{q}」\n這是在問能不能做某件事。請找出他想做的那個動作或項目（2–4 個字，例如「預支」「換成補休」「坐豪華經濟艙」），"
    "再列出公司規章裡可能使用的 2–4 個同義寫法（例如「預支」→「預借」「借支」；「留到明年」→「遞延」「保留至次年」）。"
    "只輸出 JSON：{{\"term\": \"…\", \"synonyms\": [\"…\"]}}")
NOT_FOUND = "抱歉，目前的規章文件裡找不到這個問題的資訊，建議直接聯繫人資部。"
SUPPORT_PROMPT = (
    "員工的問題：{q}\n\n小幫手的回答：\n<<<\n{a}\n>>>\n\n規章原文：\n<<<\n{evidence}\n>>>\n\n"
    "請核對：這個回答的結論，規章原文有沒有**明確**支持？逐項檢查：\n"
    "1. 問題問到的對象或事項（人、親屬、項目、做法），原文有沒有直接寫到，或用意思相同的條文用語寫到"
    "（例如 太太＝配偶、奶奶＝祖父母 算同一個；但條文沒列出的親屬，不能比照最接近的一列）。\n"
    "2. 「可以／不可以」「有／沒有」的結論，原文有沒有直接寫；原文沒寫，就算推論合理也不算支持。\n"
    '只輸出 JSON：{{"supported": true 或 false, "reason": "一句話"}}'
)
_NUMERIC_Q = re.compile(r"\d|幾|多少")
_DEFAULT_RECHECK = (
    "員工的問題：{q}\n\n以下是已經查到的規章內容：\n<<<\n{evidence}\n>>>\n\n"
    "（系統檢查）這題涉及數字（計算或門檻條件），請照順序做：\n"
    "1. 只根據上面的規章內容回答；內容不夠才用 search_related_policies 再查。不准引用上面沒出現的檔名。\n"
    "2. 逐字找出相關條文（注意「以…為基準」「乘以…倍」「前…小時」「第…小時起」「…以上」「未滿…」這類字）。\n"
    "3. 依條文列出完整公式或判斷條件：有基準就要乘基準，有分段就要分段算，有門檻就把員工的條件逐一比較。\n"
    "4. 用 calculate 算出每一個數字或比較。\n"
    "5. 最後只輸出給員工看的最終答案（附算式與 [[檔名]] 出處），不要提到系統檢查。"
)
# 候選可以用 RECHECK_PROMPT_FILE 換掉驗算 prompt（佔位符：{q}、{evidence}）
_rp = os.getenv("RECHECK_PROMPT_FILE")
_rp_default = Path(__file__).with_name("recheck_prompt.md")  # 正式版（改進循環選出的候選）
FRESH_RECHECK = Path(_rp).read_text(encoding="utf-8") if _rp else (
    _rp_default.read_text(encoding="utf-8") if _rp_default.exists() else _DEFAULT_RECHECK)


async def _tool_loop(session, http, msgs, ollama_tools, think, calls, outputs, verbose):
    """跑到模型不再呼叫工具為止，回傳最後的文字答案（None＝超過步數）。"""
    for _ in range(MAX_STEPS):
        r = await http.post(f"{OLLAMA_URL}/api/chat", json={
            "model": MODEL, "messages": msgs, "tools": ollama_tools, "stream": False,
            "think": think, "options": {"temperature": 0},
        })
        r.raise_for_status()
        msg = r.json()["message"]
        msgs.append(msg)
        if not msg.get("tool_calls"):
            return msg.get("content", "").strip()
        for tc in msg["tool_calls"]:
            name, args = tc["function"]["name"], tc["function"].get("arguments") or {}
            calls.append((name, args))
            if verbose:
                print(f"  🔧 {name}({json.dumps(args, ensure_ascii=False)})", file=sys.stderr)
            res = await session.call_tool(name, args)
            text = "\n".join(c.text for c in res.content if getattr(c, "text", None))
            outputs.append(text)
            msgs.append({"role": "tool", "content": text, "tool_name": name})
    return None


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
    nudged = calc_nudged = False
    async with httpx.AsyncClient(timeout=300) as http:
        ans = await _tool_loop(session, http, msgs, ollama_tools, THINK, calls, outputs, verbose)
        # Harness 檢查 1：沒查任何文件就下結論（又不是拒答）→ 退回去要求先查一次
        looked = any(n.endswith(_DOC_TOOLS) for n, _ in calls) if DOC_CHECK == "strict" else bool(calls)
        if ans is not None and not looked and "只能回答" not in ans:
            nudged = True
            msgs.append({"role": "user", "content": "（系統檢查）你還沒有查詢任何文件。請先用 search_related_policies 查詢後再回答。"})
            ans = await _tool_loop(session, http, msgs, ollama_tools, THINK, calls, outputs, verbose)
        # Harness 檢查 1b：沒查文件就拒答 → 自己搜一次，文件庫裡明顯有相關內容就退回
        refused_early = ans is not None and "只能回答" in ans and not any(n.endswith(_DOC_TOOLS) for n, _ in calls)
        if REFUSE_CHECK and refused_early:
            res = await session.call_tool("search_related_policies", {"query": question})
            hits = "\n".join(c.text for c in res.content if getattr(c, "text", None))
            sims = [float(x) for x in _SIM.findall(hits)]
            if (sims and max(sims) >= REFUSE_THRESHOLD) or _COMPANY_WORDS.search(question):
                nudged = True
                calls.append(("search_related_policies", {"query": question, "by": "harness"}))
                outputs.append(hits)
                msgs.append({"role": "user", "content": "（系統檢查）文件庫裡有可能相關的內容，這題可能是公司制度問題。以下是搜尋結果，請根據它回答；"
                             "如果文件裡真的沒有答案，就照規定回答找不到並建議聯繫人資部，不要拒答。\n\n" + hits})
                ans = await _tool_loop(session, http, msgs, ollama_tools, THINK, calls, outputs, verbose)
        # Harness 檢查 2：答案有心算的算式（或問題本身涉及數字）卻沒用計算機 → 驗算
        used_calc = any(n.endswith("calculate") for n, _ in calls)
        refused = ans is not None and "只能回答" in ans
        no_work = SHOW_WORK and ans is not None and bool(_NUMERIC_Q.search(question)) and not _ARITH.search(ans)
        trigger = ans is not None and not refused and (no_work or not used_calc) and (
            bool(_ARITH.search(ans)) or (NUMERIC_TRIGGER and bool(_NUMERIC_Q.search(question))))
        if CALC_CHECK and trigger:
            calc_nudged = True
            if RECHECK_MODE == "fresh" and outputs:
                evidence = "\n\n".join(o for o in outputs if "[[" in o)[:8000]  # 只給文件內容，不給計算機結果
                fresh = [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": FRESH_RECHECK.format(q=question, evidence=evidence)}]
                again = await _tool_loop(session, http, fresh, ollama_tools, RECHECK_THINK or THINK, calls, outputs, verbose)
            else:
                msgs.append({"role": "user", "content": "（系統檢查）你的回答裡有計算，但沒有使用 calculate 工具。請先重新確認條文裡的公式（基準、倍數、分段），再用 calculate 算出每一個數字，最後重寫答案。"})
                again = await _tool_loop(session, http, msgs, ollama_tools, RECHECK_THINK or THINK, calls, outputs, verbose)
            ans = again if again else ans
        # Harness 檢查 2c：引用的文件只看過片段 → 程式讀全文，請模型核對後重寫
        cite_read = False
        if CITE_READ and ans and "只能回答" not in ans:
            missing = checks.unread_citations(ans, outputs)[:2]
            if missing:
                texts = []
                for f in missing:
                    res = await session.call_tool("read_specific_document", {"filename": f})
                    text = "\n".join(c.text for c in res.content if getattr(c, "text", None))
                    calls.append(("read_specific_document", {"filename": f, "by": "harness"}))
                    outputs.append(text)
                    texts.append(text)
                cite_read = True
                msgs.append({"role": "user", "content": "（系統檢查）你引用的文件只看過片段。以下是全文，請核對數字、日期與條件後重寫答案，只根據全文回答。\n\n"
                             + "\n\n".join(texts)[:8000]})
                again = await _tool_loop(session, http, msgs, ollama_tools, THINK, calls, outputs, verbose)
                ans = again or ans
        # Harness 檢查 2d：答案裡的算式用計算機重算，算錯就附正確值退回一次
        arith_fixed = False
        if ARITH_VERIFY and ans and "只能回答" not in ans:
            bad = checks.wrong_equations(ans)
            if bad:
                arith_fixed = True
                lines = "\n".join(f"- {e} 你寫 {w:g}，正確是 {c:g}" for e, w, c in bad)
                msgs.append({"role": "assistant", "content": ans})
                msgs.append({"role": "user", "content": "（系統檢查）以下算式的結果算錯了：\n" + lines
                             + "\n請用正確的數字重算後面所有用到它的步驟與總數，重寫完整答案（不要提到系統檢查）。"})
                again = await _tool_loop(session, http, msgs, ollama_tools, THINK, calls, outputs, verbose)
                ans = again or ans
        # Harness 檢查 2b：問到條文沒列的親屬，答案卻不是找不到 → 改成找不到
        kin_fixed = False
        if KIN_CHECK and ans and "只能回答" not in ans and "找不到" not in ans and _KIN_TOPIC.search(question):
            other = _KIN_OTHER.search(question)
            if other and not any(k in question for k in KIN_MAP):
                kin_fixed = True
                ans = (f"規章裡相關條文列出的親屬沒有「{other.group(0)}」，所以文件沒有這種情況的規定。\n\n" + NOT_FOUND)
        # Harness 檢查 3：是非題的結論要有條文明確支持，否則改成找不到
        support_fixed = False
        if (SUPPORT_CHECK and ans and "只能回答" not in ans and "人資" not in ans and outputs
                and _YESNO_Q.search(question)):
            evidence = "\n\n".join(o for o in outputs if "[[" in o)[:8000]
            body = {"model": MODEL, "stream": False, "think": True, "options": {"temperature": 0, "num_ctx": 12288},
                    "format": {"type": "object", "properties": {"supported": {"type": "boolean"}, "reason": {"type": "string"}},
                               "required": ["supported"]},
                    "messages": [{"role": "user", "content": SUPPORT_PROMPT.format(q=question, a=ans, evidence=evidence)}]}
            r = await http.post(f"{OLLAMA_URL}/api/chat", json=body)
            try:
                verdict = json.loads(r.json()["message"]["content"])
            except (ValueError, KeyError):
                verdict = {"supported": True}
            if verbose:
                print("  核對：", verdict)
            if verdict.get("supported") is False:
                support_fixed = True
                ans = NOT_FOUND
        # Harness 檢查 4：問到「申請動作」（預支、分期…），條文裡完全沒有這個動作的任何寫法 → 文件沒有規定
        #   第一版讓模型自己抽關鍵詞＋同義詞，結果抽出「每年檢查」「延後使用」這種條文不會用的說法，誤殺 6 題；
        #   改成跟親屬對照一樣，由程式維護的對照表（口語 → 規章常見寫法）
        term_fixed = False
        if TERM_CHECK and ans and "只能回答" not in ans and "找不到" not in ans and outputs:
            # 搜尋結果第一行會重複使用者的問題（「搜尋「…預支…」的結果」），要先拿掉，否則一定找得到
            evidence = "\n\n".join(re.sub(r"^搜尋「.*?」的結果.*$", "", o, flags=re.M) for o in outputs if "[[" in o)
            for words, doc_words in ACTION_MAP:
                hit = next((w for w in words if w in question), None)
                if hit and checks.term_in_evidence(doc_words, evidence) is None:
                    term_fixed = True
                    ans = f"查到的規章條文裡沒有「{hit}」的相關規定，所以無法確定可不可以。\n\n" + NOT_FOUND
                    break
    return {"answer": ans if ans is not None else "（超過工具呼叫上限）", "calls": calls, "outputs": outputs,
            "nudged": nudged, "calc_nudged": calc_nudged, "support_fixed": support_fixed,
            "kin_fixed": locals().get("kin_fixed", False), "cite_read": cite_read, "arith_fixed": arith_fixed,
            "term_fixed": term_fixed}


def grade(item: dict, out: dict) -> tuple[bool, str]:
    ans, calls = out["answer"], out["calls"]
    if item.get("refuse"):
        ok = not calls and "只能回答" in ans
        return ok, "拒答且未查工具" if ok else "應拒答"
    if item.get("not_found"):
        ok = bool(calls) and bool(re.search(r"人資|財務", ans)) and bool(re.search(r"找不到|沒有.{0,4}提到|未提及|沒有相關|沒有.{0,4}規定|未.{0,2}明確|沒有.{0,2}明確", ans))
        return ok, "查過且回答找不到" if ok else "應查詢後回答找不到"
    # 「看過全文」：搜尋附了該文件全文，或有呼叫 read_specific_document 讀它
    read = any(f"[[{item['expect_file']}]]\n" in o and ("《全文》" in o or o.startswith("[[")) for o in out.get("outputs", []))
    cited = f"[[{item['expect_file']}]]" in ans
    norm = re.sub(r"\s", "", ans)
    content = any(all(k.replace(" ", "") in norm for k in group) for group in item["must_contain"])
    # must_not_contain：答案寫了錯的結論（例：配偶喪假答「十四日」，但引用條文時又寫到正確的「八日」）
    content = content and not any(k.replace(" ", "") in norm for k in item.get("must_not_contain", []))
    ok = read and cited and content
    return ok, f"看過全文={read} 引用正確={cited} 內容正確={content}" + ("（被提醒先查）" if out.get("nudged") else "") + ("（被要求驗算）" if out.get("calc_nudged") else "")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?")
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--questions", default=str(ROOT / "eval" / "questions.yaml"))
    ap.add_argument("--out", default=str(ROOT / "eval" / "results.json"))
    a = ap.parse_args()
    async with streamablehttp_client(MCP_URL) as (r, w, _), ClientSession(r, w) as session:
        await session.initialize()
        if not a.eval:
            out = await ask(session, a.question or "婚假怎麼請？")
            print(out["answer"])
            return
        items = yaml.safe_load(Path(a.questions).read_text(encoding="utf-8"))
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
                            "calls": out["calls"], "answer": out["answer"],
                            "nudged": out.get("nudged"), "calc_nudged": out.get("calc_nudged")})
        print(f"\n通過 {passed}/{len(items)}")
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
