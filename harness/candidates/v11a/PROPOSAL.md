# v11a

父候選：v10c

## 改了什麼

- `LATEX_PLAIN=1`：答案裡的 LaTeX 算式（`$240 \times (1 + \frac{2}{3})$`、`1{,}840`）先用 `checks.latex_to_plain` 轉成一般算式，再交給 FORMULA_CHECK／ARITH_VERIFY；最後顯示給使用者的答案也是轉換後的文字（聊天視窗不會渲染 LaTeX）
- `FORMULA_FALLBACK=1`：FORMULA_CHECK 退回一次後答案還是沒有條文公式算出的數字 → 直接用程式組出的答案（條文原句＋公式＋數字＋[[檔名]]）。跟 KIN_LOOKUP「再錯就用那一句回答」同一招

## 為什麼（根據哪些錯題）

- 第 4 批隱藏組的錯誤類型（只記錄類型，沒看題目細節來改）：加班費分段時模型用 LaTeX 寫算式，`checks.wrong_equations` 讀不到，算錯沒被抓；FORMULA_CHECK 退回一次也沒救回
- 這輪換問法補進調整組 2 題：時薪 260 平日加班 5 小時、時薪 150 平日加班 6 小時
- 經驗庫第 7 輪 lesson：「退回重寫一次」不夠，程式算得出答案的檢查要有保底；算式重算前要先把 LaTeX 轉成一般算式（n8n 版已經這樣做）
