"""n8n 管理小工具（本機 demo 用）：同步 system prompt、測試聊天、查看最近執行紀錄、匯出工作流。

用法：
  python scripts/n8n_admin.py sync-prompt
  python scripts/n8n_admin.py chat "婚假怎麼請？" [session_id]
  python scripts/n8n_admin.py last [n]
  python scripts/n8n_admin.py export
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:5678"
WID = "eQd5OYB5sPhJGoLN"
WEBHOOK = "17e6304d-d4c7-44f8-a102-b2fab05599e8"

_cj = http.cookiejar.CookieJar()
_op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cj))


def req(method: str, path: str, body=None):
    r = urllib.request.Request(BASE + path, method=method,
                               data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json", "browser-id": "demo-admin"})
    try:
        with _op.open(r, timeout=600) as f:
            return f.status, json.loads(f.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def login() -> None:
    lines = (ROOT / "n8n" / "OWNER_LOGIN.txt").read_text(encoding="utf-8").splitlines()
    email, pw = lines[1].split(": ", 1)[1], lines[2].split(": ", 1)[1]
    s, _ = req("POST", "/rest/login", {"emailOrLdapLoginId": email, "password": pw})
    assert s == 200, f"login failed: {s}"


def update_workflow(mutate) -> None:
    _, b = req("GET", f"/rest/workflows/{WID}")
    w = b["data"]
    mutate(w)
    s, b = req("PATCH", f"/rest/workflows/{WID}", {k: w[k] for k in ("nodes", "connections", "versionId", "name", "settings")})
    assert s == 200, b
    s, _ = req("POST", f"/rest/workflows/{WID}/activate", {"versionId": b["data"]["versionId"]})
    print("updated + activated", s)


def sync_prompt() -> None:
    prompt = (ROOT / "agent" / "system_prompt.md").read_text(encoding="utf-8")

    def m(w):
        for n in w["nodes"]:
            if n["type"].endswith(".agent"):
                n["parameters"]["options"]["systemMessage"] = prompt
    update_workflow(m)


def chat(q: str, sid: str | None = None) -> str:
    sid = sid or f"cli-{int(time.time())}"
    r = urllib.request.Request(f"{BASE}/webhook/{WEBHOOK}/chat", method="POST",
                               data=json.dumps({"action": "sendMessage", "sessionId": sid, "chatInput": q}).encode(),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=600) as f:
        return json.loads(f.read()).get("output", "")


def _resolve(arr):
    """n8n 執行資料是 flatted 格式（以字串索引互相參照），還原成一般物件。"""
    def r(v, depth=0):
        if isinstance(v, str) and v.isdigit() and depth > 0 and int(v) < len(arr):
            return r(arr[int(v)], depth + 1)
        if isinstance(v, dict):
            return {k: r(x, depth + 1) for k, x in v.items()}
        if isinstance(v, list):
            return [r(x, depth + 1) for x in v]
        return v
    return r(arr[0], 1)


def last(n: int = 1) -> None:
    flt = urllib.parse.quote(json.dumps({"workflowId": WID}))
    _, b = req("GET", f"/rest/executions?filter={flt}&limit={n}")
    ex = b["data"]
    ex = ex.get("results", ex) if isinstance(ex, dict) else ex
    for e in ex:
        _, d = req("GET", f"/rest/executions/{e['id']}")
        raw = d["data"]["data"]
        full = _resolve(json.loads(raw) if isinstance(raw, str) else raw)
        rd = full["resultData"]["runData"]
        q = rd["員工提問（Chat）"][0]["data"]["main"][0][0]["json"]["chatInput"]
        print(f"== #{e['id']} {e['status']} Q={q}")
        print("  路徑：", " → ".join(k for k in rd if not k.startswith(("Qwen3", "對話記憶", "policy"))))
        if "驗算檢查" in rd:
            chk = rd["驗算檢查"][0]["data"]["main"][0][0]["json"]
            print(f"  驗算檢查：有算式={chk.get('hasArith')} 用過計算機={chk.get('usedCalc')} → 需要驗算={chk.get('needsRecheck')}")
        for name, runs in rd.items():
            if name.startswith("policy"):
                for run in runs:
                    inp = (run.get("inputOverride") or {}).get("ai_tool", [[{}]])[0][0].get("json", {})
                    print(f"  🔧 [{name}]", json.dumps(inp, ensure_ascii=False)[:160])
        final = rd.get("回覆") or rd.get("小芽 AI Agent")
        out = final[0]["data"]["main"][0][0]["json"].get("output", "")
        print("  ->", out[:300].replace("\n", " "))


def export() -> None:
    _, b = req("GET", f"/rest/workflows/{WID}")
    w = b["data"]
    keep = {k: w[k] for k in ("name", "nodes", "connections", "settings")}
    for nd in keep["nodes"]:
        for c in (nd.get("credentials") or {}).values():
            c.pop("id", None)  # 憑證 ID 是本機的，匯入時重選
    out = ROOT / "n8n" / "workflows" / "policy_agent.json"
    out.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
    print("exported", out)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd != "chat":  # 聊天走公開 webhook，不用登入（避免觸發登入頻率限制）
        login()
    if cmd == "sync-prompt":
        sync_prompt()
    elif cmd == "chat":
        print(chat(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
    elif cmd == "last":
        last(int(sys.argv[2]) if len(sys.argv) > 2 else 1)
    elif cmd == "export":
        export()
