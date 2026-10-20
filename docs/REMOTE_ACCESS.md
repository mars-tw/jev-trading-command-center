# 遠端存取

JEV 的遠端模式是「同一台主機上的受信任 HTTPS 反向代理＋應用程式擁有者
密碼」。閘道只監聽 loopback，不可直接綁定 LAN；公開 TLS、DNS 與連接埠由
同主機代理負責。這套模式不依賴 Tailscale，也不把 `Forwarded`、
`X-Forwarded-User` 或其他轉送身分標頭當成登入證明。本文唯一的公開來源
範例是 `https://public.example.invalid`。

本機 `run_app.py` 未帶模式參數時仍是離線 DEMO；8765 與選用的 8766 都維持
loopback。啟用遠端閘道不會把它們改成公開服務。

## 第一次建立擁有者

帳號存放區必須位於版本庫外的私有目錄，並套用只有本機擁有者可讀寫的受
保護 DACL。於專案根目錄執行：

```sh
python -B run_remote.py --setup-owner --auth-store '<private-dir>/owner.json'
```

這會在 `127.0.0.1:8778` 開啟一次性的本機設定頁。建立成功後立即關閉，若
目標存放區已存在則拒絕覆寫。密碼必須是 8 至 128 個 Unicode code point；
存放區只保存使用者名稱摘要、salt、scrypt 參數與雜湊，不保存明文。系統沒有
預設帳密或公開註冊。可用 `--setup-port` 選另一個 loopback 連接埠，或加上
`--terminal` 透過不回顯的輸入完成設定；設定時不需要 `--public-origin`。

## 啟動離線 DEMO 閘道

```sh
python -B run_remote.py --public-origin https://public.example.invalid --auth-store '<private-dir>/owner.json'
```

反向代理必須在同一台主機終止 HTTPS，只轉送至閘道的 loopback 連接埠，並
使用與 `--public-origin` 完全一致的 Host／Origin。公開來源不得含路徑、查詢、
片段或使用者資訊。代理不得繞過應用程式 cookie 驗證，也不得任意注入身分。
未指定唯讀來源時，閘道只供應內建合成 DEMO，不讀 API 金鑰、不連外部 HTTP、
不啟動原生 CLI 或供應商程序。

## 啟動真實唯讀橋

只有已在 `127.0.0.1:8765` 明確啟動的 REAL paper 上游可使用完整唯讀橋：

```sh
python -B run_remote.py --public-origin https://public.example.invalid --auth-store '<private-dir>/owner.json' --upstream http://127.0.0.1:8765 --companion http://127.0.0.1:8766 --market-analysis --enable-antigravity
```

`--market-analysis` 與 DEMO 的組合會被拒絕。上游與伴跑器只能是固定 loopback
來源；一般橋接只允許列入清單的 GET。交易、財務、排程、策略變更與委託 POST
不會轉送。

分析快照 GET 只讀快取，絕不觸發行情擷取。只有擁有者登入、通過 Origin 與
CSRF 的標的選擇 POST，才能替目前選取的 symbol 填入有界、最多八個來源槽的
公開 Binance 快取。只接受已收盤 K 棒與固定十個時間週期：45m 必須由三根
15m、16h 必須由兩根 8h 依 UTC epoch 聚合；60m 與 1h 是同一張資料，24h
就是 1d。

重複或順序錯誤、缺口、尚未收盤、樣本不足，或缺少最新預期已收盤 K 棒，一律
回傳 `UNKNOWN`；不排序補救、不補值、不填洞，也不把即時 K 棒標成已收盤。
來源、實際／預期 as-of、快取 TTL 與資料 stale 狀態都明確回傳。結果固定標記
`research_only`、`analysis_only` 與 `execution_authority=false`，不建立 JEV
證據、不變更策略、不授權委託。

`--enable-antigravity` 只準備公開的十週期研究上下文。工具、MCP、外掛與憑證
隔離尚未證明前，新的原生研究請求會在 spawn 前拒絕。既有 AGY 伴跑器可透過
有界的同源 GET 回報真實 PID、工作與新鮮 heartbeat；設定版本、模型名稱、
顯示標籤或捏造的 PID 都不是供應商正在活動的證明。quota／cooldown 只接受
有限列舉，未知模型維持 `UNVERIFIED`，本版不承諾真實供應商推論。

DEMO 頁不輪詢外部 StrategyLab 狀態，也不接受注入的 native 證明；真實頁的
狀態檔缺少、過期、畸形或過大時只顯示 unavailable。

## 工作階段與請求邊界

- 登入前只有登入頁及必要的登入靜態檔可讀；儀表板、bootstrap、remote
  client、import map 與本機依賴都需要有效工作階段。
- 登入前與登入後都使用不可預測的 `__Host-` cookie，具有 `Secure`、
  `HttpOnly`、`SameSite=Strict`、`Path=/`，且沒有 `Domain`。
- 工作階段閒置期限為 30 分鐘、絕對期限為 8 小時。到期或從 BFCache 返回時，
  會遮蔽整個 body，包含載入後新增的面板。
- 登入前動作與所有登入後 POST 都要驗證對應 CSRF、完全相同的 Origin、合法
  工作階段與嚴格的內容格式。登出只有在請求確認成功後才回報成功，並立即撤銷
  工作階段。
- 登入與各路由都有速率限制、body 大小與欄位界線；重複的重要 header／cookie、
  模稜兩可的 route、request-target、Content-Length／Transfer-Encoding 或其他
  HTTP framing 會直接拒絕。
- 認證回應使用 `no-store`；CSP 的 `script-src` 僅允許同源與實際 HTML 對應的
  明確 SHA-256 雜湊。

密碼 cookie 是唯一的應用程式身分證明。閘道不讀 TLS 私鑰、供應商憑證或公開
代理設定。本文件中的 TF 是 timeframe／多週期，不是 TensorFlow。
