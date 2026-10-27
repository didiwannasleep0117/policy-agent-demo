"""輕量版 Harness² 改進循環：Propose → Probe → Compose，外加隱藏組檢查。

一個「候選 harness」＝ harness/candidates/<名稱>/ 底下的：
  system_prompt.md   Agent 的 system prompt
  env.yaml           Agent 的開關（THINK、CALC_CHECK…），以環境變數傳給 agent/run_agent.py
  PROPOSAL.md        這個候選改了什麼、為什麼（Propose 階段寫的）

指令：
  python harness/loop.py new <名稱> --from <父候選>        複製一份候選來改
  python harness/loop.py probe <名稱> [--split tune|holdout]  跑驗收，結果記到 scoreboard
  python harness/loop.py fails <名稱> [--split tune]        列出錯題（給 Propose 用；holdout 不准看）
  python harness/loop.py diff <A> <B> [--split tune]        兩個候選逐題比較
  python harness/loop.py board                             成績表
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
H = ROOT / "harness"
CANDS, RUNS, BOARD = H / "candidates", H / "runs", H / "scoreboard.csv"
SPLITS = {"tune": ROOT / "eval" / "tune.yaml", "holdout": ROOT / "eval" / "holdout.yaml",
          "holdout2": ROOT / "eval" / "holdout2.yaml", "holdout3": ROOT / "eval" / "holdout3.yaml",
          "holdout4": ROOT / "eval" / "holdout4.yaml"}
sys.stdout.reconfigure(encoding="utf-8")


def cmd_new(name: str, parent: str) -> None:
    src, dst = CANDS / parent, CANDS / name
    if dst.exists():
        sys.exit(f"{name} 已存在")
    shutil.copytree(src, dst)
    (dst / "PROPOSAL.md").write_text(f"# {name}\n\n父候選：{parent}\n\n## 改了什麼\n\n## 為什麼（根據哪些錯題）\n",
                                     encoding="utf-8")
    print("建立", dst)


def cmd_probe(name: str, split: str) -> None:
    c = CANDS / name
    env = dict(os.environ, PROMPT_FILE=str(c / "system_prompt.md"), PYTHONIOENCODING="utf-8")
    for k, v in (yaml.safe_load((c / "env.yaml").read_text(encoding="utf-8")) or {}).items():
        v = str(v)
        env[k] = str(c / v) if k.endswith("_FILE") else v  # *_FILE 是候選資料夾裡的檔案
    out = RUNS / f"{name}__{split}.json"
    t0 = time.time()
    subprocess.run(["uv", "run", "python", "agent/run_agent.py", "--eval", "--questions", str(SPLITS[split]),
                    "--out", str(out)], cwd=ROOT, env=env, check=True)
    res = json.loads(out.read_text(encoding="utf-8"))
    passed, n = sum(r["ok"] for r in res), len(res)
    avg = sum(r["seconds"] for r in res) / n
    new = not BOARD.exists()
    with BOARD.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "candidate", "split", "passed", "total", "avg_seconds", "wall_minutes"])
        w.writerow([time.strftime("%Y-%m-%d %H:%M"), name, split, passed, n, round(avg, 1),
                    round((time.time() - t0) / 60, 1)])
    print(f"\n[{name} / {split}] 通過 {passed}/{n}，平均 {avg:.1f} 秒")


def load(name: str, split: str) -> list[dict]:
    return json.loads((RUNS / f"{name}__{split}.json").read_text(encoding="utf-8"))


def cmd_fails(name: str, split: str) -> None:
    if split.startswith("holdout"):
        sys.exit("隱藏組的錯題不准拿來改 harness（會失去檢查過擬合的意義）")
    for r in load(name, split):
        if not r["ok"]:
            tools = [n for n, _ in r["calls"]]
            print(f"## {r['q']}\n  判定：{r['why']}  工具：{tools}\n  答案：{r['answer'][:400]}\n")


def cmd_diff(a: str, b: str, split: str) -> None:
    ra, rb = {r["q"]: r for r in load(a, split)}, {r["q"]: r for r in load(b, split)}
    for q in ra:
        x, y = ra[q]["ok"], rb.get(q, {}).get("ok")
        if x != y:
            print(f"{'✅→❌' if x else '❌→✅'} {q}")


def cmd_rescore(name: str, split: str) -> None:
    """評分規則修正後，用存下來的答案重新評分（不重跑模型）。看過全文的判定沿用原本的紀錄。"""
    sys.path.insert(0, str(ROOT / "agent"))
    os.environ.setdefault("PROMPT_FILE", str(ROOT / "agent" / "system_prompt.md"))
    from run_agent import grade
    items = {i["q"]: i for i in yaml.safe_load(SPLITS[split].read_text(encoding="utf-8"))}
    res = load(name, split)
    for r in res:
        it = items[r["q"]]
        read = "看過全文=True" in r["why"]
        fake_out = [f"[[{it['expect_file']}]]\n《全文》"] if (read and it.get("expect_file")) else []
        ok, why = grade(it, {"answer": r["answer"], "calls": r["calls"], "outputs": fake_out,
                             "nudged": r.get("nudged"), "calc_nudged": r.get("calc_nudged")})
        r["ok"], r["why"] = ok, why
    (RUNS / f"{name}__{split}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    passed = sum(r["ok"] for r in res)
    with BOARD.open("a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow([time.strftime("%Y-%m-%d %H:%M"), name + "（重新評分）", split, passed, len(res),
                                round(sum(r["seconds"] for r in res) / len(res), 1), 0])
    print(f"[{name} / {split}] 重新評分：{passed}/{len(res)}")


def cmd_board() -> None:
    print(BOARD.read_text(encoding="utf-8") if BOARD.exists() else "（還沒有紀錄）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new"); p.add_argument("name"); p.add_argument("--from", dest="parent", required=True)
    p = sub.add_parser("probe"); p.add_argument("name"); p.add_argument("--split", default="tune", choices=SPLITS)
    p = sub.add_parser("fails"); p.add_argument("name"); p.add_argument("--split", default="tune", choices=SPLITS)
    p = sub.add_parser("diff"); p.add_argument("a"); p.add_argument("b"); p.add_argument("--split", default="tune")
    p = sub.add_parser("rescore"); p.add_argument("name"); p.add_argument("--split", default="tune", choices=SPLITS)
    sub.add_parser("board")
    a = ap.parse_args()
    {"new": lambda: cmd_new(a.name, a.parent), "probe": lambda: cmd_probe(a.name, a.split),
     "fails": lambda: cmd_fails(a.name, a.split), "diff": lambda: cmd_diff(a.a, a.b, a.split),
     "rescore": lambda: cmd_rescore(a.name, a.split), "board": cmd_board}[a.cmd]()
