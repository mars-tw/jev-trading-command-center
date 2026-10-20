# 決策組織圖

`web/decision-graph.json` 是依來源程式宣告的執行流程，包含來源位置與 `DECLARED_WORKFLOW` provenance；它不是 Graphify AST 擷取結果，也不覆寫現有專案譜。相同拓樸嵌入 `decision-graph.js`，共十二個功能節點、三個群組標題、十八條有向連線，最多六十個重用封包。沒有 force physics 或新增套件。

主線只使用 `LIVE.scanned_at`、roles、reviews、JEV 決策與 PaperBroker 紀錄。首次快照不重播舊交易；後續已觀察事件才送出明亮封包。低亮度巡流只代表圖形待命。沒有個別模型執行證據時不從別名或 READY 狀態推論工作中。

原生 CLI 研究公會獨立顯示六條實際 lane。舊 lane market / signal / risk / counterevidence / comparison / summary 對應 market_patrol / signal_review / risk_hunt / counter_case / cross_check / research_notes。工作封包要求有效工作 ID、相符 running 狀態、整數 PID、process_alive 與新鮮 heartbeat；DEMO 使用明確合成證據，不能成為原生工作。研究產出尚未接入主 JEV。

策略實驗室只接受完整且新鮮的 schema_version 1、ADAPTIVE_PAPER 狀態，且 engine_hook_enabled 與 exchange_execution_enabled 均為 false、main_engine_unchanged 為 true。只有實驗室自己的 proposal → backtest → independent paper → trial memory 迴圈可活動；連往主策略的是沒有箭頭、沒有封包的未接線參考。核心視覺 hook 只寫私有 phase，不覆寫完整的 `window.__strategyLabState`。

主資料 TTL 為十五秒；公會使用有效期與五秒 heartbeat；實驗室 heartbeat TTL 為一百八十秒。暫停會凍結圖形時間、封包、角色 phase 與影格，但狀態仍以五百毫秒受限更新檢查新鮮度，過期立即撤回工作宣稱。縮放會重繪已凍結的 compositor 與角色，不推進時間。

角色使用原有完整透明 PNG 單格、四個視角、四影格步行與工作 API。每個工作者保留 phase；換 lane 平滑過渡，短暫消失重現使用最多六十四筆、十分鐘的幾何記憶，不保存工作授權。完整任務名稱位於舞台下方角色列；圖內短名、節點與實體核心的實際邊界供標籤避讓。桌面短舞台、直向與窄螢幕使用固定排版，沒有加高舞台。

驗證：`node tests/test_decision_graph.cjs`、既有 `test_line_workers.cjs`、`test_game_pet_art.cjs`、`test_core_effects.cjs`。`test_decision_graph_browser.cjs <web> - <receipt-dir>` 使用獨立本機 HTTP fixture 與明示合成 PID，攔截實際服務，不啟動原生工作或交易。瀏覽器驗證涵蓋非同步 PNG、暫停與過期、縮放重繪、390/737/1440 寬度、方向與四影格，以及研究與主線隔離；它不代表真實 CLI 執行或人工美感驗收。
