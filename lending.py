# 0 * * * * python lending.py

import sys
import os
import io
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'dashboard'))
from config import API_KEY as api_key, API_SECRET as api_secret

import requests
import json
import time
import hmac
import hashlib
import datetime


class _TeeWriter:
    def __init__(self, original):
        self._original = original
        self._buffer = io.StringIO()
    def write(self, s):
        self._original.write(s)
        self._buffer.write(s)
    def flush(self):
        self._original.flush()
    def getvalue(self):
        return self._buffer.getvalue()
    def reset(self):
        self._buffer = io.StringIO()

_stdout_capture = _TeeWriter(sys.stdout)
sys.stdout = _stdout_capture

SETTINGS_PATH = os.path.join(os.path.dirname(__file__), 'data', 'settings.json')
FRR_HISTORY_PATH = os.path.join(os.path.dirname(__file__), 'data', 'frr_history.json')
ASK_HISTORY_PATH = os.path.join(os.path.dirname(__file__), 'data', 'ask_history.json')
BID_HISTORY_PATH = os.path.join(os.path.dirname(__file__), 'data', 'bid_history.json')
NOTIFY_COOLDOWN_PATH = os.path.join(os.path.dirname(__file__), 'data', 'notify_cooldown.json')
DAILY_REPORT_PATH = os.path.join(os.path.dirname(__file__), 'data', 'daily_report.json')
LAST_RUN_DETAIL_PATH = os.path.join(os.path.dirname(__file__), 'data', 'last_run_detail.json')
ADAPTIVE_FLOOR_STATE_PATH = os.path.join(os.path.dirname(__file__), 'data', 'adaptive_floor_state.json')

from db import init_db, insert_lending_execution, get_today_earnings_summary, insert_decision_log, purge_old_decision_logs, save_trade_rate_snapshot
import bfx_client as _bfx_client
import settings as _settings_module
from lending_engine import (
    sym_val as _sym_val,
    scan_period_targets as _scan_period_targets,
    resolve_min_annual as _resolve_min_annual,
)
init_db()

_TZ8 = datetime.timezone(datetime.timedelta(hours=8))
print(f"=== lending.py {datetime.datetime.now(tz=_TZ8).strftime('%Y-%m-%d %H:%M:%S')} ===")


# ── 歷史紀錄（供圖表使用）──────────────────────────────────────

def _update_frr_history(symbol, annual_pct, max_entries=48):
    try:
        with open(FRR_HISTORY_PATH) as f:
            history = json.load(f)
    except Exception:
        history = {}
    entries = history.get(symbol, [])
    entries.append({'ts': int(time.time()), 'annual_pct': annual_pct})
    history[symbol] = entries[-max_entries:]
    os.makedirs(os.path.dirname(FRR_HISTORY_PATH), exist_ok=True)
    with open(FRR_HISTORY_PATH, 'w') as f:
        json.dump(history, f)


def _update_ask_history(symbol, annual_pct, max_entries=48):
    try:
        with open(ASK_HISTORY_PATH) as f:
            history = json.load(f)
    except Exception:
        history = {}
    entries = history.get(symbol, [])
    entries.append({'ts': int(time.time()), 'annual_pct': annual_pct})
    history[symbol] = entries[-max_entries:]
    os.makedirs(os.path.dirname(ASK_HISTORY_PATH), exist_ok=True)
    with open(ASK_HISTORY_PATH, 'w') as f:
        json.dump(history, f)


def _update_bid_history(symbol, annual_pct, max_entries=48):
    try:
        with open(BID_HISTORY_PATH) as f:
            history = json.load(f)
    except Exception:
        history = {}
    entries = history.get(symbol, [])
    entries.append({'ts': int(time.time()), 'annual_pct': annual_pct})
    history[symbol] = entries[-max_entries:]
    os.makedirs(os.path.dirname(BID_HISTORY_PATH), exist_ok=True)
    with open(BID_HISTORY_PATH, 'w') as f:
        json.dump(history, f)


# ── Telegram / 冷卻 / Adaptive Floor ─────────────────────────

def _send_telegram(token, chat_id, message):
    try:
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': message},
            timeout=10
        )
    except Exception as e:
        print(f'  [Telegram] 推播失敗：{e}')


def _can_notify(key, cooldown_hours=4):
    try:
        with open(NOTIFY_COOLDOWN_PATH) as f:
            data = json.load(f)
    except Exception:
        data = {}
    return (time.time() - data.get(key, 0)) > cooldown_hours * 3600


def _mark_notified(key):
    try:
        with open(NOTIFY_COOLDOWN_PATH) as f:
            data = json.load(f)
    except Exception:
        data = {}
    data[key] = time.time()
    os.makedirs(os.path.dirname(NOTIFY_COOLDOWN_PATH), exist_ok=True)
    with open(NOTIFY_COOLDOWN_PATH, 'w') as f:
        json.dump(data, f)


def _load_adaptive_floor_state():
    try:
        with open(ADAPTIVE_FLOOR_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_adaptive_floor_state(state):
    os.makedirs(os.path.dirname(ADAPTIVE_FLOOR_STATE_PATH), exist_ok=True)
    with open(ADAPTIVE_FLOOR_STATE_PATH, 'w') as f:
        json.dump(state, f, indent=2)


def _write_decision_log(symbol, balance, rates_dict, floor_rate, floor_source,
                         scan_result_data, action, allocations, run_detail_data):
    try:
        _now8 = datetime.datetime.now(tz=_TZ8)
        record = {
            'ts': int(_now8.timestamp() * 1000),
            'date': _now8.date().isoformat(),
            'symbol': symbol,
            'balance': round(balance, 2),
            'frr_annual_pct': rates_dict.get('frr') if rates_dict else None,
            'ask_annual_pct': rates_dict.get('ask') if rates_dict else None,
            'bid_annual_pct': rates_dict.get('bid') if rates_dict else None,
            'floor_rate': round(floor_rate, 4) if floor_rate is not None else None,
            'floor_source': floor_source,
            'scan_detail': json.dumps(scan_result_data.get('scan_detail', []) if scan_result_data else []),
            'action': action,
            'allocations': json.dumps(allocations or []),
            'stdout_log': _stdout_capture.getvalue(),
            'run_detail': json.dumps(run_detail_data or {}),
        }
        insert_decision_log(record)
        purge_old_decision_logs(7)
    except Exception as _e:
        print(f'[DecisionLog] 寫入失敗：{_e}')


# ── API 操作 ──────────────────────────────────────────────────

def get_balance(symbol):
    try:
        wallets = _bfx_client.get_wallets()
        wallet = next((w for w in wallets if w['symbol'] == symbol), None)
        return wallet['available'] if wallet else 0
    except Exception as e:
        print(f"Error fetching balance: {e}")
        try:
            _s = _settings_module.load()
            _tok = _s.get('telegram_bot_token', '').strip()
            _cid = _s.get('telegram_chat_id', '').strip()
            if _tok and _cid and _s.get('notify_api_failure', True) and _can_notify('api_failure'):
                _send_telegram(_tok, _cid, f'❌ Bitfinex API 連線失敗（查詢餘額）')
                _mark_notified('api_failure')
        except Exception:
            pass
        return 0


def get_current_lending_rate(symbol):
    try:
        frr_data = _bfx_client.get_frr()
        if symbol not in frr_data:
            return None
        d = frr_data[symbol]
        return {
            'frr': d['daily_pct'] / 100,
            'market_bid': d['market_bid_daily_pct'] / 100,
            'market_ask': d['market_ask_daily_pct'] / 100,
        }
    except Exception as e:
        print(f"Error fetching lending rate: {e}")
        try:
            _s = _settings_module.load()
            _tok = _s.get('telegram_bot_token', '').strip()
            _cid = _s.get('telegram_chat_id', '').strip()
            if _tok and _cid and _s.get('notify_api_failure', True) and _can_notify('api_failure'):
                _send_telegram(_tok, _cid, f'❌ Bitfinex API 連線失敗（查詢利率）')
                _mark_notified('api_failure')
        except Exception:
            pass
        return None


def auto_lend(balance, period, symbol, chunk, mininum, rate=None):
    lend_rate = rate
    if lend_rate is None:
        return []

    num_orders = int(balance // chunk)
    remaining = balance % chunk

    if num_orders == 0:
        if balance >= mininum:
            oid = place_lend_order(balance, lend_rate, period, symbol)
            return [oid] if oid else []
        return []

    offer_ids = []
    for i in range(num_orders - 1):
        oid = place_lend_order(chunk, lend_rate, period, symbol)
        if oid:
            offer_ids.append(oid)

    last_amount = chunk + remaining
    print(f"  最後一張單：{chunk} + {remaining:.6f}（餘額）= {last_amount:.6f} {symbol}")
    oid = place_lend_order(last_amount, lend_rate, period, symbol)
    if oid:
        offer_ids.append(oid)

    return offer_ids


def place_lend_order(amount, rate, period, symbol):
    api_path = 'v2/auth/w/funding/offer/submit'
    nonce = str(int(time.time() * 1000000))

    body = {
        'type': 'LIMIT',
        'symbol': f'f{symbol}',
        'amount': str(amount),
        'rate': str(rate),
        'period': period
    }

    signature = '/api/' + api_path + nonce + json.dumps(body)
    h = hmac.new(api_secret.encode(), signature.encode(), hashlib.sha384)
    signature = h.hexdigest()

    url = 'https://api.bitfinex.com/' + api_path
    headers = {
        'bfx-nonce': nonce,
        'bfx-apikey': api_key,
        'bfx-signature': signature,
        'content-type': 'application/json'
    }

    response = requests.post(url, headers=headers, data=json.dumps(body))

    if response.status_code == 200:
        data = response.json()
        offer_id = data[4][0] if data and len(data) > 4 and data[4] else None
        print(f"Lend order for {amount} {symbol} created successfully (id={offer_id})")
        return offer_id
    else:
        print("Error:", response.status_code, response.text)
        return None


def _fetch_active_lent(symbol):
    """取得出借方所有成交中的放貸（credits = 借方已用於倉位、loans = 借方借走但未使用）。
    兩者皆為已成交資金，必須合併；任一端點失敗回傳 None，避免把缺漏誤判為新成交。"""
    merged = {}
    for kind in ('credits', 'loans'):
        api_path = f'v2/auth/r/funding/{kind}/f{symbol}'
        nonce = str(int(time.time() * 1000000))
        body = {}
        signature = '/api/' + api_path + nonce + json.dumps(body)
        h = hmac.new(api_secret.encode(), signature.encode(), hashlib.sha384)
        headers = {
            'bfx-nonce': nonce,
            'bfx-apikey': api_key,
            'bfx-signature': h.hexdigest(),
            'content-type': 'application/json'
        }
        try:
            response = requests.post('https://api.bitfinex.com/' + api_path, headers=headers, data=json.dumps(body))
        except Exception:
            return None
        if response.status_code != 200:
            return None
        for loan in response.json():
            if isinstance(loan, list) and len(loan) >= 14:
                merged[loan[0]] = loan
    return list(merged.values())


def _get_active_loan_ids(symbol):
    loans = _fetch_active_lent(symbol)
    if loans is None:
        return None
    return {loan[0] for loan in loans}


def _get_active_loans_detail(symbol):
    """取得當前活躍放貸明細（ID、金額、年化利率、期數）。"""
    loans = _fetch_active_lent(symbol) or []
    return [{
        'id': loan[0],
        'amount': abs(loan[5]),
        'rate_annual_pct': round(loan[11] * 365 * 100, 4),
        'period_days': int(loan[12]),
    } for loan in loans]


def get_open_offers(symbol):
    api_path = f'v2/auth/r/funding/offers/f{symbol}'
    nonce = str(int(time.time() * 1000000))
    body = {}
    signature = '/api/' + api_path + nonce + json.dumps(body)
    h = hmac.new(api_secret.encode(), signature.encode(), hashlib.sha384)
    headers = {
        'bfx-nonce': nonce,
        'bfx-apikey': api_key,
        'bfx-signature': h.hexdigest(),
        'content-type': 'application/json'
    }
    response = requests.post('https://api.bitfinex.com/' + api_path, headers=headers, data=json.dumps(body))
    if response.status_code == 200:
        return [offer[0] for offer in response.json()]
    print(f"Error fetching offers for {symbol}:", response.status_code, response.text)
    return []


def cancel_offer(offer_id):
    api_path = 'v2/auth/w/funding/offer/cancel'
    nonce = str(int(time.time() * 1000000))
    body = {'id': offer_id}
    signature = '/api/' + api_path + nonce + json.dumps(body)
    h = hmac.new(api_secret.encode(), signature.encode(), hashlib.sha384)
    headers = {
        'bfx-nonce': nonce,
        'bfx-apikey': api_key,
        'bfx-signature': h.hexdigest(),
        'content-type': 'application/json'
    }
    response = requests.post('https://api.bitfinex.com/' + api_path, headers=headers, data=json.dumps(body))
    if response.status_code == 200:
        print(f"Cancelled offer {offer_id}")
    else:
        print(f"Failed to cancel offer {offer_id}: {response.text}")


# ── 主程式 ────────────────────────────────────────────────────
SYMBOLS = ['UST']
MINIMUMS = {'UST': 150}

_run_detail = {
    'ts': int(time.time()),
    'symbols': {}
}

for symbol in SYMBOLS:
    mininum = MINIMUMS[symbol]

    # ── 久未成交偵測（Adaptive Floor Decay）───────────────────
    _stale_settings = _settings_module.load()
    _stale_enabled = _stale_settings.get('stale_floor_enabled', True)
    _stale_hours = _stale_settings.get('stale_offer_hours', 1)
    _stale_decay_pct = _stale_settings.get('stale_floor_decay_pct', 10)
    _stale_min_ratio = _stale_settings.get('stale_floor_min_ratio_pct', 50) / 100
    _tg_token_s = _stale_settings.get('telegram_bot_token', '').strip()
    _tg_chat_s = _stale_settings.get('telegram_chat_id', '').strip()
    _adaptive_floor_override = None

    if _stale_enabled:
        _now_ms = int(time.time() * 1000)
        _adp_state = _load_adaptive_floor_state()
        _sym_adp = _adp_state.get(symbol, {})

        _cur_loan_ids = _get_active_loan_ids(symbol)
        _prev_loan_ids = set(_sym_adp.get('last_loan_ids', []))
        _new_fills = (set(_cur_loan_ids) - _prev_loan_ids) if _cur_loan_ids is not None else set()

        if _new_fills:
            # ── 放貸成交通知 ──────────────────────────────────
            if _tg_token_s and _tg_chat_s and _stale_settings.get('notify_loan_fill', True):
                _loan_details = _get_active_loans_detail(symbol)
                _new_loans = [l for l in _loan_details if l['id'] in _new_fills]
                if _new_loans:
                    _fill_total = sum(l['amount'] for l in _new_loans)
                    _rates = [l['rate_annual_pct'] for l in _new_loans]
                    _rate_txt = f"{_rates[0]:.4f}%" if len(set(_rates)) == 1 else \
                                f"{min(_rates):.2f}% ~ {max(_rates):.2f}%"
                    try:
                        _fill_wallets = _bfx_client.get_wallets()
                        _fill_w = next((w for w in _fill_wallets if w['symbol'] == symbol), None)
                        _available = _fill_w['available'] if _fill_w else 0.0
                    except Exception:
                        _available = 0.0
                    _msg_lines = [
                        f'💰 {symbol} 放貸成交（{len(_new_loans)} 筆）',
                        f'放貸金額：{_fill_total:.2f} {symbol}',
                        f'年化利率：{_rate_txt}',
                        f'剩餘可用：{_available:.2f} {symbol}',
                    ]
                    if len(_new_loans) > 1:
                        for _ld in _new_loans:
                            _msg_lines.append(f'  • {_ld["amount"]:.2f} × {_ld["rate_annual_pct"]:.4f}% / {_ld["period_days"]}天')
                    _send_telegram(_tg_token_s, _tg_chat_s, '\n'.join(_msg_lines))
                    print(f'  [Fill] 通知已發送：{len(_new_loans)} 筆新成交，總計 {_fill_total:.2f} {symbol}')

            if _sym_adp.get('active'):
                _orig_floor = _sym_adp.get('original_floor', 5.0)
                print(f'  [Stale] {symbol} 偵測到新成交，保底利率恢復至 {_orig_floor:.2f}%')
                if _tg_token_s and _tg_chat_s and _stale_settings.get('notify_stale_floor', True) and _can_notify(f'adaptive_restore_{symbol}'):
                    _send_telegram(_tg_token_s, _tg_chat_s,
                        f'✅ {symbol} 掛單已成交，保底利率恢復\n'
                        f'恢復至：{_orig_floor:.2f}%'
                    )
                    _mark_notified(f'adaptive_restore_{symbol}')
            _adp_state[symbol] = {
                **_sym_adp,
                'stale_since_ms': None,
                'active': False,
                'last_loan_ids': list(set(_cur_loan_ids)),
                'restored_floor': _sym_adp.get('original_floor') if _sym_adp.get('active') else None,
                'restored_at_ms': _now_ms if _sym_adp.get('active') else None,
            }
            _save_adaptive_floor_state(_adp_state)
        else:
            _stale_since = _sym_adp.get('stale_since_ms') or _now_ms
            _elapsed_h = (_now_ms - _stale_since) / 3_600_000

            if _elapsed_h >= _stale_hours:
                _cur_floor = _sym_adp.get('adaptive_floor') if _sym_adp.get('active') else \
                             (_sym_adp.get('last_offer_rate') or _stale_settings.get('min_annual_rates', {}).get(symbol, 5.0))
                _orig_floor = _sym_adp.get('original_floor') if _sym_adp.get('active') else \
                             (_sym_adp.get('last_offer_rate') or _cur_floor)
                _hard_min = round(_orig_floor * _stale_min_ratio, 4)
                _af = round(max(_cur_floor * (1 - _stale_decay_pct / 100), _hard_min), 4)
                if _af < _cur_floor:
                    _adaptive_floor_override = _af
                    _adp_state[symbol] = {
                        **_sym_adp,
                        'active': True,
                        'original_floor': _orig_floor,
                        'adaptive_floor': _af,
                        'applied_at_ms': _now_ms,
                        'stale_since_ms': _stale_since,  # 不重置，持續衰減直到成交
                        'last_loan_ids': list(_prev_loan_ids if _cur_loan_ids is None else set(_cur_loan_ids)),
                    }
                    _save_adaptive_floor_state(_adp_state)
                    _at_min = _af <= _hard_min
                    _min_note = f'（已達下限 {_hard_min:.4f}%）' if _at_min else f'（每次 -{_stale_decay_pct}%，下限 {_hard_min:.4f}%）'
                    print(f'  [Stale] {symbol} {_elapsed_h:.1f}h 未成交，保底調降 {_cur_floor:.4f}% → {_af:.4f}% {_min_note}')
                    if _tg_token_s and _tg_chat_s and _stale_settings.get('notify_stale_floor', True) and _can_notify(f'stale_{symbol}'):
                        _send_telegram(_tg_token_s, _tg_chat_s,
                            f'⚠️ {symbol} {_elapsed_h:.1f}h 未成交，保底利率調降\n'
                            f'調整前：{_cur_floor:.4f}% → 調整後：{_af:.4f}%{_min_note}'
                        )
                        _mark_notified(f'stale_{symbol}')
                elif _sym_adp.get('active'):
                    _adaptive_floor_override = _sym_adp.get('adaptive_floor')
            elif _sym_adp.get('active'):
                _adaptive_floor_override = _sym_adp.get('adaptive_floor')

            if _cur_loan_ids is not None:
                _adp_state.setdefault(symbol, {}).update({
                    'stale_since_ms': _stale_since,
                    'last_loan_ids': list(set(_cur_loan_ids)),
                })
                _save_adaptive_floor_state(_adp_state)

    # ── 取消舊掛單 ────────────────────────────────────────────
    offer_ids = get_open_offers(symbol)
    for oid in offer_ids:
        cancel_offer(oid)
        time.sleep(0.1)
    # 取消是非同步的：資金要等掛單真正消失後才會回到 available。
    # 若立刻查餘額，尚未釋放的資金不會被重新掛出，只能閒置到下一輪。
    if offer_ids:
        for _ in range(10):
            time.sleep(0.5)
            if not get_open_offers(symbol):
                break
        else:
            print(f'  [Cancel] 等待 5 秒後仍有掛單未取消，部分資金本輪可能無法重新掛出')

    balance = get_balance(symbol)
    rates = get_current_lending_rate(symbol)
    print("get_balance:", balance)
    print("get_current_lending_rate:", rates)

    settings = _settings_module.load()
    if not settings.get('symbol_enabled', {}).get(symbol, True):
        print(f"[{symbol}] 已停用，跳過。")
        _write_decision_log(symbol, balance, None, None, 'manual', None, 'disabled', [], {})
        continue

    # 記錄歷史（供圖表使用）
    if rates:
        frr_raw = rates['frr']
        market_bid = rates['market_bid']
        market_ask = rates.get('market_ask', market_bid) or market_bid
        frr_raw_annual_pct = frr_raw * 365 * 100
        market_bid_annual_pct = market_bid * 365 * 100
        market_ask_annual_pct = market_ask * 365 * 100
        _update_frr_history(symbol, frr_raw_annual_pct)
        _update_ask_history(symbol, market_ask_annual_pct)
        _update_bid_history(symbol, market_bid_annual_pct)
    else:
        frr_raw_annual_pct = 0
        market_bid_annual_pct = 0
        market_ask_annual_pct = 0

    _rates_dict = {'frr': round(frr_raw_annual_pct, 4), 'ask': round(market_ask_annual_pct, 4), 'bid': round(market_bid_annual_pct, 4)} if rates else None

    chunk = _sym_val(settings.get('chunk_amount'), symbol, 500)
    max_amounts = settings.get('max_amounts', {})
    max_amt = max_amounts.get(symbol)

    # 取得各期數已放貸金額（offer 已取消，此時只有 active loans 佔用額度）
    try:
        _active_loans = _bfx_client.get_funding_loans()
        _lent_by_period = {}
        for _loan in _active_loans:
            if _loan['symbol'] == symbol:
                _p = _loan['period_days']
                _lent_by_period[_p] = round(_lent_by_period.get(_p, 0) + _loan['amount'], 2)
    except Exception:
        _lent_by_period = {}

    if max_amt is not None and max_amt > 0:
        _total_lent = sum(_lent_by_period.values())
        _headroom = max(round(max_amt - _total_lent, 8), 0)
        balance = min(balance, _headroom)
        print(f'  [MaxAmt] 已放貸 {_total_lent:.2f}，剩餘額度 {_headroom:.2f}，可用 balance {balance:.2f}')

    # ── 期數區間策略：掃描 Order Book，從最長期數往下匹配 ────
    targets = settings.get('period_rate_targets', [])
    # 各期數近 6h 最高成交利率（一次查所有期數，bucket 邏輯與 order book 掃描一致）
    _periods_list = [t['period'] for t in targets] if targets else [2]
    try:
        _recent_rates_by_period = _bfx_client.get_recent_trade_max_rates_by_period(
            symbol, _periods_list, hours=6
        )
    except Exception:
        _recent_rates_by_period = {}

    # 記錄各期數成交利率快照（供利率分析歷史圖表使用）
    try:
        _snap_trades = _bfx_client.get_funding_trades(symbol, limit=5000, start_ms=int((time.time() - 3600) * 1000), sort=1)
        if _snap_trades and isinstance(_snap_trades, list):
            _SNAP_BUCKETS = {'120': (31, 120), '30': (3, 30), '2': (0, 2)}
            _snap_stats = {bk: [] for bk in _SNAP_BUCKETS}
            for _t in _snap_trades:
                if not isinstance(_t, list) or len(_t) < 5:
                    continue
                _tp = int(_t[4])
                _tr = round(_t[3] * 365 * 100, 4)
                for _bk, (_lo, _hi) in _SNAP_BUCKETS.items():
                    if _lo < _tp <= _hi:
                        _snap_stats[_bk].append(_tr)
                        break
            _bucket_result = {}
            for _bk, _rates in _snap_stats.items():
                if _rates:
                    _bucket_result[_bk] = {
                        'max': round(max(_rates), 4),
                        'min': round(min(_rates), 4),
                        'avg': round(sum(_rates) / len(_rates), 4),
                        'count': len(_rates),
                    }
                else:
                    _bucket_result[_bk] = {'max': None, 'min': None, 'avg': None, 'count': 0}
            save_trade_rate_snapshot(symbol, _bucket_result)
    except Exception as _e:
        print(f'  [TradeSnapshot] 快照記錄失敗：{_e}')

    # 保底利率：auto=True 時取近 6h 2天最高成交利率（與 UI recent_6h_max 同口徑）ceil，否則沿用手動設定
    min_annual_pct, _floor_source = _resolve_min_annual(settings, symbol, _recent_rates_by_period.get(2))
    if _floor_source == 'auto':
        print(f'  [AutoFloor] 自動保底啟用：近6h最高成交 → {min_annual_pct:.4f}%')
    elif _floor_source == 'auto_fallback':
        print(f'  [AutoFloor] 自動保底啟用但近6h無成交，沿用手動保底 {min_annual_pct:.4f}%')
    if _adaptive_floor_override is not None:
        print(f'  [Stale] 使用自適應保底：{_adaptive_floor_override:.4f}%')
        min_annual_pct = _adaptive_floor_override
        _floor_source = 'adaptive'
    elif _floor_source == 'auto':
        _adp_chk = _load_adaptive_floor_state().get(symbol, {})
        _restored_floor = _adp_chk.get('restored_floor')
        _restored_at = _adp_chk.get('restored_at_ms')
        if _restored_floor and _restored_at and (int(time.time() * 1000) - _restored_at) < 6 * 3_600_000:
            if min_annual_pct < _restored_floor:
                print(f'  [AutoFloor] 近 6h 成交含衰減期資料，保底提升 {min_annual_pct:.4f}% → {_restored_floor:.4f}%')
                min_annual_pct = _restored_floor

    # fallback 利率：優先使用近 6h 2天期的最高成交利率，無資料時才用 min(ASK, FRR)
    _recent_max = _recent_rates_by_period.get(2)
    if _recent_max is not None and _recent_max > 0:
        fallback_rate_pct = _recent_max
        print(f'  [Fallback] 使用近 6h 2天最高成交利率：{_recent_max:.4f}%')
    else:
        fallback_rate_pct = min(frr_raw_annual_pct, market_ask_annual_pct) if rates else min_annual_pct
        print(f'  [Fallback] 無近期成交，使用 min(ASK, FRR)：{fallback_rate_pct:.4f}%')
    # 近6h 成交只用於 period scan 比對，fallback 掛單利率直接用保底（min_annual 或 adaptive_floor）
    fallback_rate_pct = min_annual_pct

    # 計算 base（用於 max_pct 滿額判斷，以 max_amt 總額為基準）
    _base = max_amt if (max_amt and max_amt > 0) else balance

    if targets:
        scan_result = _scan_period_targets(
            symbol, targets, fallback_rate=fallback_rate_pct,
            lent_by_period=_lent_by_period, base_amount=_base,
            recent_trade_rates=_recent_rates_by_period,
        )
    else:
        scan_result = {
            'matched': False, 'allocations': [], 'fallback_rate': min_annual_pct,
            'period': 2, 'rate': min_annual_pct, 'reason': 'no_targets', 'scan_detail': [],
        }

    if balance >= mininum:
        _remaining = balance
        all_offer_ids = []
        executed_allocs = []

        for alloc in scan_result.get('allocations', []):
            if alloc.get('max_pct') is not None:
                _cap = _base * (alloc['max_pct'] / 100)
                _already = _lent_by_period.get(alloc['period'], 0)
                _remaining_cap = _cap - _already
                if _remaining_cap <= 0:
                    print(f"  [Period] ⏭ {alloc['period']}天：分配已滿（已放 {_already:.2f} >= 配額 {_cap:.2f}），跳過")
                    continue
                _amount = min(_remaining, _remaining_cap)
            else:
                _amount = _remaining
            _amount = round(_amount, 8)
            if _amount < mininum:
                print(f"  [Period] {alloc['period']}天配額 {_amount:.2f} < 最低 {mininum}，跳過")
                if alloc.get('max_pct') is None:
                    break
                continue
            _rate_pct = min(alloc['rate'], _adaptive_floor_override) if _adaptive_floor_override is not None else alloc['rate']
            print(f"  [利率] 配置 {_amount:.2f} {symbol} → {alloc['period']}天 {_rate_pct:.4f}%，決策：{alloc['reason']}")
            _oids = auto_lend(_amount, alloc['period'], symbol, chunk, mininum, rate=_rate_pct / 100 / 365)
            all_offer_ids.extend(_oids or [])
            executed_allocs.append({'period': alloc['period'], 'rate_pct': _rate_pct, 'amount': _amount})
            _remaining = round(_remaining - _amount, 8)
            if _remaining < mininum:
                break

        # 剩餘資金 → fallback 2 天
        if _remaining >= mininum:
            _fb_rate_pct = max(scan_result.get('fallback_rate', min_annual_pct), min_annual_pct)
            print(f"  [Period] 剩餘 {_remaining:.2f} {symbol}，fallback 2天 {_fb_rate_pct:.4f}%")
            _oids = auto_lend(_remaining, 2, symbol, chunk, mininum, rate=_fb_rate_pct / 100 / 365)
            all_offer_ids.extend(_oids or [])
            executed_allocs.append({'period': 2, 'rate_pct': _fb_rate_pct, 'amount': _remaining})

        # 決策摘要（向後相容）
        _first = executed_allocs[0] if executed_allocs else None
        lend_period = _first['period'] if _first else 2
        lend_rate_pct = _first['rate_pct'] if _first else min_annual_pct

        if all_offer_ids and settings.get('notify_order_summary', True):
            tg_token = settings.get('telegram_bot_token', '').strip()
            tg_chat = settings.get('telegram_chat_id', '').strip()
            if tg_token and tg_chat:
                if len(executed_allocs) > 1:
                    _lines = [f'📌 {symbol} 掛單完成（{len(all_offer_ids)} 筆，{len(executed_allocs)} 個分配）']
                    for _ea in executed_allocs:
                        _lines.append(f'  • {_ea["period"]}天 {_ea["rate_pct"]:.4f}% × {_ea["amount"]:.2f}')
                    _send_telegram(tg_token, tg_chat, '\n'.join(_lines))
                else:
                    reason_label = scan_result.get('reason', '?')
                    if scan_result.get('matched'):
                        reason_label = f"✅ {lend_period}天目標達成"
                    elif reason_label == 'fallback':
                        if _adaptive_floor_override is not None:
                            reason_label = "⚠️ 未匹配，以自適應保底利率掛單"
                        else:
                            reason_label = "⚠️ 未匹配，以保底利率掛單"
                    _send_telegram(tg_token, tg_chat,
                        f'📌 {symbol} 掛單完成（{len(all_offer_ids)} 筆）\n'
                        f'總金額：{balance:.2f} {symbol}\n'
                        f'年化利率：{lend_rate_pct:.4f}%\n'
                        f'期間：{lend_period} 天\n'
                        f'決策：{reason_label}'
                    )

        for _ea in executed_allocs:
            insert_lending_execution(symbol, _ea['amount'], _ea['rate_pct'], _ea['period'])

        # 儲存本次掛單利率供下次 stale floor 計算下限使用
        if _stale_enabled:
            _adp_state_lr = _load_adaptive_floor_state()
            _adp_state_lr.setdefault(symbol, {})['last_offer_rate'] = round(lend_rate_pct, 4)
            _save_adaptive_floor_state(_adp_state_lr)

        _run_detail['symbols'][symbol] = {
            'frr_annual_pct': round(frr_raw_annual_pct, 4),
            'market_bid_annual_pct': round(market_bid_annual_pct, 4),
            'market_ask_annual_pct': round(market_ask_annual_pct, 4),
            'rate_annual_pct': round(lend_rate_pct, 4),
            'period_days': lend_period,
            'scan_reason': scan_result.get('reason'),
            'scan_matched': scan_result.get('matched', False),
            'scan_detail': scan_result.get('scan_detail', []),
            'allocations': executed_allocs,
        }
        _write_decision_log(symbol, balance, _rates_dict, min_annual_pct, _floor_source,
                            scan_result, 'placed', executed_allocs, _run_detail['symbols'][symbol])
    else:
        print(f"Balance too low to lend: {balance:.2f} < {mininum}")
        if _stale_enabled:
            _adp_state_bl = _load_adaptive_floor_state()
            _sym_adp_bl = _adp_state_bl.get(symbol, {})
            _adp_state_bl[symbol] = {**_sym_adp_bl, 'stale_since_ms': None, 'active': False}
            _save_adaptive_floor_state(_adp_state_bl)
        if settings.get('notify_balance_low', True) and _can_notify(f'balance_low_{symbol}'):
            tg_token = settings.get('telegram_bot_token', '').strip()
            tg_chat = settings.get('telegram_chat_id', '').strip()
            if tg_token and tg_chat:
                _send_telegram(tg_token, tg_chat,
                    f'⚠️ {symbol} 餘額不足，本次跳過\n'
                    f'可用餘額：{balance:.2f} {symbol}\n'
                    f'最低需求：{mininum} {symbol}'
                )
                _mark_notified(f'balance_low_{symbol}')
        _write_decision_log(symbol, balance, _rates_dict, min_annual_pct, _floor_source,
                            scan_result, 'balance_low', [], {})

# ── 儲存本次執行詳情 ──────────────────────────────────────────
os.makedirs(os.path.dirname(LAST_RUN_DETAIL_PATH), exist_ok=True)
with open(LAST_RUN_DETAIL_PATH, 'w') as f:
    json.dump(_run_detail, f, indent=2)

# ── 每日收益推播 ──────────────────────────────────────────────
_fs = _settings_module.load()
_tg_token = _fs.get('telegram_bot_token', '').strip()
_tg_chat = _fs.get('telegram_chat_id', '').strip()
_daily_hour = _fs.get('daily_report_hour', 21)
_now = datetime.datetime.now(tz=_TZ8)
if _tg_token and _tg_chat and _fs.get('notify_daily_report', True) and _now.hour == _daily_hour:
    _sent_today = False
    try:
        with open(DAILY_REPORT_PATH) as _f:
            _sent_today = json.load(_f).get('date') == _now.strftime('%Y-%m-%d')
    except Exception:
        pass
    if not _sent_today:
        _s = get_today_earnings_summary()
        _today_lines = '\n'.join(f'{c}: +{a:.6f}' for c, a in _s['today'].items()) or '（暫無數據）'
        _month_lines = '\n'.join(f'{c}: +{a:.6f}' for c, a in _s['month'].items()) or '（暫無數據）'
        _send_telegram(_tg_token, _tg_chat,
            f'📊 每日收益摘要 ({_s["date"]})\n\n'
            f'今日收益：\n{_today_lines}\n\n'
            f'本月累計：\n{_month_lines}'
        )
        os.makedirs(os.path.dirname(DAILY_REPORT_PATH), exist_ok=True)
        with open(DAILY_REPORT_PATH, 'w') as _f:
            json.dump({'date': _now.strftime('%Y-%m-%d')}, _f)
        print('[每日推播] 已發送收益摘要')
