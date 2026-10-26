# 自動評測「拆成兩半」：實作報告

分支：`eval-ci`（本機，**沒有 push**、沒改 repo 可見度、沒建 release）。基底是 `master` 的 e25971f。做法跟 text2sql-demo 已合併的 `eval-ci` 一樣，差別寫在下面。

## 做了什麼

**CI 這一半（GitHub Actions，不跑模型、不需要 API 金鑰、不用開 MCP 伺服器）**

- `.github/workflows/eval-ci.yml`：只在 push 到 master、對 master 開 PR、手動觸發（workflow_dispatch）時跑。`actions/checkout@v7` → `astral-sh/setup-uv@v10.3.2`（開 cache）→ `uv sync --locked` → `uv run python -m pytest -q tests` → `uv run python scripts/check_regression.py`。權限只有 `contents: read`。
- 這個 repo 原本沒有 `tests/`，也沒有不需要模型的既有測試，所以三個測試檔都是新的（共 70 個案例）：
  - `tests/test_grade.py`（35 個）：評分程式本身
    - `agent/run_agent.py` 的 `grade()`（Python 版驗收、`harness/loop.py probe` 都用它）
      - 算對：搜尋結果附了全文、用 `read_specific_document` 讀全文、任一組關鍵字（八日／8）、空白換行不影響
      - 算錯：正確文件只出現在片段（第 2 名）、引用錯檔／沒引用／沒用 `[[檔名]]` 格式、關鍵字不齊（年終只寫 1.5 沒寫 2.25）
      - 應拒答：沒查就拒答才算對；查了才拒、沒拒、說「找不到」都算錯
      - 應回答找不到：4 種說法算對；沒查就說找不到、沒請員工找人資／財務、自己編答案、拒答都算錯
    - `eval/run_n8n_eval.py` 的 `grade()`：`[[檔名]]` 或 `/docs/<URL 編碼檔名>` 卡片都算引用；拒答、找不到
    - `eval/run_router_eval.py` 的 `answered()`：規章／數據／其他三種路線各自的「答覆正常」判定（9 種）
    - 題庫（questions、tune、holdout、holdout2、holdout3）：題目不重複、`expect_file` 都在 `data/docs/`、`must_contain` 格式正確、照標準造的完美回答兩個評分器都評對、什麼都沒查的「我不知道。」一定評錯
  - `tests/test_checks.py`（27 個）：`agent/checks.py` 的算式重算（README 的 666.67 → 533.33 例子）、申請動作對照、引用讀全文；`run_agent._ARITH` 驗算觸發（README 寫的「婚假共八日」「4,000 元」不會誤判）；`policy_mcp.calc.calculate`（含拒絕 `__import__`、`open`、次方太大、算式太長）；`cn_numbers` 中文數字加註
  - `tests/test_regression.py`（8 個）：退步判定、`allowed_drop`、題數改變／少一組、基準只升不降、README 區塊插入與取代、「看過全文」沿用跑分當下的判定、以及 commit 進來的結果通過回歸檢查（跟 CI 同一個檢查）
- `scripts/check_regression.py`：
  1. 用現在的 `grade()` 和題庫重新評分 `eval/latest.json` 那個候選在 `harness/runs/` 的四組結果，要跟 `latest.json` 一樣；題庫跟結果檔題目不一致也會失敗
  2. `latest.json` 每組答對題數 ≥ `eval/baseline.json` 的基準 − `allowed_drop`（= 1）
  3. README 分數區塊要跟 `latest.json` 一致

**本機這一半**

- `scripts/run_eval.py`：一行指令 `uv run python scripts/run_eval.py`
  - 先檢查 Ollama、`qwen3:8b`、`bge-m3`、規章 MCP 伺服器（`MCP_URL`）有沒有開；沒開就印原因、結束碼 2、不改任何檔案
  - 跑調整組＋三批隱藏組（直接沿用 `harness/loop.py` 的 `cmd_probe`，結果存 `harness/runs/`、記進 `scoreboard.csv`）
  - 寫 `eval/latest.json`；跟基準比，沒退步就把基準往上調（只升不降）；**任何一組退步就整份基準都不動**、結束碼 1
  - 改寫 README 最上面 `<!-- EVAL:START -->`～`<!-- EVAL:END -->` 的區塊（每組分數、百分比、平均秒數、日期、模型、候選、CI 基準）
  - 參數：`--candidate`（預設沿用 latest.json，現在是 v9c）、`--splits`（只重跑某幾組，其餘沿用既有結果）、`--from-existing`、`--accept`、`--dry-run`
- `eval/scores.py`：兩支腳本共用的邏輯。評分直接 import `agent/run_agent.py` 的 `grade()`（評分規則只有一份）。結果檔沒有存工具輸出全文，「看過全文」跟 `loop.py rescore` 一樣沿用 `why` 裡的 `看過全文=True`。寫回 README 時保留 CRLF。
- `eval/latest.json`、`eval/baseline.json`：用 `--from-existing --candidate v9c` 從既有 `harness/runs/v9c__*.json` 產生：調整組 45/45、第 1 批隱藏組 15/17、第 2 批 15/17、第 3 批 5/9（跟 README 原本手寫的一致），`allowed_drop: 1`。
- `pyproject.toml`／`uv.lock`：加 dev 依賴 `pytest>=9.1.1`（`uv add --dev pytest`，只新增 pytest、iniconfig、pluggy）。

**文件**

- `README.md`：最上面加自動產生的分數區塊；在「自動改進循環」前面新增「自動評測：CI 檢查 ＋ 本機跑模型」一節（為什麼拆兩半、CI 檢查什麼、本機怎麼跑、每個分數的意思、限制）；「快速開始」「目錄」補上新指令和檔案。手寫的「改善前後差異」表格和各輪的表格都沒動。
- 本報告。

沒有刪除或改動任何既有的 `harness/runs/`、`scoreboard.csv`、候選、題庫、`eval/results*.json`、`agent/` 程式。

**跟 text2sql-demo 不同的地方**

- 四組（tune＋holdout＋holdout2＋holdout3），不是兩組；所以多了 `--splits`
- `run_eval.py` 多檢查 MCP 伺服器和 bge-m3
- 模型名稱註明 `（只在驗算時思考）`（v9c 的 `RECHECK_THINK: 1`）
- 有一組退步時整份基準都不動（text2sql 版會把「沒退步的其他組」的時間戳記更新進基準，然後又印「基準沒有更新」，訊息會誤導；見下面的實跑紀錄）

## 實際執行的指令與輸出

Windows 本機、repo 根目錄、`eval-ci` 分支：

```
$ uv sync --locked
Resolved 112 packages in 1ms
Checked 109 packages in 4ms

$ uv run python -m pytest -q tests
70 passed in 0.87s

$ uv run python scripts/check_regression.py
[v9c / tune] 重新評分 45/45（latest.json 記錄 45/45）
[v9c / holdout] 重新評分 15/17（latest.json 記錄 15/17）
[v9c / holdout2] 重新評分 15/17（latest.json 記錄 15/17）
[v9c / holdout3] 重新評分 5/9（latest.json 記錄 5/9）
[基準 / tune] 45/45（v9c，2026-10-22 16:24，容許少 1 題）
[基準 / holdout] 15/17（v9c，2026-10-22 16:28，容許少 1 題）
[基準 / holdout2] 15/17（v9c，2026-10-22 16:30，容許少 1 題）
[基準 / holdout3] 5/9（v9c，2026-10-22 16:38，容許少 1 題）
✅ 沒有退步                                 # exit=0

$ uv run python scripts/run_eval.py --from-existing     # 第二次跑
已更新 eval/latest.json（README 不用改）                 # exit=0，內容不變
```

**模擬 CI 環境**：

- 乾淨 clone（`core.autocrlf=false`，LF 換行、新的 .venv）→ `uv sync --locked`、`pytest` 70 passed、`check_regression.py` ✅ exit 0
- Linux 容器 `ghcr.io/astral-sh/uv:python3.11-bookworm-slim`（Docker）裡同樣三個指令：`70 passed in 0.96s`、`✅ 沒有退步`、`exit=0`

**故意弄壞，確認會擋**（每次都用備份還原，最後 `git status` 乾淨、回歸檢查 ✅）：

| 動作 | 結果 |
|---|---|
| `latest.json` 調整組 45 改成 43 | 回歸檢查 exit 1：重新評分不一致、調整組退步（容許少 1 題）、README 不一致（3 項） |
| 手改 README 區塊 holdout2 15/17 → 16/17 | exit 1：README 分數區塊跟 latest.json 不一致 |
| `baseline.json` 第 1 批隱藏組改成 17/17 | exit 1：第 1 批隱藏組退步 15/17、README 不一致 |
| `grade()` 找不到題不再要求「請洽人資／財務」 | pytest 1 failed（`test_not_found_wrong_answers_graded_wrong`）；回歸檢查仍 ✅（v9c 的結果剛好不受影響） |
| `grade()` 不檢查有沒有看過全文 | pytest 2 failed（`test_only_snippet_seen_graded_wrong`、`test_regrade_saved_uses_recorded_full_text_flag`）；回歸檢查仍 ✅（同上） |
| `checks.py` 算式誤差容許 0.5% → 20% | pytest 1 failed（`test_wrong_equations_caught[時薪 200 × 1.5 = 350]`） |
| `tune.yaml` 加一題 | exit 1：題庫跟結果檔不一致（結果檔少 1 題） |
| `OLLAMA_URL=http://127.0.0.1:9 run_eval.py` | exit 2：連不到模型伺服器（ConnectError），提示 `--from-existing` |
| MCP 伺服器沒開時 `run_eval.py` | exit 2：連不到規章 MCP 伺服器，提示 `uv run policy-mcp` |
| 基準 holdout 17、holdout2 14 時 `run_eval.py --from-existing` | exit 1：第 1 批隱藏組退步；`baseline.json` 一個位元組都沒變（holdout2 沒有被偷偷調高） |

所以：**評分規則的錯，主要靠單元測試抓**；回歸檢查抓的是「分數／README／基準／題庫」對不上。

**真的跑了一次模型**（Ollama 0.40.9＋qwen3:8b＋bge-m3 有開；MCP 伺服器從主 repo 用 `uv run policy-mcp` 暫時開起來，跑完關掉）。為了不覆蓋已 commit 的 `harness/runs/v9c__*.json`，在暫存 git worktree（暫時分支 `eval-ci-realrun`）裡跑，跑完 worktree 和暫時分支都刪掉，**結果沒有 commit**：

```
$ uv run python scripts/run_eval.py        # 暫存 worktree，四組 88 題，711 秒
[v9c / tune] 43/45，平均 8.3 秒（2026-10-26 20:37）
[v9c / holdout] 13/17，平均 9.1 秒（2026-10-26 20:39）
[v9c / holdout2] 15/17，平均 6.8 秒（2026-10-26 20:41）
[v9c / holdout3] 5/9，平均 6.4 秒（2026-10-26 20:42）
❌ 調整組退步：43/45，基準 45/45（容許少 1 題）
❌ 第 1 批隱藏組退步：13/17，基準 15/17（容許少 1 題）
有退步：基準沒有更新（同一題每次跑可能 ±1，確定要接受就加 --accept）
exit=1
$ uv run python scripts/check_regression.py    # 同一個 worktree
回歸檢查失敗（2 項）                            # exit=1 —— commit 上去 CI 會紅，符合設計
```

- 這次實跑發現一個問題：當時的 `run_eval.py`（照 text2sql 版）在有退步時，仍把沒退步的 holdout2／holdout3 換成新的時間戳記寫進 `baseline.json`，同時印「基準沒有更新」。已修正（commit 7b4ce5b），用修正版只重跑第 1 批隱藏組（`--splits holdout`，157 秒）：一樣 13/17、錯同樣 4 題、`baseline.json` 沒被動到。
- **分數掉了是真的，不是隨機浮動**：temperature 0，第 1 批隱藏組兩次跑錯的題目、秒數幾乎一樣。跟 10/22 的結果比，變錯的題：
  - 調整組「婚假怎麼請？」：搜尋結果有附請假管理辦法全文（我另外呼叫 MCP 確認過），模型卻回「找不到」
  - 調整組「時薪 180 加班 4 小時」：只算加給的部分（360），10/22 是 1,200
  - 第 1 批「月薪 6 萬考績 A 年終」：漏乘考績倍數（90,000，應為 135,000）
  - 第 1 批「那如果是父親過世呢？」（追問題）：答「直系親屬十四日」，條文是父母八日
- 可能原因（**沒有證實**）：Ollama 在 10/23 從 0.40.6 升級到 0.40.9（`ollama.exe` 修改時間 10/23 00:28，server log 版本號），而 v9c 的分數是 10/22 跑的；模型檔本身（qwen3:8b，10/21 下載）沒變。text2sql-demo 10/26 實跑分數跟 10/22 一樣，所以影響可能只在這個 repo 比較長的工具呼叫／驗算流程上。另外第一次實跑的調整組前半段，跟一個 Docker 容器（上面的 Linux CI 模擬，只用 CPU）同時在跑；第 1 批隱藏組是在容器結束後跑的，結果一樣。
- 我**沒有**把 43/45、13/17 commit 進來，也沒有改基準；目前 commit 的是 10/22 的既有結果。

## 沒有驗證到的

- **GitHub Actions 本身沒跑過**：依規定沒有 push。同樣的指令在 Windows 和 Linux 容器（python 3.11，uv）都跑過、都通過，但不是真的 ubuntu runner。依賴裡有 chromadb，第一次 `uv sync` 大約 1～3 分鐘。
- `actions/checkout@v7`、`astral-sh/setup-uv@v10.3.2` 照 text2sql-demo master 用的版本，這裡沒辦法在 Actions 上確認。
- n8n 版（`run_n8n_eval.py`）和分流（`run_router_eval.py`）只測了評分函式，沒有實跑（要開 n8n）；它們的分數也不在自動區塊裡。
- 重新評分時「看過全文」是沿用跑分當下的判定（結果檔沒存工具輸出），所以改了「看過全文」的判定規則，回歸檢查不會發現；這部分只有單元測試把關。

## 想請滴滴決定的事

1. **實跑分數比 commit 的低**（調整組 43/45、第 1 批隱藏組 13/17，判斷不是隨機浮動）。要不要：(a) 先確認 Ollama 版本的影響（例如裝回 0.40.6 重跑），(b) 接受新分數（`run_eval.py --accept` 後 commit，README 區塊和基準一起降），還是 (c) 維持現在用 10/22 的結果？我沒有替你決定。
2. **text2sql-demo 要不要同步這個修正**：那邊的 `run_eval.py` 在有一組退步時，會把其他組的新時間戳記寫進 `baseline.json`，同時印「基準沒有更新」。分數不會變錯，但訊息誤導、會多一個 diff。改法就是一行（`base = old_base if fails else ...`）。
3. **自動區塊要不要加 n8n 版分數**：目前只有 Python 版（可以用 `harness/runs/` 重新評分）。n8n 結果在 `eval/results_n8n*.json`，這些檔案被 `.gitignore` 掉了，CI 拿不到；要加的話得先決定把哪個結果檔 commit 進來。
4. **結果檔要不要多存「看過全文」欄位**：在 `run_agent.py --eval` 的結果裡加一個 `read` 布林值，就不用從 `why` 字串解析。動到 agent 的輸出格式，所以這次沒做。
5. 第 3 批隱藏組只有 9 題，`allowed_drop = 1` 等於容許掉 11%。要不要對小題組另外設比較鬆或比較嚴的規則？目前四組共用同一個 `allowed_drop`。
