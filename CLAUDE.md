# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# 專案規則（覆蓋全域設定）

## Git 規則

此專案允許 commit、push，以及操作 develop / master / main 分支。

---

## 常用指令

```bash
# 本機啟動（port 5150 — workspace port allocation）
docker compose up -d --build

# 查看 log
docker compose logs -f

# 停止服務
docker compose down

# 手動觸發放貸腳本（容器內）
docker compose exec dashboard python3 /app/lending.py

# 資金對帳（錢包總額 vs 可用 / 掛單 / credits / loans）
docker compose exec dashboard python3 /app/diagnose_funds.py
```

> 本專案無獨立 test suite，也無 lint 設定。

---

## 架構說明

**技術堆疊：** Python 3.11、Flask、SQLite、Docker + cron、AWS ECS Fargate、Cloudflare Tunnel

### 兩大主體

| 元件 | 說明 |
|------|------|
| `lending.py` | 放貸核心腳本，由容器內 cron 定時執行。負責取消舊掛單、定價、下單、記錄收益、推播通知。 |
| `dashboard/app.py` | Flask 服務（port 5000），提供監控 UI 與 REST API，同時管理 cron 排程（寫入 crontab）。 |

### `dashboard/` 模組職責

- **`bfx_client.py`** — Bitfinex REST API 封裝（HMAC-SHA384 簽名），所有與交易所的通訊集中於此。
- **`market_indicator.py`** — 牛熊市判斷邏輯（目前未被引用，屬遺留模組）。
- **`settings.py`** — 讀寫 `data/settings.json`，DEFAULTS 為所有設定的單一來源；Telegram token 從 env var 注入（優先於 JSON）。
- **`db.py`** — SQLite 存取，`data/earnings.db` 記錄收益、資金使用率快照與決策歷程（`decision_logs` 表）。
- **`cron_checker.py`** �� 讀取目前 crontab，判斷排程是否啟用。
- **`lending_engine.py`** — 放貸決策共用���組：`sym_val()` 處理 per-symbol 設定向下相���；`scan_period_targets()` 掃描 Order Book BID 側，從最長期數往下匹配目標利率；`resolve_min_annual()` 決定保底利率（auto 模式 = 近 6h 最高成交 ceil 到整數）。
- **`config.py`** — 從���境變數讀取 API 憑證與幣種設定的單一���源（`API_KEY`、`API_SECRET`、`BASE_URL`、`SYMBOLS`）。

### 定價流程（`lending.py`）

1. 久未成交偵測（Adaptive Floor Decay）：若啟用且超過設定時間無新成交，自動衰減保底利率；偵測到新成交時恢復原始保底
2. 取消所有現有掛單，查詢餘額與市場利率（FRR / ASK / BID）
3. 決定保底利率：`resolve_min_annual()` 依 auto/manual 模式取值；若 adaptive floor 啟用中則以衰減後的值覆蓋
4. 期間區間策略：`scan_period_targets()` 掃描 Order Book，從最長期數往下匹配 `period_rate_targets` 設定的目標利率
5. 匹配成功 → 直接以掃描到的利率掛單（保底利率 auto/手動不介入，但 adaptive floor 仍作為下限）
6. 全部未匹配 → fallback 2 天，以保底利率掛單
7. 記錄決策歷程至 `decision_logs` 表，供 Dashboard「決策」頁籤查閱

### 資料持久化（`data/` volume）

JSON 檔（`frr_history.json`、`ask_history.json`���`bid_history.json`）各保留最近 48 筆歷史。`settings.json` 為使用者設定的唯一持久來源。`adaptive_floor_state.json` 記錄久未成交降保底的 runtime 狀態（active、adaptive_floor、original_floor、stale_since_ms、last_loan_ids 等），在 AWS 上儲存於 EFS；本機 `./data/` 為獨��副本，兩���互不影響。

### 部署拓撲

- 容器啟動：`entrypoint.sh` 依序啟動 crond → cloudflared tunnel → Flask（前景）
- Dashboard 無內建登入驗證，請只在本機/內網執行，或自行加一層認證後再對外

### 環境變數（`.env`）

| 變數 | 說明 |
|------|------|
| `BFX_API_KEY` / `BFX_API_SECRET` | Bitfinex API 憑證（需 Funding R/W + Wallets R） |
| `BFX_BASE_URL` | 預設 `https://api.bitfinex.com` |
| `BFX_SYMBOLS` | 放貸幣種，例如 `UST` |
| `CLOUDFLARE_TUNNEL_TOKEN` | 本機開發可留空 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 推播用，覆蓋 settings.json |

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
