# 目錄（專案結構）

[← 回到 README](../README.zh-TW.md)

```
src/policy_mcp/   MCP 伺服器（loaders / indexer / server）
agent/            system prompt、Python 版 agent 與驗收、程式化檢查（checks.py）、追蹤（tracing.py、trace_report.py）
eval/             驗收題目與結果；scores.py（重新評分、基準、README 分數區塊）、latest.json、baseline.json
harness/          改進循環：候選、跑分結果（runs/）、成績表、經驗庫
n8n/              docker-compose、匯出的工作流
scripts/          假資料產生、n8n 管理、截圖；run_eval.py（本機完整評測）、check_regression.py（CI 回歸檢查）
traces/           追蹤檔（--trace／TRACE=1 產生，不 commit；只有 example.jsonl 是範例）
tests/            不需要模型的單元測試（CI 跑）
.github/workflows/eval-ci.yml   CI
```
