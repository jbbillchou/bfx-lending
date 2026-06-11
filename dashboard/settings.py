import json
import os

SETTINGS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'settings.json')

# Secrets Manager 後端（設定 SETTINGS_SECRET_ARN 時啟用，local + ECS 共用同一份設定）
_SECRET_ARN = os.environ.get('SETTINGS_SECRET_ARN', '')

DEFAULTS = {
    "symbol_enabled": {"UST": True},
    "chunk_amount": {"UST": 500},
    "min_annual_rates": {"UST": 8.0},
    # 保底利率自動模式：開啟後以近 6h 最高成交利率 ceil 到整數作為保底
    "min_annual_auto": {"UST": False},
    "max_amounts": {"UST": None},
    "cron_interval_minutes": 15,
    "schedule_enabled": False,
    "default_period_days": 2,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "daily_report_hour": 21,
    # 推播開關
    "notify_api_failure":   True,
    "notify_order_summary": True,
    "notify_balance_low":   True,
    "notify_stale_floor":   True,
    "notify_daily_report":  True,
    "notify_loan_fill":     True,
    # 久未成交自動降保底
    "stale_floor_enabled": True,
    "stale_offer_hours": 1,
    "stale_floor_decay_pct": 10,
    "stale_floor_min_ratio_pct": 50,
    # 期數區間策略：從最長期數往下掃描 Order Book，找到符合目標利率的 BID 即掛單
    # max_pct: 該期數最多佔最大放貸金額的比例（%），null = 無限制（吃完剩餘資金，不再往下掃）
    "period_rate_targets": [
        {"period": 120, "min_rate": 12.0, "max_pct": None},
        {"period": 30,  "min_rate": 10.0, "max_pct": None},
        {"period": 7,   "min_rate": 8.0,  "max_pct": None},
        {"period": 2,   "min_rate": 6.0,  "max_pct": None}
    ],
}


def _inject_env(data):
    """注入環境變數覆蓋（Telegram token 優先從 env 取）。"""
    if os.environ.get('TELEGRAM_BOT_TOKEN'):
        data['telegram_bot_token'] = os.environ['TELEGRAM_BOT_TOKEN']
    if os.environ.get('TELEGRAM_CHAT_ID'):
        data['telegram_chat_id'] = os.environ['TELEGRAM_CHAT_ID']
    return data


def _sm_load():
    """從 Secrets Manager 讀取設定，失敗回傳 None。"""
    if not _SECRET_ARN:
        return None
    try:
        import boto3
        client = boto3.client('secretsmanager', region_name=_SECRET_ARN.split(':')[3])
        resp = client.get_secret_value(SecretId=_SECRET_ARN)
        return json.loads(resp['SecretString'])
    except Exception as e:
        print(f'[settings] Secrets Manager 讀取失敗，回退本地檔案：{e}')
        return None


def _sm_save(data):
    """寫入 Secrets Manager，失敗靜默（已同步寫本地）。"""
    if not _SECRET_ARN:
        return
    try:
        import boto3
        client = boto3.client('secretsmanager', region_name=_SECRET_ARN.split(':')[3])
        client.put_secret_value(SecretId=_SECRET_ARN, SecretString=json.dumps(data, indent=2))
    except Exception as e:
        print(f'[settings] Secrets Manager 寫入失敗：{e}')


def load():
    # 優先嘗試 Secrets Manager（共享設定）
    if _SECRET_ARN:
        raw = _sm_load()
        if raw is not None:
            return _inject_env({**DEFAULTS, **raw})

    # 本地檔案 fallback
    if not os.path.exists(SETTINGS_PATH):
        data = DEFAULTS.copy()
    else:
        with open(SETTINGS_PATH) as f:
            data = {**DEFAULTS, **json.load(f)}
    return _inject_env(data)


def save(data):
    # 寫入 Secrets Manager（共享）
    _sm_save(data)
    # 同時寫本地（cache / fallback）
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, 'w') as f:
        json.dump(data, f, indent=2)
