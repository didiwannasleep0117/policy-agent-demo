"""CI 回歸檢查（不需要模型、不需要 API 金鑰），GitHub Actions 每次 push／PR 都會跑。

  uv run python scripts/check_regression.py

1. 用「現在的」評分規則（agent/run_agent.py 的 grade）和題庫，重新評分 eval/latest.json 那個候選存在
   harness/runs/ 的結果，分數要跟 latest.json 一樣（改了評分規則或題目、分數掉了一定會被發現）
2. latest.json 每一組答對的題數不能低於 eval/baseline.json 的基準（容許少 allowed_drop 題）
3. README.md（英文）和 README.zh-TW.md 的分數區塊都要跟 latest.json 一致
任何一項不通過 → 結束碼 1。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
sys.stdout.reconfigure(encoding="utf-8")

import scores  # noqa: E402


def check() -> list[str]:
    latest, baseline = scores.read_json(scores.LATEST), scores.read_json(scores.BASELINE)
    if not latest or not baseline:
        return ["缺少 eval/latest.json 或 eval/baseline.json，先在本機跑 scripts/run_eval.py"]
    fails = []
    cand = latest["candidate"]
    for split, s in latest["splits"].items():
        if split not in scores.SPLITS:
            fails.append(f"latest.json 有不認得的組別 {split}")
            continue
        passed, total, _, problems = scores.regrade(cand, split)
        fails += problems
        print(f"[{cand} / {split}] 重新評分 {passed}/{total}（latest.json 記錄 {s['passed']}/{s['total']}）")
        if not problems and (passed, total) != (s["passed"], s["total"]):
            fails.append(f"{split} 重新評分 {passed}/{total}，跟 latest.json 的 {s['passed']}/{s['total']} 不一樣"
                         "（評分規則或題目改了？）。在本機跑 scripts/run_eval.py --from-existing 更新")
    for split, b in baseline.get("splits", {}).items():
        print(f"[基準 / {split}] {b['passed']}/{b['total']}（{b['candidate']}，{b['time']}，容許少 {baseline.get('allowed_drop', 0)} 題）")
    fails += scores.compare(latest, baseline)
    for lang, path in scores.READMES.items():
        if scores.readme_block(scores.read_text(path)) != scores.render_block(latest, baseline, lang):
            fails.append(f"{path.name} 分數區塊跟 eval/latest.json 不一致，在本機跑 scripts/run_eval.py --from-existing 更新")
    return fails


if __name__ == "__main__":
    problems = check()
    for p in problems:
        print("❌", p)
    print("✅ 沒有退步" if not problems else f"回歸檢查失敗（{len(problems)} 項）")
    sys.exit(1 if problems else 0)
