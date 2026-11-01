# 評測：驗收、CI 與本機跑分

[← 回到 README](../README.zh-TW.md)

## 驗收（`eval/questions.yaml`，14 題）

每題檢查三件事：有沒有看過正確文件的全文、引用的檔名對不對、答案關鍵字對不對；另有「應拒答」「應回答找不到」「追問」題。

| 版本 | 改了什麼 | 通過 | 平均耗時 |
|---|---|---|---|
| v1 | 基本 prompt | 3/10 | ~6s |
| v2 | 強制讀全文、福利含特約商店、只寫文件有的內容、引用格式 | 7/10 | ~6s |
| v3 | 新增追問題；搜尋直接附前 2 名全文（small-to-big）；harness 檢查 | 8/11 | ~5s |
| **v4** | 只附第 1 名全文、只回前 3 名（太多全文會讓 8B 模型離題） | **9/11** | **~4s** |
| v4 + 思考模式 | Qwen3 thinking | 9/11 | ~17s |
| v6 | 新增 3 題計算題（共 14 題）＋ `calculate` 工具＋ harness 檢查「答案有算式卻沒用計算機 → 退回驗算」 | 10/14 | ~4s |
| **v6 + 思考模式** | 同上，開 Qwen3 thinking | **14/14** | ~24s |

**計算機的教訓**：光是「給工具」不夠。v5 只加了 `calculate` 和 prompt，14 題裡模型只用了 1 次，還把公式列錯（漏乘基準 1.5、加班沒分段）。真正有效的是兩件事一起：
1. **Harness 檢查**：答案裡出現「數字 × 數字」這類算式、卻沒呼叫過 `calculate`，就退回去要求「重新確認條文公式，再用計算機算」
2. **思考模式**：模型要先想清楚公式（基準 × 倍數、前 2 小時和第 3 小時分段），計算機只負責算對數字

不開思考模式時，模型被要求驗算後還是列錯公式（例如年終只算 40,000 × 1.2），所以 10/14。**取捨**：要快（~4 秒）就關思考模式、接受計算題可能錯；要準（14/14）就開，每題約 24 秒。實務上可以只在問題需要計算時才開。

跑法：`uv run python agent/run_agent.py --eval`（結果寫到 `eval/results.json`）

**v4 時還沒過的 2 題（v6 用計算機＋思考模式解掉了）**
- 「考績 A 年終幾個月？」條文是「1.5 個月為基準 × 1.5 倍」，模型答 1.5，沒算出 2.25
- 「35 歲可以每年健檢嗎？」條文是 40 歲以上才每年，模型答可以

v6 的做法見上方「計算機的教訓」。

## 自動評測：CI 檢查 ＋ 本機跑模型

模型跑在自己電腦上（Ollama），GitHub 的 CI 機器上沒有模型；為了 CI 去呼叫付費 API 也不划算，而且換了模型分數就不是同一個東西了。所以評測拆成兩半：**CI 只做不需要模型的檢查，真正跑模型的評測在本機做，分數跟著 commit 進來，再由 CI 把關。**

### CI（GitHub Actions：`.github/workflows/eval-ci.yml`）

push 到 master、對 master 開 PR 時自動跑（也可在 Actions 頁面手動觸發），不呼叫模型、不需要 API 金鑰、不用開 MCP 伺服器：

1. **單元測試** `uv run python -m pytest -q tests`
   - `tests/test_grade.py`：評分程式本身對不對（Python 版 `agent/run_agent.py` 的 `grade()`、n8n 版 `eval/run_n8n_eval.py`、分流評測的「答覆正常」判定）
     - 算對：搜尋結果附了全文或用 `read_specific_document` 讀過全文、引用 `[[檔名]]` 正確、任一組關鍵字都有（空白換行不影響）；n8n 的檔案卡片連結也算引用
     - 算錯：正確文件只看過片段、引用錯檔案或沒用 `[[檔名]]`、關鍵字不齊（例：年終只寫 1.5 沒寫 2.25）
     - 應拒答：沒查工具就拒答才算對，查了才拒、或說「找不到」都算錯
     - 應回答找不到：要查過、說找不到、而且請員工找人資／財務；沒查就說找不到、自己編答案都算錯
     - 題庫：每一題的檔案都在 `data/docs/`、題目不重複、照標準造的「完美回答」一定評對、什麼都沒查的回答一定評錯
   - `tests/test_checks.py`：程式化檢查和工具（`agent/checks.py` 的算式重算、申請動作對照、引用讀全文、公式解析、親屬名單、LaTeX 轉換、兩個稱謂的親屬、文件沒寫的福利；驗算觸發的算式偵測；計算機拒絕 `__import__` 這類程式碼、次方太大；中文數字加註）
   - `tests/test_regression.py`：回歸檢查和 README 分數區塊的邏輯
2. **回歸檢查** `uv run python scripts/check_regression.py`
   - 用**現在的**評分規則和題庫，重新評分已 commit 的結果（`harness/runs/<候選>__tune.json`、`__holdout.json`、`__holdout2.json`、`__holdout3.json`、`__holdout4.json`、`__holdout5.json`），分數要跟 `eval/latest.json` 一樣。改了 `grade()` 或題目、讓分數變了，一定會被抓到
   - `eval/latest.json` 每一組答對的題數，不能低於基準 `eval/baseline.json`
   - 兩份 README（英文 `README.md`、中文 `README.zh-TW.md`）最上面的分數區塊都要跟 `eval/latest.json` 一致（不會出現 README 寫的分數跟實際結果對不上）

要注意：
- CI 不知道模型「現在」會怎麼回答。它檢查的是「commit 進來的分數有沒有退步、用現在的評分規則算不算得出來」。改了 prompt、檢查規則或檢索，要在本機重跑評測，CI 才會看到新分數
- 結果檔沒有存工具輸出全文，所以重新評分時「有沒有看過全文」沿用跑分當下的判定（跟 `loop.py rescore` 一樣）
- n8n 版和分流的分數（見 [n8n 版：驗算檢查與分流](n8n.md) 的表格）要開 n8n 才跑得出來，不在自動區塊裡

### 本機：一行指令跑完整評測

```bash
# 先開 Ollama（預設 http://127.0.0.1:11434，可用環境變數 OLLAMA_URL 改；要有 qwen3:8b 和 bge-m3）
# 和規章 MCP 伺服器：uv run policy-mcp（預設 http://127.0.0.1:8000/mcp，可用 MCP_URL 改）
uv run python scripts/run_eval.py                    # 評目前的候選（eval/latest.json 裡記的），調整組＋五批隱藏組 129 題都跑，約 25 分鐘
uv run python scripts/run_eval.py --splits tune      # 只重跑調整組，其他組沿用既有結果
uv run python scripts/run_eval.py --candidate v8     # 換候選（harness/candidates/v8）
uv run python scripts/run_eval.py --from-existing    # 不跑模型：用 harness/runs/ 既有的結果重新評分（改了評分規則、或只想更新 README）
uv run python scripts/run_eval.py --accept           # 題數改了、或確定接受分數下降：基準直接換成這次的結果
uv run python scripts/run_eval.py --from-existing --dry-run   # 只印出結果，不寫任何檔案
```

它會依序：

1. 檢查 Ollama、模型、MCP 伺服器有沒有開（沒開就說明原因、結束碼 2，什麼都不改）
2. 跑調整組和五批隱藏組（跟 `harness/loop.py probe` 一樣：結果存 `harness/runs/`、記進 `harness/scoreboard.csv`）
3. 寫 `eval/latest.json`（候選、模型、每組分數、平均秒數、時間）
4. 跟基準 `eval/baseline.json` 比：**沒退步就把基準往上調**（基準只升不降）；任何一組退步就整份基準都不動，結束碼 1（README 區塊還是會寫成這次的分數，commit 上去 CI 會紅，提醒你分數掉了）
5. 改寫兩份 README 裡 `<!-- EVAL:START -->` ～ `<!-- EVAL:END -->` 之間的分數區塊（不要手動改這段）

跑完把 `harness/runs/`、`harness/scoreboard.csv`、`eval/latest.json`、`eval/baseline.json`、`README.md`、`README.zh-TW.md` 一起 commit。如果分數退步又沒有 `--accept`，CI 會失敗，提醒你這次改動讓分數變差。重跑會覆蓋 `harness/runs/<候選>__<組>.json`，舊結果留在 git 歷史裡。

### 分數怎麼看

- **調整組 tune（58 題）**：改進循環看得到錯題，有被「針對」過，分數偏樂觀。舊的 14 題驗收（`eval/questions.yaml`）都包含在裡面
- **第 1 批隱藏組 holdout（17 題）**：題型類似、題目不同。它找到的錯誤類型（採購問題被拒答）在第 4 輪換個問法補進了調整組，所以不算完全沒看過；第 7 輪又把它的兩種錯誤類型（年終漏乘考績倍數、追問換親屬答錯天數）換問法補進調整組
- **第 2 批隱藏組 holdout2（17 題）**：第 4 輪換上的全新題目，最接近真實表現
- **第 3 批隱藏組 holdout3（9 題）**：第 5 輪換上，刻意出難題（名單外的親屬、條文沒列的情況、文件沒寫可不可以）。題數少，1 題就差 11%，只能當參考
- **第 4 批隱藏組 holdout4（14 題）**：第 7 輪換上的全新題目，在看任何第 7 輪結果之前寫好。不過寫第 7 輪親屬對照（KIN_LOOKUP）的 agent 看過這批題目，親屬題不算完全隱藏（見[第 7 輪紀錄](harness.md#第-7-輪公式和喪假天數交給程式ollama-0409)）。第 8 輪又把它還錯的三種類型（LaTeX 算式的加班費、兩個稱謂的親屬、文件沒寫的福利）換問法補進調整組，這三種類型從此不算沒看過
- **第 5 批隱藏組 holdout5（14 題）**：第 8 輪換上的全新題目，在第 8 輪任何調整、跑分之前寫好並單獨 commit。對第 8 輪處理的三種錯誤類型來說，這是唯一沒被借題的一批。限制：出題的跟寫第 8 輪檢查的是同一個 agent，寫檢查時知道這批題目的寫法，分數偏樂觀（見[第 8 輪紀錄](harness.md#第-8-輪兩個稱謂的親屬文件沒寫的福利latex-算式)）
- **答對**：一般題要「看過正確文件的全文 ＋ 引用正確 ＋ 關鍵字對」三個都成立；拒答題要沒查就拒答；找不到題要查過、說找不到、請員工找人資或財務
- **平均秒數**：每題從問問題到回答完成的時間（追問題含前一題），跟電腦的 GPU 和負載有關，只能在同一台電腦上比較
- **CI 基準**：每組最少要答對幾題。同一題每次跑可能有 ±1 題的浮動，`eval/baseline.json` 的 `allowed_drop` 設定容許少幾題（目前設 1：少 1 題算正常浮動，少 2 題以上 CI 才會失敗）
- 隱藏組的錯題不要拿來改 harness（`loop.py fails --split holdout` 會拒絕；`run_eval.py` 跑分時畫面會逐題顯示對錯，看分數就好）
