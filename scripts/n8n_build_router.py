"""建立／更新 n8n「分流」工作流：一個聊天入口，依問題類型交給不同的 Agent。

  員工提問（Chat）→ 問題分類（Text Classifier，Qwen3-8B）
     ├─ 規章 → 呼叫原本的小芽規章工作流（HTTP → 它的聊天 webhook，指令和驗算分支都不動）
     ├─ 數據 → 呼叫星芽數據小幫手（HTTP → text2sql-demo 的 /ask，本機 Text-to-SQL 流程）
     └─ 其他 → 再交給規章 Agent 判斷（OTHER_TO_POLICY=1，預設）；=0 則直接固定拒答

每個 Agent 保留自己的指令和工具，互不干擾；原本調好、跑過分數的版本直接沿用。
工作流 ID 和 webhook ID 存在 n8n/router_ids.json（第一次執行時建立）。

用法：python scripts/n8n_build_router.py
"""
from __future__ import annotations

import json
import sys
import uuid

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])
from n8n_admin import ROOT, WEBHOOK, login, req  # noqa: E402

IDS = ROOT / "n8n" / "router_ids.json"
OTHER_TO_POLICY = __import__("os").getenv("OTHER_TO_POLICY", "1") == "1"
NAME = "星芽小幫手（分流：規章／數據）"
TRIGGER = "員工提問（Chat）"
POLICY_URL = f"http://localhost:5678/webhook/{WEBHOOK}/chat"     # n8n 容器內呼叫自己
DATA_URL = "http://host.docker.internal:8002/ask"                 # text2sql-demo（主機上）

CATEGORIES = [
    ("規章", "公司內部規章制度與員工福利：請假、喪假、特休、加班費、考績與年終、補助（結婚、生育、旅遊、教育訓練、健檢）、"
             "出差與報銷、採購請款、遠距工作、特約商店與員工優惠、公司活動。"),
    ("數據", "App 營運與廣告數據的數字查詢：營收、DAU、新安裝、sessions、留存率、ARPDAU、廣告曝光、點擊率、CPI、廣告花費、"
             "廣告短網址或活動成效，通常會提到 App 名稱（芽芽記帳、星芽健身、芽語單字、星芽相機）、國家、平台或期間。"),
]
CLASSIFY_PROMPT = (
    "你是星芽科技內部小幫手的分流器。把員工的問題分到下列其中一類：{categories}。\n"
    "注意：「星芽健身」「芽芽記帳」這類是公司的 App 名稱，問它們的營收、下載、使用人數屬於「數據」；"
    "問員工自己的薪資、年終、補助金額屬於「規章」。兩類都不是（閒聊、天氣、寫程式、翻譯、旅遊規劃）就不要選任何類別。"
    "不要解釋，只輸出 JSON。"
)

POLICY_OUT_JS = r"""
// 規章工作流的回覆（引用已經是檔案卡片）。分類器判「其他」的題目也會送到這裡，由規章 Agent 自己決定要不要拒答
const output = $input.first().json.output ?? '';
const route = output.includes('只能回答') ? '其他' : '規章';
return [{ json: { output, route } }];
"""
DATA_OUT_JS = r"""
// 數據小幫手回傳 markdown（摘要＋表格＋SQL）
const r = $input.first().json;
return [{ json: { output: r.markdown ?? r.answer ?? '', route: '數據', action: r.action } }];
"""
OTHER_JS = r"""
return [{ json: { output: '抱歉，我只能回答星芽科技的規章福利，或 App 營運數據的問題喔。', route: '其他' } }];
"""


def latest(types, name):
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
    v = {n: latest(types, n) for n in ("n8n-nodes-base.code", "n8n-nodes-base.httpRequest",
                                      "@n8n/n8n-nodes-langchain.textClassifier")}
    ids = json.loads(IDS.read_text(encoding="utf-8")) if IDS.exists() else {"webhook": str(uuid.uuid4())}
    nid = lambda: str(uuid.uuid4())  # noqa: E731

    def http(name, pos, url, body_expr):
        return {"id": nid(), "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": v["n8n-nodes-base.httpRequest"],
                "position": pos,
                "parameters": {"method": "POST", "url": url, "sendBody": True, "specifyBody": "json",
                               "jsonBody": body_expr, "options": {"timeout": 300000}}}

    def code(name, pos, js):
        return {"id": nid(), "name": name, "type": "n8n-nodes-base.code", "typeVersion": v["n8n-nodes-base.code"],
                "position": pos, "parameters": {"jsCode": js.strip()}}

    trig = f"$('{TRIGGER}').first().json"
    nodes = [
        {"id": nid(), "name": TRIGGER, "type": "@n8n/n8n-nodes-langchain.chatTrigger", "typeVersion": 1.5,
         "position": [0, 0], "webhookId": ids["webhook"],
         "parameters": {"public": True, "mode": "hostedChat",
                        "initialMessages": "嗨！我是星芽小幫手 🌱\n規章福利（請假、補助、報銷）和 App 營運數據（營收、DAU、廣告成效）都可以問我～",
                        "options": {"title": "星芽小幫手 · 規章＋數據", "subtitle": "示範用虛構公司，所有文件與數據皆為假資料",
                                    "inputPlaceholder": "例如：婚假怎麼請？／上個月各 App 的總營收"}}},
        {"id": nid(), "name": "問題分類", "type": "@n8n/n8n-nodes-langchain.textClassifier",
         "typeVersion": v["@n8n/n8n-nodes-langchain.textClassifier"], "position": [240, 0],
         "parameters": {"inputText": "={{ $json.chatInput }}",
                        "categories": {"categories": [{"category": c, "description": d} for c, d in CATEGORIES]},
                        "options": {"fallback": "other", "systemPromptTemplate": CLASSIFY_PROMPT}}},
        {"id": nid(), "name": "Qwen3-8B（分類）", "type": "@n8n/n8n-nodes-langchain.lmChatOpenAi", "typeVersion": 1.3,
         "position": [240, 220],
         "parameters": {"model": {"__rl": True, "mode": "id", "value": "qwen3:8b"}, "responsesApiEnabled": False,
                        "options": {"temperature": 0, "extraBody": json.dumps({"reasoning_effort": "none"})}},
         "credentials": {"openAiApi": {"id": cred["id"], "name": cred["name"]}}},
        http("規章 Agent", [520, -160], POLICY_URL,
             "={{ JSON.stringify({ action: 'sendMessage', sessionId: 'router-' + " + trig + ".sessionId, chatInput: "
             + trig + ".chatInput }) }}"),
        http("數據 Agent", [520, 20], DATA_URL,
             "={{ JSON.stringify({ question: " + trig + ".chatInput, sessionId: " + trig + ".sessionId }) }}"),
        code("規章回覆", [760, -160], POLICY_OUT_JS),
        code("數據回覆", [760, 20], DATA_OUT_JS),
        code("其他：拒答", [520, 200], OTHER_JS),
    ]
    m = lambda n: {"node": n, "type": "main", "index": 0}  # noqa: E731
    conns = {
        TRIGGER: {"main": [[m("問題分類")]]},
        # 「其他」不直接拒答，改交給規章 Agent 當第二道關卡（分類器把「員工優惠、尾牙」誤判成其他，v1 實測 3/30）
        "問題分類": {"main": [[m("規章 Agent")], [m("數據 Agent")], [m("規章 Agent")] if OTHER_TO_POLICY else [m("其他：拒答")]]},
        "規章 Agent": {"main": [[m("規章回覆")]]},
        "數據 Agent": {"main": [[m("數據回覆")]]},
        "Qwen3-8B（分類）": {"ai_languageModel": [[{"node": "問題分類", "type": "ai_languageModel", "index": 0}]]},
    }
    body = {"name": NAME, "nodes": nodes, "connections": conns, "settings": {"executionOrder": "v1"}}
    if "workflow" in ids:
        _, b = req("GET", f"/rest/workflows/{ids['workflow']}")
        body["versionId"] = b["data"]["versionId"]
        s, b = req("PATCH", f"/rest/workflows/{ids['workflow']}", body)
    else:
        s, b = req("POST", "/rest/workflows", body)
    assert s in (200, 201), b
    ids["workflow"] = b["data"]["id"]
    IDS.write_text(json.dumps(ids, indent=1), encoding="utf-8")
    s, _ = req("POST", f"/rest/workflows/{ids['workflow']}/activate", {"versionId": b["data"]["versionId"]})
    print("router built + activated", s, ids, v)


if __name__ == "__main__":
    main()
