"""用 Playwright 截 demo 畫面：n8n 聊天頁（問答）、n8n 工作流畫布。

用法：python scripts/take_screenshots.py [chat|workflow|all] 問題1 問題2 ...
"""
import sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"; OUT.mkdir(parents=True, exist_ok=True)
import json, os
# TARGET=router：截分流工作流（ID 在 n8n/router_ids.json），檔名加 router_ 前綴
TARGET = os.getenv("TARGET", "policy")
if TARGET == "router":
    _ids = json.loads((ROOT / "n8n" / "router_ids.json").read_text(encoding="utf-8"))
    WH, WID, PREFIX = _ids["webhook"], _ids["workflow"], "router_"
else:
    WH, WID, PREFIX = "17e6304d-d4c7-44f8-a102-b2fab05599e8", "eQd5OYB5sPhJGoLN", ""
mode = sys.argv[1] if len(sys.argv) > 1 else "all"
QUESTIONS = sys.argv[2:] or ["婚假怎麼請？"]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def shoot_chat(ctx):
    page = ctx.new_page()
    page.goto(f"http://localhost:5678/webhook/{WH}/chat"); page.wait_for_timeout(2500)
    bots = page.locator(".chat-message-from-bot")
    for i, q in enumerate(QUESTIONS):
        before = bots.count()
        page.locator("textarea").first.fill(q)
        page.locator(".chat-input-send-button").first.click()
        log("送出", q)
        last, stable = "", 0
        for _ in range(120):
            page.wait_for_timeout(1000)
            if bots.count() > before:
                txt = bots.last.inner_text()
                stable = stable + 1 if txt == last and txt.strip() else 0
                last = txt
                if stable >= 2:
                    break
        log("回覆", len(last), "字")
        page.screenshot(path=str(OUT / f"{PREFIX}chat_{i + 1}.png"), full_page=True)


def shoot_workflow(ctx):
    lines = (ROOT / "n8n" / "OWNER_LOGIN.txt").read_text(encoding="utf-8").splitlines()
    email, pw = lines[1].split(": ", 1)[1], lines[2].split(": ", 1)[1]
    page = ctx.new_page(); page.set_viewport_size({"width": 1400, "height": 800})
    page.goto("http://localhost:5678/signin"); page.wait_for_timeout(2500)
    page.locator("input[type=email], input[autocomplete=email], input[name=emailOrLdapLoginId]").first.fill(email, timeout=10000)
    page.locator("input[type=password]").first.fill(pw, timeout=10000)
    page.keyboard.press("Enter"); page.wait_for_timeout(3500)
    page.goto(f"http://localhost:5678/workflow/{WID}"); page.wait_for_timeout(5000)
    page.keyboard.press("Escape")
    page.keyboard.press("Shift+1"); page.wait_for_timeout(1500)
    page.screenshot(path=str(OUT / f"{PREFIX}workflow.png"))
    log("workflow 截圖完成")


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 900, "height": 1000}, device_scale_factor=2, locale="zh-TW")
    if mode in ("chat", "all"):
        shoot_chat(ctx)
    if mode in ("workflow", "all"):
        shoot_workflow(ctx)
    b.close()
log("ok", sorted(x.name for x in OUT.iterdir()))
