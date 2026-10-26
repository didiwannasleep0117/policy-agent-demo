"""回歸檢查和 README 分數區塊的邏輯（eval/scores.py、scripts/check_regression.py），不需要模型。

uv run python -m pytest -q tests/test_regression.py
"""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "eval"), str(ROOT / "scripts")]

import scores  # noqa: E402


def latest(tune=45, holdout=15, holdout3=5, total=(45, 17, 17, 9)):
    sp = {"tune": tune, "holdout": holdout, "holdout2": 15, "holdout3": holdout3}
    return {"candidate": "vX", "model": "qwen3:8b", "splits": {
        s: {"passed": p, "total": t, "avg_seconds": 8.2, "time": "2026-10-22 16:24"}
        for (s, p), t in zip(sp.items(), total)}}


BASE = scores.raise_baseline(latest(), {"allowed_drop": 0}, force=True)


def test_no_regression():
    assert scores.compare(latest(), BASE) == []
    assert scores.compare(latest(holdout=16), BASE) == []


def test_drop_fails_each_split():
    assert len(scores.compare(latest(tune=44), BASE)) == 1
    assert len(scores.compare(latest(holdout3=4), BASE)) == 1
    assert len(scores.compare(latest(tune=44, holdout=14, holdout3=3), BASE)) == 3


def test_allowed_drop():
    assert scores.compare(latest(tune=44, holdout3=4), {**BASE, "allowed_drop": 1}) == []
    assert scores.compare(latest(tune=43), {**BASE, "allowed_drop": 1}) != []


def test_total_changed_or_split_missing_fails():
    assert scores.compare(latest(total=(46, 17, 17, 9)), BASE) != []
    only_tune = latest()
    del only_tune["splits"]["holdout2"]
    assert scores.compare(only_tune, BASE) != []


def test_baseline_only_goes_up():
    assert scores.raise_baseline(latest(holdout=14), BASE)["splits"]["holdout"]["passed"] == 15
    up = scores.raise_baseline(latest(holdout=16, holdout3=4), BASE)
    assert up["splits"]["holdout"]["passed"] == 16 and up["splits"]["holdout3"]["passed"] == 5
    assert scores.raise_baseline(latest(tune=40), BASE, force=True)["splits"]["tune"]["passed"] == 40
    assert scores.raise_baseline(latest(), {**BASE, "allowed_drop": 1})["allowed_drop"] == 1
    assert scores.raise_baseline(latest(), {}, force=True)["allowed_drop"] == 1  # 第一次建立基準預設容許 1 題


def test_readme_block_insert_and_replace():
    base = {**BASE, "allowed_drop": 1}
    block = scores.render_block(latest(), base)
    assert "45/45" in block and "100%" in block and "88.2%" in block and "5/9" in block and "55.6%" in block
    assert "CI 基準 ≥ 44" in block and "CI 基準 ≥ 4" in block
    text = "# 標題\n\n> ⚠️ 示範專案\n\n![圖](x.png)\n\n說明：`<!-- EVAL:START -->` ～ `<!-- EVAL:END -->` 之間自動產生\n"
    assert scores.readme_block(text) is None  # 說明文字裡提到標記，不算區塊
    once = scores.replace_block(text, block)
    assert once.index(block) > once.index("示範專案") and once.index(block) < once.index("![圖]")
    assert once.endswith(text[text.index("![圖]"):])  # 說明文字原封不動
    again = scores.replace_block(once, scores.render_block(latest(holdout=16), base))
    assert again.count(scores.START + "\n") == 1 and "16/17" in again
    assert scores.readme_block(again) == scores.render_block(latest(holdout=16), base)


def test_regrade_saved_uses_recorded_full_text_flag():
    item = {"q": "婚假怎麼請？", "expect_file": "請假管理辦法.docx", "must_contain": [["八日"]]}
    r = {"q": item["q"], "answer": "婚假八日 [[請假管理辦法.docx]]", "calls": [["search_related_policies", {}]]}
    assert scores.regrade_saved(item, {**r, "why": "看過全文=True 引用正確=True 內容正確=True"})
    assert not scores.regrade_saved(item, {**r, "why": "看過全文=False 引用正確=True 內容正確=True"})


def test_committed_results_pass_regression_check():
    # 跟 CI 跑的是同一個檢查：commit 進來的 latest.json／baseline.json／README／harness/runs 要一致、沒有退步
    check_regression = importlib.import_module("check_regression")
    assert check_regression.check() == []
