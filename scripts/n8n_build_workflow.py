"""重建 n8n 工作流（含驗算檢查分支）。可重複執行：會覆寫既有工作流的節點，保留 webhook ID。

流程：
  員工提問 → 小芽 AI Agent（快：不思考）→ 驗算檢查（Code）
     ├─ 不需驗算 ────────────────────────────────┐
     └─ 需要驗算 → 小芽 驗算 Agent（思考模式）──→ 回覆（Code）

驗算檢查的規則和 Python 版 agent 一樣：答案裡有「數字 運算子 數字」，
但這一輪沒有呼叫過 calculate → 交給驗算 Agent 重新列公式、用計算機算。

用法：python scripts/n8n_build_workflow.py
"""
from __future__ import annotations

import json
import sys
import uuid

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])
from n8n_admin import ROOT, WID, WEBHOOK, login, req  # noqa: E402

PROMPT = (ROOT / "agent" / "system_prompt.md").read_text(encoding="utf-8")
TRIGGER = "員工提問（Chat）"
THINK_NODE = __import__("os").getenv("THINK_NODE", "ollama")  # ollama | openai

CHECK_JS = r"""
// 驗算檢查：答案有算式，但沒用過 calculate → 需要驗算
const item = $input.first().json;
const draft = (item.output || '').trim();
const steps = item.intermediateSteps || [];
const usedCalc = steps.some(s => String(s?.action?.tool || '').endsWith('calculate'));
const hasArith = /\d[\d,.]*\s*(?:萬|千)?\s*(?:元|小時|個月|倍|日)?\s*[×xX*÷\/+]\s*\(?\s*\d/.test(draft);
const question = $('員工提問（Chat）').first().json.chatInput;
// 問題本身在問數量或帶數字條件（幾個月、多少、35 歲…）→ 也交給思考模式驗算
const asksNumber = /\d|幾|多少/.test(question);
// 把主 Agent 查到的文件內容交給驗算 Agent（實測驗算 Agent 自己常常不查就編）
const evidence = steps.map(s => typeof s?.observation === 'string' ? s.observation : JSON.stringify(s?.observation ?? ''))
  .join('\n\n').slice(0, 8000);
return [{ json: {
  question, draft, usedCalc, hasArith, asksNumber, evidence,
  needsRecheck: !usedCalc && (hasArith || asksNumber),
  tools: steps.map(s => s?.action?.tool).filter(Boolean),
}}];
"""

FINAL_JS = r"""
// 統一輸出給聊天視窗：驗算過就用驗算後的答案
const it = $input.first().json;
return [{ json: { output: (it.output ?? it.draft ?? '').trim() } }];
"""

RECHECK_TEXT = (
    "=員工的問題：{{ $json.question }}\n\n"
    # 不附草稿答案：實測附上草稿會讓模型沿用錯的公式（錨定效應）
    # 改附主 Agent 查到的文件內容：實測驗算 Agent 自己常常不查文件就編
    "以下是已經查到的規章內容：\n<<<\n{{ $json.evidence }}\n>>>\n\n"
    "（系統檢查）這題涉及數字（計算或門檻條件），請照順序做：\n"
    "1. 只根據上面的規章內容回答；內容不夠才用 search_related_policies 再查。不准引用上面沒出現的檔名。\n"
    "2. 逐字找出相關條文（注意「以…為基準」「乘以…倍」「前…小時」「第…小時起」「…歲以上」「未滿…」這類字）。\n"
    "3. 依條文列出完整公式或判斷條件：有基準就要乘基準，有分段就要分段算，有門檻就把員工的條件逐一比較。\n"
    "4. 用 calculate 算出每一個數字或比較（例如 `35 >= 40`）。\n"
    "5. 最後只輸出給員工看的最終答案（附算式與 [[檔名]] 出處），不要提到系統檢查。"
)


def latest(types: list[dict], name: str) -> float:
    vs = []
    for t in types:
        if t["name"] == name:
            v = t["version"]
            vs += v if isinstance(v, list) else [v]
    return max(vs)


def main() -> None:
    login()
    _, types = req("GET", "/types/nodes.json")
    _, creds = req("GET", "/rest/credentials")
    cred = next(c for c in creds["data"] if c["type"] == "openAiApi")
    code_v, if_v = latest(types, "n8n-nodes-base.code"), latest(types, "n8n-nodes-base.if")

    def model(name, pos, think: bool):
        if think and THINK_NODE == "ollama":
            # 原生 Ollama 節點才能明確開 think（OpenAI 相容端點不保證有思考）
            oc = next(c for c in creds["data"] if c["type"] == "ollamaApi")
            return {"id": str(uuid.uuid4()), "name": name, "type": "@n8n/n8n-nodes-langchain.lmChatOllama",
                    "typeVersion": 1, "position": pos,
                    "parameters": {"model": "qwen3:8b", "options": {"temperature": 0, "think": True}},
                    "credentials": {"ollamaApi": {"id": oc["id"], "name": oc["name"]}}}
        opts = {"temperature": 0}
        if not think:
            opts["extraBody"] = json.dumps({"reasoning_effort": "none"})
        return {"id": str(uuid.uuid4()), "name": name, "type": "@n8n/n8n-nodes-langchain.lmChatOpenAi",
                "typeVersion": 1.3, "position": pos,
                "parameters": {"model": {"__rl": True, "mode": "id", "value": "qwen3:8b"},
                               "responsesApiEnabled": False, "options": opts},
                "credentials": {"openAiApi": {"id": cred["id"], "name": cred["name"]}}}

    def mcp(name, pos):
        # 節點名稱必須是英文：n8n 會拿它當工具名前綴
        return {"id": str(uuid.uuid4()), "name": name, "type": "@n8n/n8n-nodes-langchain.mcpClientTool",
                "typeVersion": 1.4, "position": pos,
                "parameters": {"endpointUrl": "http://host.docker.internal:8000/mcp",
                               "serverTransport": "httpStreamable", "include": "all", "options": {}}}

    nodes = [
        {"id": str(uuid.uuid4()), "name": TRIGGER, "type": "@n8n/n8n-nodes-langchain.chatTrigger",
         "typeVersion": 1.5, "position": [0, 0], "webhookId": WEBHOOK,
         "parameters": {"public": True, "mode": "hostedChat",
                        "initialMessages": "嗨！我是小芽 🌱\n星芽科技的規章與福利小幫手，請假、補助、報銷都可以問我～",
                        "options": {"title": "小芽 · 星芽科技規章小幫手", "subtitle": "示範用虛構公司，所有文件皆為假資料",
                                    "inputPlaceholder": "例如：婚假怎麼請？"}}},
        {"id": str(uuid.uuid4()), "name": "小芽 AI Agent", "type": "@n8n/n8n-nodes-langchain.agent",
         "typeVersion": 3.1, "position": [260, 0],
         "parameters": {"promptType": "auto", "options": {"systemMessage": PROMPT, "maxIterations": 8,
                                                         "enableStreaming": False, "returnIntermediateSteps": True}}},
        model("Qwen3-8B（快速）", [160, 240], think=False),
        {"id": str(uuid.uuid4()), "name": "對話記憶", "type": "@n8n/n8n-nodes-langchain.memoryBufferWindow",
         "typeVersion": 1.4, "position": [320, 240], "parameters": {"contextWindowLength": 6}},
        mcp("policy", [480, 240]),
        {"id": str(uuid.uuid4()), "name": "驗算檢查", "type": "n8n-nodes-base.code", "typeVersion": code_v,
         "position": [600, 0], "parameters": {"jsCode": CHECK_JS.strip()}},
        {"id": str(uuid.uuid4()), "name": "需要驗算？", "type": "n8n-nodes-base.if", "typeVersion": if_v,
         "position": [820, 0],
         "parameters": {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                                       "conditions": [{"id": str(uuid.uuid4()), "leftValue": "={{ $json.needsRecheck }}",
                                                       "rightValue": "", "operator": {"type": "boolean", "operation": "true",
                                                                                      "singleValue": True}}],
                                       "combinator": "and"}, "options": {}}},
        {"id": str(uuid.uuid4()), "name": "小芽 驗算 Agent", "type": "@n8n/n8n-nodes-langchain.agent",
         "typeVersion": 3.1, "position": [1060, -120],
         "parameters": {"promptType": "define", "text": RECHECK_TEXT,
                        "options": {"systemMessage": PROMPT, "maxIterations": 8, "enableStreaming": False}}},
        model("Qwen3-8B（思考）", [980, 140], think=True),
        mcp("policy_check", [1140, 140]),
        {"id": str(uuid.uuid4()), "name": "回覆", "type": "n8n-nodes-base.code", "typeVersion": code_v,
         "position": [1320, 0], "parameters": {"jsCode": FINAL_JS.strip()}},
    ]
    main_ = lambda n: {"node": n, "type": "main", "index": 0}  # noqa: E731
    conns = {
        TRIGGER: {"main": [[main_("小芽 AI Agent")]]},
        "小芽 AI Agent": {"main": [[main_("驗算檢查")]]},
        "驗算檢查": {"main": [[main_("需要驗算？")]]},
        "需要驗算？": {"main": [[main_("小芽 驗算 Agent")], [main_("回覆")]]},
        "小芽 驗算 Agent": {"main": [[main_("回覆")]]},
        "Qwen3-8B（快速）": {"ai_languageModel": [[{"node": "小芽 AI Agent", "type": "ai_languageModel", "index": 0}]]},
        "對話記憶": {"ai_memory": [[{"node": "小芽 AI Agent", "type": "ai_memory", "index": 0}]]},
        "policy": {"ai_tool": [[{"node": "小芽 AI Agent", "type": "ai_tool", "index": 0}]]},
        "Qwen3-8B（思考）": {"ai_languageModel": [[{"node": "小芽 驗算 Agent", "type": "ai_languageModel", "index": 0}]]},
        "policy_check": {"ai_tool": [[{"node": "小芽 驗算 Agent", "type": "ai_tool", "index": 0}]]},
    }
    _, b = req("GET", f"/rest/workflows/{WID}")
    w = b["data"]
    s, b = req("PATCH", f"/rest/workflows/{WID}", {"nodes": nodes, "connections": conns, "versionId": w["versionId"],
                                                   "name": w["name"], "settings": w["settings"]})
    assert s == 200, b
    s, _ = req("POST", f"/rest/workflows/{WID}/activate", {"versionId": b["data"]["versionId"]})
    print("built + activated", s, f"(code v{code_v}, if v{if_v})")


if __name__ == "__main__":
    main()
