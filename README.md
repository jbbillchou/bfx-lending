# BFX Lending Bot

Bitfinex 自動化放貸機器人，支援動態單率與網格掛單兩種策略，內建複合式牛熊市指標，配備即時監控 Dashboard。

---

## 給朋友的上手清單（5 步驟）

第一次部署照這個順序做就好，需要 **Docker** 已安裝。

**Step 1 — 取得程式碼**
```bash
git clone <這個 repo 的網址>
cd bfx-lending
```

**Step 2 — 申請 Bitfinex API Key（重要）**
1. 登入 Bitfinex → 右上頭像 → **API Keys** → Create New Key
2. 權限**只勾這兩項**，其餘全部不要勾：
   - ✅ **Funding** — Read & Write（放貸掛單用）
   - ✅ **Wallets** — Read（讀餘額用）
   - ❌ **Withdraw（提領）絕對不要勾** — 防止 key 萬一外洩被搬走資產
3. 建立後複製 **API Key** 與 **API Secret**（Secret 只會顯示一次）

**Step 3 — 設定 `.env`**
```bash
cp .env.example .env
```
編輯 `.env`，填入剛剛的金鑰：
```env
BFX_API_KEY=你的_api_key
BFX_API_SECRET=你的_api_secret
BFX_BASE_URL=https://api.bitfinex.com
BFX_SYMBOLS=UST          # 要放貸的幣種，例如 UST（USDt）
```

**Step 4 — 啟動**
```bash
docker compose up -d --build
```
啟動後開瀏覽器：**http://localhost:5150**

**Step 5 — 開始放貸**
1. Dashboard「利率設定」頁設定保底利率、期數策略
2. 回「總覽」頁點「啟用」排程（預設每 60 分鐘自動掛單）
3. 想收推播可到「設定」頁填 Telegram Bot Token（選用）

> ⚠️ **安全提醒**：這支 Dashboard 沒有登入驗證，**請只在自己電腦/內網跑，不要把 port 對外開放**。完整安全注意事項見下方 [安全注意事項](#安全注意事項自架前務必閱讀) 章節。

常用維護指令：
```bash
docker compose logs -f      # 看即時 log
docker compose down         # 停止
docker compose up -d --build # 改設定後重啟
```

---

## 功能特色

- **自動放貸**：定時取消舊掛單、依市場狀態重新定價、分批下單
- **市場狀態指標**：依 ASK/FRR 比值自動判斷牛/中性市場，並調整溢價、期間等策略參數
- **兩種掛單策略**
  - `single`：動態單率，依趨勢溢價自動調整
  - `grid`：網格掛單，依餘額自動計算層數（上限 10 層）
- **久未成交保底調降**：掛單超過設定時數未成交，自動調低保底利率
- **Telegram 推播**：市場狀態切換、FRR 急升、掛單摘要、每日收益報告等 8 種通知
- **Dashboard**：即時監控、收益追蹤、利率設定、推播管理

---

## 技術架構

| 元件 | 說明 |
|------|------|
| `lending.py` | 放貸核心腳本，由 cron 定時執行 |
| `dashboard/app.py` | Flask Web 介面（port 5000） |
| `dashboard/market_indicator.py` | 牛熊市指標計算（ASK/FRR 比值） |
| `data/` | SQLite + JSON 持久化（Docker volume） |
| Docker + cron | 排程執行環境 |
| AWS ECS (Fargate) | 雲端長駐運行 |
| Cloudflare Tunnel + Access | 遠端存取與身份驗證 |

---

## 快速開始

### 1. 建立 `.env`

```bash
cp .env.example .env  # 若有範例檔
```

編輯 `.env`，填入以下設定：

```env
BFX_API_KEY=your_api_key_here
BFX_API_SECRET=your_api_secret_here
BFX_BASE_URL=https://api.bitfinex.com
BFX_SYMBOLS=UST

# 遠端存取用（本機開發可留空）
CLOUDFLARE_TUNNEL_TOKEN=
```

> Bitfinex API Key 需要 **Funding: Read/Write** 和 **Wallets: Read** 權限。

### 2. 本機啟動

```bash
docker compose up -d --build
```

開啟 Dashboard：http://localhost:5050

> 本機服務不會隨開機自動啟動，需要時手動執行上述指令。

### 3. 啟用排程

在 Dashboard 總覽頁點擊「啟用」，設定自動執行間隔（預設每 60 分鐘）。

---

## Dashboard 頁籤

| 頁籤 | 功能 |
|------|------|
| **總覽** | 排程狀態、市場指標（FRR/BID/ASK）、利率 K 線 + 委託簿 + 歷史成交、掛單列表（點列修改、× 刪除）、已成交放貸 |
| **收益追蹤** | 帳戶總市值 + 資產變化（vs 上月底）+ YTD 年化、加權資金使用率歷史（6H–1Y）、收益明細（月份導航）、利率走勢 |
| **利率設定** | 期數區間策略、保底利率、久未成交降保底、排程間隔 |
| **設定** | 幣種設定、Telegram 通知 |
| **推播** | 推播開關管理 |

---

## 資料持久化

所有資料存放於 `./data/`（Docker volume），重建容器不會遺失：

| 檔案 | 內容 |
|------|------|
| `earnings.db` | SQLite 收益記錄 |
| `settings.json` | 放貸設定 |
| `frr_history.json` | FRR 歷史（近 48 筆） |
| `last_run_detail.json` | 最近一次放貸策略細節 |
| `lending_last.log` | 最近一次執行輸出 |

---

## 安全注意事項（自架前務必閱讀）

這是一支會動到**真實資金**的放貸機器人，自行部署前請確認以下幾點：

1. **API Key 權限最小化** — 在 Bitfinex 建立 API Key 時，**只勾選 Funding（放貸）相關權限**，另加 Wallets: Read 即可。**絕對不要開啟 Withdraw（提領）權限**。這樣即使 Key 外洩，攻擊者最多只能干擾你的放貸掛單，無法把資產轉走。

2. **Dashboard 沒有內建登入驗證** — 所有 API（修改設定、刪除掛單、開關排程）皆無認證。**請勿直接把 port 對外開放**。正確做法擇一：
   - 只在本機 / 內網 / VPN 內存取（最簡單）
   - 對外時前面加一層認證：Cloudflare Access、反向代理 Basic Auth、或 OAuth proxy
   - 本專案正式環境即是走 Cloudflare Tunnel + Access，port 不對外暴露

3. **憑證只放 `.env`，不要寫進程式碼** — `.env`、`dashboard/config.py`、`data/`、`.aws-config.env` 皆已列入 `.gitignore`，請勿提交。要分享部署設定時改用 `*.example` 範本檔。

4. **Telegram Bot Token 為明文** — 啟用推播時 token 會以明文存於 `data/settings.json`。該目錄已 gitignore，但請留意檔案權限，勿任意外流。

5. **先用小額測試** — 放貸有市場風險，建議先以小額資金跑一段時間確認策略行為符合預期，再投入較大金額。

---

## 注意事項

- 本專案僅供學習與個人使用，放貸有市場風險，請自行評估
- `.env` 已加入 `.gitignore`，請勿提交至版本控制
