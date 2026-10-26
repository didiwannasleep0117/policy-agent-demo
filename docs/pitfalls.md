# 踩過的坑

[← 回到 README](../README.md)

- **n8n 回覆一片空白**：一開始以為是 Qwen3 的思考模式把工具呼叫吃掉，所以改用 OpenAI 相容節點、`extraBody` 加 `{"reasoning_effort": "none"}`。後來在 n8n 和 Ollama 中間架一個記錄用的 proxy，才發現**主因是下一條的工具名前綴**：模型呼叫的工具名對不上，Ollama 就把呼叫丟掉、回空字串。教訓：猜原因之前，先看實際送出去的 request。
- **MCP 節點名稱**：n8n 會用節點名稱當工具名前綴，中文名稱會變成 `______MCP_search...`，模型呼叫的名字對不上就被丟掉。節點用英文命名，改名後要重啟 n8n（工具清單有快取）。
- **FastMCP 的 Host 檢查**：從 Docker 用 `host.docker.internal` 連進來會被 DNS rebinding 防護擋掉，demo 環境關閉它（正式環境應改設 `allowed_hosts`）。
