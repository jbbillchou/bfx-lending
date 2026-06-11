# BFX Lending Bot — 功能需求文件

> 最後更新：2026-04-14 v2.6.3

---

## 目錄

1. [系統概述](#1-系統概述)
2. [技術架構](#2-技術架構)
3. [設定檔說明](#3-設定檔說明)
4. [放貸核心邏輯 lending.py](#4-放貸核心邏輯-lendingpy)
5. [Dashboard Web 介面](#5-dashboard-web-介面)
6. [API 端點](#6-api-端點)
7. [資料庫](#7-資料庫)
8. [排程機制](#8-排程機制)
9. [Docker 部署](#9-docker-部署)
10. [資料流向](#10-資料流向)
11. [新人 Onboarding 步驟](#11-新人-onboarding-步驟)

---

## 1. 系統概述

**BFX Lending Bot** 是一套自動化的 Bitfinex 加密貨幣放貸系統，目前支援：

| 幣種 | 預設每單開單量 | 最低餘額門檻 |
|------|-------------|------------|
| UST  | 500 顆       | 150        |

系統由兩個主要元件組成：
- **`lending.py`**：放貸核心腳本，由 cron 定時執行
- **Dashboard**：Flask 網頁介面，提供即時監控與設定管理

### 放貸策略：期數區間掃描

系統採用「期數區間策略」，每次排程執行時掃描 Order Book BID 側，從最長期數往下找符合目標利率的 BID，第一個匹配即掛單。全部未匹配時以 2 天 + 保底利率（`min_annual_rates`、自動保底或 adaptive floor）作為 fallback 掛單。保底利率可由 `min_annual_auto` 啟用「自動抓」模式：以近 6 小時最高成交利率無條件進位到整數作為當次保底。

---

## 2. 技術架構

```
bfx-lending/
├── lending.py                  # 放貸核心腳本
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh               # 容器啟動腳本
├── requirements.txt
├── data/                       # Docker volume（持久化）
│   ├── earnings.db             # SQLite 收益記錄
│   ├── settings.json           # 放貸設定
│   ├── frr_history.json        # FRR 歷史（保留最近 48 筆）
│   ├── ask_history.json        # ASK 歷史（同上）
│   ├── bid_history.json        # BID 歷史（同上）
│   ├── last_run_detail.json    # 最近一次放貸策略細節
│   ├── notify_cooldown.json    # 推播冷卻紀錄
│   ├── daily_report.json       # 每日推播已發送標記
│   ├── adaptive_floor_state.json  # 久未成交保底調降狀態
│   └── lending_last.log        # 最近一次執行輸出
└── dashboard/
    ├── app.py                  # Flask 應用程式
    ├── bfx_client.py           # Bitfinex API 客戶端
    ├── cron_checker.py         # 排程狀態偵測
    ├── db.py                   # SQLite 操作層
    ├── lending_engine.py       # 期數區間掃描引擎
    ├── market_indicator.py     # 市場指標（已停用，保留 stub）
    ├── settings.py             # 設定讀寫模組
    ├── config.py               # API 金鑰（gitignored）
    └── templates/
        └── index.html          # 前端 UI（Tailwind CSS + Vanilla JS）
```

---

## 3. 設定檔說明

### `data/settings.json`

```json
{
  "symbol_enabled":         { "UST": true },
  "chunk_amount":           { "UST": 500 },
  "min_annual_rates":       { "UST": 8.0 },
  "min_annual_auto":        { "UST": false },
  "max_amounts":            { "UST": null },
  "cron_interval_minutes":  15,
  "default_period_days":    2,
  "period_rate_targets": [
    { "period": 120, "min_rate": 12.0, "max_pct": 30 },
    { "period": 30,  "min_rate": 10.0, "max_pct": 40 },
    { "period": 2,   "min_rate": 6.0,  "max_pct": null }
  ],
  "telegram_bot_token":     "",
  "telegram_chat_id":       "",
  "daily_report_hour":      21,
  "notify_api_failure":     true,
  "notify_order_summary":   true,
  "notify_balance_low":     true,
  "notify_stale_floor":     true,
  "notify_daily_report":    true,
  "notify_loan_fill":       true,
  "stale_floor_enabled":    true,
  "stale_offer_hours":      1,
  "stale_floor_decay_pct":  10,
  "stale_floor_min_ratio_pct": 50,
  "schedule_enabled": false
}
```

| 欄位 | 說明 |
|------|------|
| `symbol_enabled` | 各幣種是否啟用放貸 |
| `chunk_amount` | 每筆掛單開單量（顆） |
| `min_annual_rates` | 保底年化利率（%，手動），掛單利率不低於此值；`min_annual_auto` 為 `true` 時忽略此值 |
| `min_annual_auto` | 啟用自動保底：每次排程取近 6h 最高成交利率 `ceil` 到整數作為保底；近 6h 無成交時回退 `min_annual_rates` |
| `max_amounts` | 最大放貸金額（顆），`null` = 不限。可用額度 = `max_amounts - 已放貸 - 掛單中`，每次執行前自動計算 |
| `cron_interval_minutes` | 排程間隔：15 / 30 / 45 / 60 分鐘 |
| `default_period_days` | fallback 借貸天數（預設 2） |
| `period_rate_targets` | 期數區間策略清單，每項含 `period`（天）和 `min_rate`（目標年化 %） |
| `telegram_bot_token` / `telegram_chat_id` | Telegram 推播設定 |
| `daily_report_hour` | 每日推播時間（UTC+8，0–23） |
| `notify_*` | 各類推播開關（`notify_api_failure` / `notify_order_summary` / `notify_balance_low` / `notify_stale_floor` / `notify_daily_report` / `notify_loan_fill`） |
| `stale_floor_enabled` | 啟用久未成交保底調降 |
| `stale_offer_hours` | 連續 N 小時無新成交視為久未成交 |
| `stale_floor_decay_pct` | 每次保底下降 %（例：10 → 保底 × 0.9） |
| `stale_floor_min_ratio_pct` | 保底硬下限（原始保底 × min_ratio_pct%） |
| `period_rate_targets[].max_pct` | 此期數最多佔最大放貸金額的比例（%），`null` = 無限制（吃完剩餘資金，停止繼續掃描） |
| `schedule_enabled` | 排程是否啟用（容器重啟時 entrypoint.sh 自動還原 crontab） |

---

## 4. 放貸核心邏輯 lending.py

### 執行流程

```
for each 幣種:
  0. Adaptive Floor Decay（追蹤 Active Loan ID 差異）
     - 有新成交 → 恢復原始保底，TG 通知
     - 無新成交 ≥ stale_offer_hours → 保底漸降，TG 通知
  1. 取消所有舊掛單
  2. 查詢餘額 + FRR / ASK / BID → 寫歷史
  3. 取得已放貸金額（供 max_amounts 剩餘額度計算與 max_pct 配額判斷）
  4. 期數區間掃描（scan_period_targets）
     - 從 120天 → 30天 → 2天 依序掃 BID Order Book + 近 6h 成交
     - effective_rate = max(best_bid, 近6h成交)，配額檢查（max_pct），Look-ahead 讓路
     - 全部未匹配 → fallback: period=2, rate=保底利率（min_annual / 自動保底 / adaptive_floor）
  5. rate = max(掃描結果, 保底)
  6. auto_lend() 分批下單
  7. TG 通知 + 寫 lending_execution + 寫 run_detail

after loop:
  → 寫 last_run_detail.json
  → 每日收益推播
```

> **保底利率取值**（依優先序）：adaptive_floor（久未成交衰減中）→ 自動保底（`min_annual_auto=true` 時取近 6h 最高成交 `ceil` 整數）→ 手動 `min_annual_rates`。

### 期數區間掃描邏輯（lending_engine.scan_period_targets）

掃描 Bitfinex Order Book BID 側（`amount < 0` = 借方出價），依使用者設定的期數目標從最長往最短逐一檢查：

```
targets sorted by period DESC: [120, 30, 2]
recent_rates = 近 6h 各期數最高成交利率

for each target:
    best_bid = max BID rate where bid.period <= target.period
    effective_rate = max(best_bid, recent_rates[target.period])
    if effective_rate < target.min_rate → skip

    if target.max_pct is not None:
        if lent_by_period[target.period] >= balance × max_pct% → skip（配額滿）

    # Look-ahead：確認更低期數是否有更高 effective_rate
    for lower_target in remaining:
        if lower_effective > effective_rate and lower_effective >= lower.min_rate:
            skip current（讓低期高利承接）

    → 掛 target.period 天，rate = effective_rate

全部未匹配 → period=2, rate=保底利率（min_annual / 自動保底 / adaptive_floor）
注意：近6h成交僅用於 effective_rate 計算，不作為 fallback 掛單利率
```

**effective_rate**：`max(best_bid, 近6h該期最高成交)`，避免 BID 側短暫空洞導致錯失高成交行情。

### 自適應降底（Adaptive Floor Decay）

| 事件 | 動作 |
|------|------|
| 偵測到新 Loan ID（`_prev_loan_ids` 非空時才判斷） | 恢復 active=false，重置計時，保留既有 state 欄位 |
| 無新成交 ≥ stale_offer_hours（首次） | 進入持續衰減模式，`_orig_floor = last_offer_rate`（前次掛單利率） |
| 無新成交 ≥ stale_offer_hours（持續） | **每次排程都衰減**：保底 × (1 - decay_pct%)，`stale_since_ms` 不重置；fallback 掛單利率同步降至 adaptive_floor。到達 `hard_min` 後鎖定不再繼續衰減，掛單保底固定於下限直到有新成交 |
| 餘額不足 | 重置計時（不算未成交） |

**`_new_fills` 偵測規則**：
- `_prev_loan_ids`（上次儲存的 loan IDs）**非空**時，才計算差集判斷是否有新成交
- `_prev_loan_ids` 為空（初始狀態或剛重置）時，跳過偵測，避免既有 loan 被誤判為新成交

**下限計算**：`hard_min = last_offer_rate × (stale_floor_min_ratio_pct / 100)`
- `last_offer_rate`：每次成功掛單後儲存至 `adaptive_floor_state.json`
- 首次部署無 `last_offer_rate` 時，fall back 到 `min_annual`

**`adaptive_floor_state.json` 結構**：
```json
{
  "UST": {
    "active": true,
    "original_floor": 12.5,
    "adaptive_floor": 11.25,
    "stale_since_ms": 1712500000000,
    "last_offer_rate": 12.5,
    "last_loan_ids": ["123456"],
    "applied_at_ms": 1712500000000
  }
}
```

---

## 5. Dashboard Web 介面

### 頁籤

| 頁籤 | 內容 |
|------|------|
| 總覽 | **表頭**：排程狀態 / 啟用・停用・執行・掛機四按鈕 / 健康度 chips（資金使用率・最近到期・當月收益，格式統一，chip-status-* 背景）。當月收益 chip 格式：`累計額(+日均u)`，括號內為當月平均每日收益（累計 ÷ 已過天數）。Header 右側顯示版號（讀自 `VERSION` 檔，每次部署自動遞增 patch，例 `v2.6.3`）與最後更新時間。**面板**：掛單（可收合，點列開啟修改 Modal，× 按鈕刪除）・放貸（相同利率+期數自動分組）・fUST 走勢（K 線 + **掛單利率線**（琥珀色虛線）+ **放貸利率線**（紅色虛線）+ 委託簿 + 歷史成交）。**自動降保底狀態**：可收合區塊，分「上次掛單」（掛單利率、套用時間、活躍筆數）與「下次掛單」（衰減啟用、自適應保底（僅 active=true 時顯示）、衰減基準、保底下限、未成交起始）兩欄，資料來自 `adaptive_floor_state`。**Console**：append 模式 |
| 收益追蹤 | **表頭卡片列**：帳戶總市值 + 資產變化（vs 上月底快照，漲跌幅 + 差額）+ YTD 年化。**加權資金使用率**（歷史走勢折線圖固定於頂，6H/24H/1M/3M/6M/1Y 切換；收合按鈕下方：使用率 × 放貸利率 = 有效年化 三卡視覺化 + 期數分佈，各期進度條以「上限金額 cap」為分母。使用率僅計已成交放貸，不含掛單中）。**收益明細**（表頭顯示當月累計 + 平均每日收益（`均X.XXXX/日`）；當月用已過天數、歷史月份用該月總天數作為除數；月份導航，可收合明細表）。**利率分析**（120d/30d/2d 歷史折線，支援 6H/24H/1M/3M/6M/1Y 切換，>48H 自動改為日聚合） |
| 利率設定 | 無子頁籤，操作列僅保留「試算」與「儲存設定」按鈕。**區塊 1（期數區間策略）**：FRR / ASK / 近 Nh 成交 inline 顯示於標題列；期數表格各欄：BID 最高/最低（上下排）＋獨立「筆」按鈕（可點）、近Nh成交最高/最低（上下排）＋獨立「筆」按鈕（可點）；點擊筆數彈出 `period-detail-modal`，BID 模式顯示個別委託（利率、金額，高→低），成交模式顯示歷史成交（時間、利率、金額，新→舊）。**區塊 2（保底利率）**：UST 年化輸入欄 inline 於標題列右側。**區塊 3（久未成交自動降保底）**：啟用 toggle 靠右；三個參數以 `grid-cols-3` 灰底卡片排列（幾小時 / 下降幅度% / 下限比例%），手機版移除左縮排以避免換行。**排程間隔**：select 靠右對齊。各區塊說明文字隱藏至 `?` popover。**試算功能**：同時 fetch `/api/rate-calc` + `/api/market/period-rates`；掃描表格欄位：期數 / 目標 / 最高 BID / 近6h成交（上下排） / 總計（已放/預期）；移除即時 FRR/BID/ASK 三列；底部可展開「保底衰減預覽」 |
| 設定 | 幣種設定 + Telegram + 久未成交降保底 |
| 推播 | 所有 TG 推播開關 |

---

## 6. API 端點

| Method | Path | 說明 |
|--------|------|------|
| GET | `/api/data` | 錢包、掛單、放貸、排程、保底利率、run_detail、monthly_lending_volume、`version`（讀自 `VERSION` 檔）、`adaptive_floor_state`（25 秒快取）。`monthly_lending_volume` 結構：`{current_month, current_volume, prev_month, prev_volume, current_days}`，`current_days` 為當月已過天數（用於前端計算日均）。`adaptive_floor_state` 結構：`{active, adaptive_floor, original_floor, last_offer_rate, applied_at_ms, stale_since_ms, last_loan_ids_count, stale_floor_min_ratio_pct, stale_floor_enabled}` |
| GET | `/api/rate-calc?symbol=UST` | 期數區間掃描試算（含 allocations / scan_detail / stale floor 狀態：`stale_floor_enabled`, `stale_offer_hours`, `stale_floor_decay_pct`, `stale_floor_min_ratio_pct`, `stale_since_ms`, `adaptive_floor`, `original_floor`, `cron_interval_minutes`） |
| GET | `/api/market/period-rates?symbol=UST&lookback_hours=6` | 各期數 BID 市場利率 + 近 N 小時成交統計（預設 6h，30 秒 cache）。每期回傳：`best_bid`, `min_bid`, `bid_count`, `bid_orders`（個別委託列表，含 rate/period/amount）, `trade_stats`（max/min/count）, `trade_records`（個別成交列表，含 ts/rate_annual/amount/period，由新到舊） |
| GET/POST | `/api/settings` | 讀寫設定 |
| POST | `/api/cron/enable` | 啟用排程（依 `cron_interval_minutes` 寫入 crontab） |
| POST | `/api/cron/disable` | 停用排程（移除 crontab 中 lending.py 排程） |
| POST | `/api/run-now` | 手動立即執行 lending.py |
| POST | `/api/run-on-expiry` | 合約到期觸發放貸（自動重試 2 次 + TG 通知） |
| GET | `/api/lending-log` | 最近一次執行 log |
| GET | `/api/frr-history` | FRR/ASK/BID 歷史（各 48 筆） |
| POST | `/api/earnings/sync?since=YYYY-MM-DD` | 手動同步 Bitfinex 帳本收益；`since` 可指定起始日期補歷史資料（省略則從 DB 最新記錄往後同步） |
| GET | `/api/earnings/sync-status` | 同步狀態（上次時間、是否同步中、錯誤訊息） |
| GET | `/api/earnings/monthly?month=YYYY-MM` | 指定月份每日收益明細 + 月合計 `{month, total, daily}` |
| GET | `/api/earnings/ytd` | YTD 年化統計：基準快照日、年化 %、累積收益、市值、`portfolio_prev_month`（上月底市值）、`portfolio_prev_month_date` |
| GET | `/api/earnings/range?months=N` | 近 N 個月收益合計 `{currency: total}` |
| GET | `/api/loans/weighted` | 當前放貸加權分析：有效年化、使用率、各期段分佈（即時呼叫 BFX API）。`by_period` 各項包含 `cap`（期數上限金額）與 `cap_pct`（amount/cap×100），cap 依 `period_rate_targets[].max_pct` 計算，null 者平分剩餘可用額度 |
| DELETE | `/api/offers/<id>` | 取消單筆掛單（呼叫 BFX `v2/auth/w/funding/offer/cancel`） |
| PUT | `/api/offers/<id>` | 修改單筆掛單：body `{symbol, amount, rate_annual_pct, period_days}`。先取消原單再以新參數重新掛單 |
| GET | `/api/utilization/history?hours=N` | 資金使用率快照歷史。hours ≤ 48：逐筆（MM-DD HH:MM）；hours > 48：按日聚合（MM-DD），最大 8760h（1Y）。每筆含 `ts`、`label`、`util_pct`（僅計已成交放貸）、`weighted_avg_rate`、`total_usd`、`lent_usd` |
| GET | `/api/market/rate-history?days=30&symbol=UST` | 各期段（120d/30d/2d）歷史成交利率。`days` 參數：日線粒度，max/min/count。`hours` 參數（取代 days）：小時粒度，max/avg/count，時間格式 `MM-DD HH:00`，limit=5000 |
| GET | `/api/market/candles?symbol=UST&timeframe=1h&limit=168` | K 線資料（年化 %） |
| GET | `/api/market/orderbook?symbol=UST` | 委託簿（ASK/BID，年化 %） |
| GET | `/api/market/trades?symbol=UST&limit=100` | 近期成交（年化 %） |
| POST | `/api/telegram/test` | 測試推播 |

---

## 7. 資料庫

SQLite：`data/earnings.db`

| 表名 | 用途 |
|------|------|
| `earnings` | Ledger 收益記錄 |
| `portfolio_snapshots` | 每日帳戶市值 |
| `lending_executions` | 成交放貸記錄 |
| `utilization_snapshots` | 資金使用率（每 60 分鐘）。欄位：`ts`, `date`, `detail`（總額 JSON）, `available_detail`, `loans_detail`, `offers_detail`, `weighted_avg_rate`（加權平均利率 %，僅計已成交放貸）。`get_wallets()` 失敗時自動 fallback 至 `portfolio_snapshots` 最後一筆，確保快照持續寫入 |

---

## 8. 排程機制

| 間隔 | Cron |
|------|------|
| 15 min | `*/15 * * * *` |
| 30 min | `*/30 * * * *` |
| 45 min | `*/45 * * * *` |
| 60 min | `0 * * * *` |

---

## 9. Docker 部署

```bash
docker compose up -d --build   # 啟動
docker compose down            # 停止
```

---

## 10. 資料流向

```
Bitfinex API
    ├── 公開：tickers (FRR/ASK/BID) + Order Book (期數掃描)
    └── 私有：wallets + offers + loans + ledgers + offer/submit

lending.py (cron 每 15 分鐘)
    ├── Adaptive Floor Decay
    ├── 取消舊掛單 → 查餘額/利率 → 寫歷史
    ├── scan_period_targets() → 掃 BID Order Book
    ├── auto_lend() → 分批下單
    └── TG 通知 + 寫 DB

Dashboard (Flask :5000)
    ├── /api/data → 錢包+掛單+放貸
    ├── /api/rate-calc → 期數掃描試算
    ├── /api/market/period-rates → 各期數 BID 參考
    ├── /api/earnings/monthly|ytd|range → 收益統計
    ├── /api/loans/weighted → 加權資金使用率（有效年化，含 cap/cap_pct）
    ├── /api/offers/<id> DELETE/PUT → 取消/修改單筆掛單
    ├── /api/utilization/history → 歷史使用率快照（6H–1Y，>48h 日聚合）
    ├── /api/market/rate-history → 各期段歷史成交利率（日線 days= / 小時 hours=）
    └── background: utilization snapshot（60min，wallets 失敗自動 fallback）+ 收益同步

entrypoint.sh
    ├── 啟動 crond
    ├── 讀取 settings.json → schedule_enabled = true 時自動還原 crontab
    └── 啟動 cloudflared + Flask
```

---

## 11. 新人 Onboarding

1. `cp dashboard/config.example.py dashboard/config.py`（填 API Key）
2. `docker compose up -d`
3. 開啟 `http://localhost:5050`
4. 利率設定 → 調整期數區間目標
5. 總覽 → 啟用排程
6. 收益追蹤 → 同步收益
