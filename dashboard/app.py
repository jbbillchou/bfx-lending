import datetime
import json
import os
import threading
import time as _time

TZ_OFFSET = datetime.timezone(datetime.timedelta(hours=8))
import subprocess
import requests as http_requests
from flask import Flask, render_template, jsonify, request
import bfx_client
import cron_checker
import db
import settings
from lending_engine import scan_period_targets, get_market_rates_by_period, resolve_min_annual

db.init_db()

LENDING_SCRIPT = '/app/lending.py'
_UTIL_SNAPSHOT_INTERVAL = 60 * 60  # 每小時記錄一次資金使用率快照


def _record_utilization_snapshot():
    try:
        prices = bfx_client.get_usd_prices()
    except Exception:
        prices = {}

    # 取錢包資料；失敗時 fallback 到最後一筆 portfolio_snapshot
    try:
        wallets = bfx_client.get_wallets()
        detail = {w['symbol']: round(w['total'] * prices.get(w['symbol'], 0.0), 2) for w in wallets}
        available_detail = {w['symbol']: round((w['available'] or 0.0) * prices.get(w['symbol'], 0.0), 2) for w in wallets}
    except Exception:
        # fallback：從 portfolio_snapshots 取最後已知總額
        snap = db.get_last_portfolio_snapshot()
        if snap:
            detail = snap.get('detail', {})
            available_detail = snap.get('available_detail', {}) or {}
        else:
            detail = {}
            available_detail = {}

    try:
        loans = bfx_client.get_funding_loans()
    except Exception:
        loans = []

    try:
        offers = bfx_client.get_funding_offers()
    except Exception:
        offers = []

    loans_detail = {}
    for loan in loans:
        sym = loan['symbol']
        loans_detail[sym] = loans_detail.get(sym, 0.0) + round(loan['amount'] * prices.get(sym, 0.0), 2)
    offers_detail = {}
    for offer in offers:
        sym = offer['symbol']
        offers_detail[sym] = offers_detail.get(sym, 0.0) + round(offer['amount'] * prices.get(sym, 0.0), 2)

    # 計算加權平均利率（以 USD 金額加權）
    total_lent_usd = 0.0
    weighted_sum = 0.0
    for loan in loans:
        sym = loan['symbol']
        usd_val = loan['amount'] * prices.get(sym, 0.0)
        rate = loan.get('rate_annual_pct', 0.0)
        total_lent_usd += usd_val
        weighted_sum += usd_val * rate
    weighted_avg_rate = round(weighted_sum / total_lent_usd, 4) if total_lent_usd > 0 else None

    # 只有 detail 完全為空時才跳過寫入（無任何已知資料）
    if not detail:
        return
    try:
        db.insert_utilization_snapshot(detail, available_detail, loans_detail, offers_detail, weighted_avg_rate)
    except Exception:
        pass
    finally:
        t = threading.Timer(_UTIL_SNAPSHOT_INTERVAL, _record_utilization_snapshot)
        t.daemon = True
        t.start()


_util_timer = threading.Timer(_UTIL_SNAPSHOT_INTERVAL, _record_utilization_snapshot)
_util_timer.daemon = True
_util_timer.start()
LENDING_LOG = '/app/data/lending_last.log'
_run_lock = threading.Lock()

# ── 收益自動同步 ──────────────────────────────────────────────────────────────
_SYNC_STATE_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'sync_state.json')
_AUTO_SYNC_INTERVAL = 60 * 60  # 每 60 分鐘同步一次
_sync_state_lock = threading.Lock()


def _load_sync_state():
    try:
        with open(_SYNC_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_sync_state(state):
    os.makedirs(os.path.dirname(_SYNC_STATE_PATH), exist_ok=True)
    with open(_SYNC_STATE_PATH, 'w') as f:
        json.dump(state, f)


def _do_sync_earnings(force_start_ms=None):
    """同步 Bitfinex 帳本收益到 SQLite，並記錄同步狀態。
    force_start_ms: 若指定，強制從此毫秒時間戳開始抓（用於補歷史資料）。
    """
    with _sync_state_lock:
        state = _load_sync_state()
        state['is_syncing'] = True
        state['last_error'] = None
        _save_sync_state(state)
    try:
        start_ms = force_start_ms if force_start_ms is not None else db.get_latest_ts()
        total_synced = 0
        for symbol in ['USD', 'UST', 'ETH', 'BTC', 'LTC']:
            try:
                entries = bfx_client.get_ledger_earnings(symbol, start_ms=start_ms)
                if entries:
                    total_synced += db.upsert_earnings(entries)
            except Exception:
                pass
        # 同步 Bitfinex credits history → 補充 lending_executions（平均利率）
        for symbol in ['UST', 'ETH', 'BTC', 'LTC']:
            try:
                latest_exec_ts = db.get_latest_lending_execution_ts(symbol)
                credits = bfx_client.get_funding_credits_history(symbol, start_ms=latest_exec_ts)
                if credits:
                    db.upsert_lending_executions_from_credits(credits)
            except Exception:
                pass
        # 同時存 portfolio snapshot
        try:
            _record_portfolio_snapshot()
        except Exception:
            pass
        now_ts = int(_time.time())
        tz8 = datetime.timezone(datetime.timedelta(hours=8))
        with _sync_state_lock:
            state = _load_sync_state()
            state['is_syncing'] = False
            state['last_sync_ts'] = now_ts
            state['last_sync_iso'] = datetime.datetime.fromtimestamp(now_ts, tz=tz8).isoformat()
            state['last_error'] = None
            _save_sync_state(state)
        return total_synced
    except Exception as e:
        with _sync_state_lock:
            state = _load_sync_state()
            state['is_syncing'] = False
            state['last_error'] = str(e)
            _save_sync_state(state)
        return 0


def _record_portfolio_snapshot():
    wallets = bfx_client.get_wallets()
    prices = bfx_client.get_usd_prices()
    detail = {w['symbol']: round(w['total'] * prices.get(w['symbol'], 1.0), 2) for w in wallets}
    available_detail = {w['symbol']: round((w['available'] or 0.0) * prices.get(w['symbol'], 1.0), 2) for w in wallets}
    total_usd = sum(detail.values())
    today = datetime.date.today().isoformat()
    db.save_snapshot(today, total_usd, detail, available_detail)


def _auto_sync_loop():
    _time.sleep(10)  # 啟動後延遲 10 秒，避免與 Flask 初始化衝突
    while True:
        try:
            _do_sync_earnings()
        except Exception:
            pass
        _time.sleep(_AUTO_SYNC_INTERVAL)


_auto_sync_thread = threading.Thread(target=_auto_sync_loop, daemon=True)
_auto_sync_thread.start()


def _build_cron_entry(interval_minutes):
    if interval_minutes <= 0:
        return None
    if interval_minutes < 60:
        cron_time = f'*/{interval_minutes} *'
    elif interval_minutes == 60:
        cron_time = '0 *'
    elif interval_minutes == 1440:
        cron_time = '0 0'
    else:
        cron_time = f'0 */{interval_minutes // 60}'
    return f'{cron_time} * * * /usr/local/bin/python3 {LENDING_SCRIPT} > {LENDING_LOG} 2>&1'

app = Flask(__name__)

_api_data_cache = {'result': None, 'ts': 0.0}
_API_DATA_TTL = 25  # seconds


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/data')
def api_data():
    _now = _time.monotonic()
    if _api_data_cache['result'] and _now - _api_data_cache['ts'] < _API_DATA_TTL:
        return jsonify(_api_data_cache['result'])
    errors = []

    try:
        wallets = bfx_client.get_wallets()
    except Exception as e:
        wallets = []
        errors.append(f'wallets: {e}')

    try:
        offers = bfx_client.get_funding_offers()
    except Exception as e:
        offers = []
        errors.append(f'offers: {e}')

    try:
        loans = bfx_client.get_funding_loans()
    except Exception as e:
        loans = []
        errors.append(f'loans: {e}')

    try:
        frr = bfx_client.get_frr()
    except Exception as e:
        frr = {}
        errors.append(f'frr: {e}')

    try:
        prices = bfx_client.get_usd_prices()
    except Exception as e:
        prices = {}
        errors.append(f'prices: {e}')

    bfx_down = any(k in e for e in errors for k in ('wallets', 'offers', 'loans', 'frr'))
    breakdown = {w['symbol']: round(w['total'] * prices.get(w['symbol'], 0.0), 2) for w in wallets}
    available_breakdown = {w['symbol']: round((w['available'] or 0.0) * prices.get(w['symbol'], 0.0), 2) for w in wallets}
    total_usd = round(sum(breakdown.values()), 2)
    effective_yield_usd = sum(
        loan['amount'] * prices.get(loan['symbol'], 0.0) * loan['rate_annual_pct'] / 100
        for loan in loans
    )
    effective_yield_pct = round(effective_yield_usd / total_usd * 100, 4) if total_usd > 0 else 0.0
    today = datetime.date.today().isoformat()
    if not bfx_down:
        try:
            db.save_snapshot(today, total_usd, breakdown, available_breakdown)
        except Exception:
            pass

    cron = cron_checker.get_cron_status()

    s = settings.load()


    run_detail = {}
    try:
        with open(LAST_RUN_DETAIL_PATH) as f:
            run_detail = json.load(f)
    except Exception:
        pass

    _version = 'unknown'
    try:
        with open(os.path.join(os.path.dirname(__file__), '..', 'VERSION')) as f:
            _version = f.read().strip()
    except Exception:
        pass

    try:
        monthly_vol = db.get_monthly_earnings_volume()
    except Exception:
        monthly_vol = None

    _adp_state_data = {}
    try:
        _adp_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'adaptive_floor_state.json')
        with open(_adp_path) as f:
            _adp_all = json.load(f)
        symbol = (s.get('symbols') or ['UST'])[0]
        _adp_state_data = _adp_all.get(symbol, {})
    except Exception:
        pass

    result = {
        'cron': cron,
        'wallets': wallets,
        'offers': offers,
        'loans': loans,
        'frr': frr,
        'portfolio': {'total_usd': total_usd, 'breakdown': breakdown},
        'effective_yield_pct': effective_yield_pct,
        'updated_at': datetime.datetime.now(tz=TZ_OFFSET).strftime('%Y-%m-%d %H:%M:%S'),
        'errors': errors,
        'bfx_down': bfx_down,
        'min_annual_rates': s.get('min_annual_rates', {}),
        'min_annual_auto': s.get('min_annual_auto', {}),
        'run_detail': run_detail,
        'monthly_lending_volume': monthly_vol,
        'version': _version,
        'adaptive_floor_state': {
            'active': _adp_state_data.get('active', False),
            'adaptive_floor': _adp_state_data.get('adaptive_floor'),
            'original_floor': _adp_state_data.get('original_floor'),
            'last_offer_rate': _adp_state_data.get('last_offer_rate'),
            'applied_at_ms': _adp_state_data.get('applied_at_ms'),
            'stale_since_ms': _adp_state_data.get('stale_since_ms'),
            'last_loan_ids_count': len(_adp_state_data.get('last_loan_ids', [])),
            'stale_floor_min_ratio_pct': s.get('stale_floor_min_ratio_pct', 50),
            'stale_floor_enabled': s.get('stale_floor_enabled', True),
        },
    }
    if not bfx_down:
        _api_data_cache['result'] = result
        _api_data_cache['ts'] = _time.monotonic()
    return jsonify(result)


@app.route('/api/earnings/sync', methods=['POST'])
def api_earnings_sync():
    # 支援 ?since=YYYY-MM-DD 強制從指定日期補歷史資料
    since = request.args.get('since')
    force_start_ms = None
    if since:
        try:
            dt = datetime.datetime.strptime(since, '%Y-%m-%d')
            force_start_ms = int(dt.timestamp() * 1000)
        except ValueError:
            return jsonify({'error': 'since 格式錯誤，請用 YYYY-MM-DD'}), 400
    total_synced = _do_sync_earnings(force_start_ms=force_start_ms)
    state = _load_sync_state()
    return jsonify({'synced': total_synced, 'last_sync_iso': state.get('last_sync_iso'),
                    'last_error': state.get('last_error')})


@app.route('/api/earnings/sync-status', methods=['GET'])
def api_earnings_sync_status():
    state = _load_sync_state()
    return jsonify({
        'last_sync_ts': state.get('last_sync_ts'),
        'last_sync_iso': state.get('last_sync_iso'),
        'is_syncing': state.get('is_syncing', False),
        'last_error': state.get('last_error'),
    })


@app.route('/api/cron/enable', methods=['POST'])
def cron_enable():
    logs = []
    try:
        s = settings.load()
        interval = s.get('cron_interval_minutes', 60)
        cron_entry = _build_cron_entry(interval)

        # 讀取現有 crontab
        existing = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        current = existing.stdout if existing.returncode == 0 else ''
        logs.append('$ crontab -l')
        logs.append(current.strip() if current.strip() else '(no crontab)')

        # 移除舊的 lending.py 行
        filtered = '\n'.join(line for line in current.splitlines() if 'lending.py' not in line)

        if cron_entry is None:
            # interval=0：不執行模式，只移除舊排程
            result = subprocess.run(['crontab', '-'], input=filtered + '\n', capture_output=True, text=True)
            logs.append('=> 網格策略已設為不自動執行（手動觸發）✅')
            return jsonify({'success': True, 'logs': logs})

        new_crontab = filtered.rstrip('\n') + '\n' + cron_entry + '\n'
        result = subprocess.run(['crontab', '-'], input=new_crontab, capture_output=True, text=True)
        logs.append(f'$ 設定排程（每 {interval} 分鐘）：{cron_entry}')
        if result.returncode == 0:
            logs.append('=> 排程設定成功 ✅')
        else:
            logs.append(f'=> 錯誤：{result.stderr}')
            return jsonify({'success': False, 'logs': logs})
    except Exception as e:
        logs.append(f'=> 例外：{e}')
        return jsonify({'success': False, 'logs': logs})

    s['schedule_enabled'] = True
    settings.save(s)
    _api_data_cache['ts'] = 0.0  # invalidate cache
    return jsonify({'success': True, 'logs': logs})


@app.route('/api/cron/disable', methods=['POST'])
def cron_disable():
    logs = []
    try:
        existing = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        logs.append('$ crontab -l')
        if existing.returncode != 0:
            logs.append('(no crontab)')
            return jsonify({'success': True, 'logs': logs})

        filtered = '\n'.join(
            line for line in existing.stdout.splitlines()
            if 'lending.py' not in line
        )
        result = subprocess.run(['crontab', '-'], input=filtered + '\n', capture_output=True, text=True)
        logs.append(f'$ crontab -l | grep -v lending.py | crontab -')
        if result.returncode == 0:
            logs.append('=> 排程已移除 ✅')
        else:
            logs.append(f'=> 錯誤：{result.stderr}')
            return jsonify({'success': False, 'logs': logs})
    except Exception as e:
        logs.append(f'=> 例外：{e}')
        return jsonify({'success': False, 'logs': logs})

    s = settings.load()
    s['schedule_enabled'] = False
    settings.save(s)
    _api_data_cache['ts'] = 0.0  # invalidate cache
    return jsonify({'success': True, 'logs': logs})


@app.route('/api/settings', methods=['GET'])
def api_settings_get():
    return jsonify(settings.load())


@app.route('/api/settings', methods=['POST'])
def api_settings_post():
    new_s = request.json
    # 若 min_annual_rates 調高超過目前 adaptive_floor，自動重置該幣種的 adaptive floor state
    _adp_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'adaptive_floor_state.json')
    try:
        with open(_adp_path) as _f:
            _adp_state = json.load(_f)
        _new_min = new_s.get('min_annual_rates', {})
        _changed = False
        for _sym, _new_val in _new_min.items():
            _sym_state = _adp_state.get(_sym, {})
            _cur_af = _sym_state.get('adaptive_floor')
            if _sym_state.get('active') and _cur_af is not None and float(_new_val) > float(_cur_af):
                # 用戶把 min_annual 調高超過衰減後的保底，視為手動重置
                _adp_state[_sym] = {k: v for k, v in _sym_state.items()
                                    if k in ('last_loan_ids', 'last_offer_rate')}
                _adp_state[_sym]['active'] = False
                _changed = True
        if _changed:
            with open(_adp_path, 'w') as _f:
                json.dump(_adp_state, _f, indent=2)
    except Exception:
        pass
    settings.save(new_s)
    _api_data_cache['ts'] = 0.0  # invalidate cache so next /api/data reflects new settings
    return jsonify({'ok': True})


@app.route('/api/offers/<int:offer_id>', methods=['DELETE'])
def api_cancel_offer(offer_id):
    try:
        ok = bfx_client.cancel_offer(offer_id)
        return jsonify({'ok': ok})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/offers/<int:offer_id>', methods=['PUT'])
def api_update_offer(offer_id):
    """取消原掛單後以新參數重新掛單。"""
    body = request.get_json(force=True) or {}
    symbol = body.get('symbol', '').upper()
    try:
        amount = float(body['amount'])
        rate_annual_pct = float(body['rate_annual_pct'])
        period_days = int(body['period_days'])
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({'ok': False, 'error': f'invalid params: {e}'}), 400
    if not symbol or amount <= 0 or rate_annual_pct <= 0 or period_days < 2:
        return jsonify({'ok': False, 'error': 'invalid params'}), 400
    try:
        bfx_client.cancel_offer(offer_id)
        new_id = bfx_client.submit_offer(symbol, amount, rate_annual_pct, period_days)
        return jsonify({'ok': True, 'new_id': new_id})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/run-now', methods=['POST'])
def api_run_now():
    try:
        result = subprocess.run(
            ['/usr/local/bin/python3', LENDING_SCRIPT],
            capture_output=True, text=True, timeout=120
        )
        output = (result.stdout + result.stderr).strip()
        os.makedirs(os.path.dirname(LENDING_LOG), exist_ok=True)
        with open(LENDING_LOG, 'w') as f:
            f.write(output)
        return jsonify({'ok': result.returncode == 0, 'output': output})
    except Exception as e:
        return jsonify({'ok': False, 'output': str(e)}), 500


@app.route('/api/run-on-expiry', methods=['POST'])
def api_run_on_expiry():
    if not _run_lock.acquire(blocking=False):
        return jsonify({'ok': False, 'output': '已有執行中的任務，略過'}), 409
    try:
        s = settings.load()
        tg_token = s.get('telegram_bot_token', '').strip()
        tg_chat = s.get('telegram_chat_id', '').strip()
        if tg_token and tg_chat:
            try:
                http_requests.post(
                    f'https://api.telegram.org/bot{tg_token}/sendMessage',
                    json={'chat_id': tg_chat, 'text': '⏰ 合約到期，自動重新放貸中...'},
                    timeout=8
                )
            except Exception:
                pass

        result = subprocess.run(
            ['/usr/local/bin/python3', LENDING_SCRIPT],
            capture_output=True, text=True, timeout=120
        )
        output = (result.stdout + result.stderr).strip()

        os.makedirs(os.path.dirname(LENDING_LOG), exist_ok=True)
        with open(LENDING_LOG, 'w') as f:
            f.write(output)
        if tg_token and tg_chat:
            try:
                if result.returncode == 0:
                    tg_msg = '✅ 重新放貸完成\n\n' + '\n'.join(
                        line for line in output.splitlines()
                        if any(k in line for k in ['Lend order', '最後一張單', 'Balance too low', 'skipping', '已停用', 'get_balance'])
                    )
                else:
                    tg_msg = f'❌ 放貸執行失敗\n\n{output[-300:]}'
                http_requests.post(
                    f'https://api.telegram.org/bot{tg_token}/sendMessage',
                    json={'chat_id': tg_chat, 'text': tg_msg},
                    timeout=8
                )
            except Exception:
                pass
        return jsonify({'ok': result.returncode == 0, 'output': output})
    except Exception as e:
        return jsonify({'ok': False, 'output': str(e)}), 500
    finally:
        _run_lock.release()


@app.route('/api/lending-log')
def api_lending_log():
    try:
        with open(LENDING_LOG) as f:
            return jsonify({'log': f.read()})
    except FileNotFoundError:
        return jsonify({'log': ''})


@app.route('/api/decision-logs')
def api_decision_logs():
    date_str = request.args.get('date')
    limit = min(int(request.args.get('limit', 200)), 500)
    rows = db.get_decision_logs(date_str=date_str, limit=limit)
    for r in rows:
        r['stdout_log_length'] = len(r.get('stdout_log') or '')
        r.pop('stdout_log', None)
    return jsonify(rows)


@app.route('/api/decision-logs/<int:log_id>/log')
def api_decision_log_detail(log_id):
    text = db.get_decision_log_stdout(log_id)
    if text is None:
        return jsonify({'log': ''}), 404
    return jsonify({'log': text})


FRR_HISTORY_PATH = '/app/data/frr_history.json'
ASK_HISTORY_PATH = '/app/data/ask_history.json'
BID_HISTORY_PATH = '/app/data/bid_history.json'
PRICE_HISTORY_PATH = '/app/data/price_history.json'
LAST_RUN_DETAIL_PATH = '/app/data/last_run_detail.json'





@app.route('/api/rate-calc')
def api_rate_calc():
    """期數區間試算：掃描 Order Book，回傳匹配結果。"""
    symbol = request.args.get('symbol', 'UST')
    s = settings.load()

    try:
        rates = bfx_client.get_frr()
        frr_data = rates.get(symbol, {})
        frr_spot = frr_data.get('annual_pct', 0)
        bid_spot = frr_data.get('market_bid_annual_pct', 0)
        ask_spot = frr_data.get('market_ask_annual_pct', bid_spot)
    except Exception:
        frr_spot, bid_spot, ask_spot = 0, 0, 0

    # 讀取 adaptive_floor_state.json（供試算保底衰減預覽）
    state_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'adaptive_floor_state.json')
    stale_state = {}
    try:
        with open(state_path) as f:
            stale_state = json.load(f).get(symbol, {})
    except Exception:
        pass

    targets = s.get('period_rate_targets', [])
    # 各期數近 6h 最高成交利率（一次查所有期數）
    _periods_list = [t['period'] for t in targets] if targets else [2]
    try:
        _recent_rates = bfx_client.get_recent_trade_max_rates_by_period(symbol, _periods_list, hours=6)
    except Exception:
        _recent_rates = {}

    # 保底利率：auto=True 時取近 6h 最高成交利率（period 2 bucket，與 UI recent_6h_max 一致）ceil，否則取手動設定
    min_annual, min_annual_source = resolve_min_annual(s, symbol, _recent_rates.get(2))

    # 若 adaptive floor 啟用中，以衰減後的值覆蓋 min_annual（與 lending.py 一致）
    adaptive_floor_active = stale_state.get('active') and stale_state.get('adaptive_floor') is not None
    if adaptive_floor_active:
        min_annual = stale_state['adaptive_floor']

    # 近6h 成交只用於 period scan 比對，fallback 掛單利率直接用保底（與 lending.py 一致）
    recent_max = _recent_rates.get(2)
    fallback_rate = min_annual

    # 取得 wallet 與已放貸資料（scan 前需要，用於滿額判斷）
    try:
        wallets = bfx_client.get_wallets()
        wallet = next((w for w in wallets if w['symbol'] == symbol), None)
        wallet_total = round(wallet['total'] if wallet else 0.0, 2)
    except Exception:
        wallet_total = 0.0

    try:
        loans = bfx_client.get_funding_loans()
        sym_loans = [l for l in loans if l['symbol'] == symbol]
        lent_amount = round(sum(l['amount'] for l in sym_loans), 2)
        lent_by_period = {}
        for l in sym_loans:
            p = l['period_days']
            lent_by_period[p] = round(lent_by_period.get(p, 0) + l['amount'], 2)
    except Exception:
        lent_amount = 0.0
        lent_by_period = {}

    try:
        all_offers = bfx_client.get_funding_offers()
        pending_amount = round(sum(o['amount'] for o in all_offers if o['symbol'] == symbol), 2)
    except Exception:
        pending_amount = 0.0

    max_amt = s.get('max_amounts', {}).get(symbol)
    if max_amt is not None and max_amt > 0:
        # base_amount 用 max_amt 計算 max_pct 配額；expected_total 為剩餘可部署空間
        base_amount = max_amt
        expected_total = round(max(max_amt - lent_amount - pending_amount, 0), 2)
    else:
        base_amount = wallet_total
        expected_total = wallet_total

    if targets:
        result = scan_period_targets(symbol, targets, fallback_rate=fallback_rate,
                                     lent_by_period=lent_by_period, base_amount=base_amount,
                                     recent_trade_rates=_recent_rates)
        if result['matched']:
            final_rate = min(result['rate'], min_annual) if adaptive_floor_active else result['rate']
        else:
            final_rate = max(result['rate'], min_annual)
    else:
        result = {'matched': False, 'period': int(s.get('default_period_days', 2)),
                  'rate': min_annual, 'reason': 'no_targets', 'scan_detail': []}
        final_rate = min_annual

    # 將 per-period 放貸金額與預期配額注入 scan_detail（UX 顯示用）
    scan_detail = result.get('scan_detail', [])
    for d in scan_detail:
        p = d['period']
        d['lent_amount'] = lent_by_period.get(p, 0.0)
        mp = d.get('max_pct')
        d['expected_amount'] = round(base_amount * mp / 100, 2) if mp is not None else None

    return jsonify({
        'symbol': symbol,
        'frr_spot': round(frr_spot, 4),
        'bid_spot': round(bid_spot, 4),
        'ask_spot': round(ask_spot, 4),
        'min_annual': min_annual,
        'min_annual_source': min_annual_source,
        'final_rate': round(final_rate, 4),
        'lend_period': result['period'],
        'matched': result['matched'],
        'reason': result['reason'],
        'scan_detail': scan_detail,
        'wallet_total': wallet_total,
        'lent_amount': lent_amount,
        'pending_amount': pending_amount,
        'expected_total': expected_total,
        'recent_6h_max': round(recent_max, 4) if recent_max else None,
        'stale_floor_enabled': s.get('stale_floor_enabled', False),
        'stale_offer_hours': s.get('stale_offer_hours', 6),
        'stale_floor_decay_pct': s.get('stale_floor_decay_pct', 0.5),
        'stale_floor_min_ratio_pct': s.get('stale_floor_min_ratio_pct', 50),
        'stale_since_ms': stale_state.get('stale_since_ms'),
        'adaptive_floor': stale_state.get('adaptive_floor'),
        'original_floor': stale_state.get('original_floor'),
        'adaptive_floor_active': stale_state.get('active', False),
        'last_offer_rate': stale_state.get('last_offer_rate'),
        'applied_at_ms': stale_state.get('applied_at_ms'),
        'last_loan_ids_count': len(stale_state.get('last_loan_ids', [])),
        'cron_interval_minutes': s.get('cron_interval_minutes', 60),
    })


_period_rates_cache = {}  # {lookback_hours: {'result': ..., 'ts': monotonic}}
_PERIOD_RATES_TTL = 30  # seconds


@app.route('/api/market/period-rates')
def api_market_period_rates():
    """取得各期數的 BID 側市場利率及近 N 小時成交統計（供設定頁即時參考）。"""
    symbol = request.args.get('symbol', 'UST')
    lookback_hours = int(request.args.get('lookback_hours', 24))
    import time as _time
    _now = _time.monotonic()
    cached = _period_rates_cache.get(lookback_hours)
    if cached and _now - cached['ts'] < _PERIOD_RATES_TTL:
        return jsonify(cached['result'])
    s = settings.load()
    targets = s.get('period_rate_targets', [])
    periods = [t['period'] for t in targets] if targets else [2, 7, 30, 120]

    try:
        rates = bfx_client.get_frr()
        frr_data = rates.get(symbol, {})
        frr_spot = frr_data.get('annual_pct', 0)
        ask_spot = frr_data.get('market_ask_annual_pct', 0)
    except Exception:
        frr_spot, ask_spot = 0, 0

    market = get_market_rates_by_period(symbol, periods)

    # 近 N 小時成交統計（各期數，range-based：每一期只計入自己區間的成交）
    trade_stats = {p: {'max_rate': None, 'min_rate': None, 'count': 0} for p in periods}
    trade_records = {p: [] for p in periods}
    sorted_periods = sorted(periods, reverse=True)
    period_ranges = {
        p: (sorted_periods[i + 1] if i + 1 < len(sorted_periods) else 0)
        for i, p in enumerate(sorted_periods)
    }
    try:
        import time as _time
        start_ms = int((_time.time() - lookback_hours * 3600) * 1000)
        raw_trades = bfx_client.get_funding_trades(symbol, limit=5000, start_ms=start_ms)
        if isinstance(raw_trades, list):
            for t in raw_trades:
                if not isinstance(t, list) or len(t) < 5:
                    continue
                rate_annual = round(t[3] * 365 * 100, 4)
                t_period = int(t[4])
                for p, lower in period_ranges.items():
                    if lower < t_period <= p:
                        st = trade_stats[p]
                        st['count'] += 1
                        st['max_rate'] = max(st['max_rate'], rate_annual) if st['max_rate'] is not None else rate_annual
                        st['min_rate'] = min(st['min_rate'], rate_annual) if st['min_rate'] is not None else rate_annual
                        trade_records[p].append({
                            'ts': t[1],
                            'rate_annual': rate_annual,
                            'amount': round(abs(t[2]), 2),
                            'period': t_period,
                        })
    except Exception:
        pass

    result = {
        'symbol': symbol,
        'frr_spot': round(frr_spot, 4),
        'ask_spot': round(ask_spot, 4),
        'lookback_hours': lookback_hours,
        'periods': {
            str(p): {
                **market[p],
                'trade_stats': trade_stats[p],
                'trade_records': sorted(trade_records[p], key=lambda x: -x['ts']),
            }
            for p in periods
        },
    }
    _period_rates_cache[lookback_hours] = {'result': result, 'ts': _time.monotonic()}
    return jsonify(result)


@app.route('/api/frr-history')
def api_frr_history():
    def _load(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}
    frr = _load(FRR_HISTORY_PATH)
    ask = _load(ASK_HISTORY_PATH)
    bid = _load(BID_HISTORY_PATH)
    symbols = set(frr) | set(ask) | set(bid)
    result = {
        sym: {
            'frr': frr.get(sym, []),
            'ask': ask.get(sym, []),
            'bid': bid.get(sym, []),
        }
        for sym in symbols
    }
    return jsonify(result)


@app.route('/api/market/candles')
def api_market_candles():
    symbol = request.args.get('symbol', 'UST')
    timeframe = request.args.get('timeframe', '1h')
    limit = min(int(request.args.get('limit', 168)), 500)
    try:
        raw = bfx_client.get_funding_candles(symbol, timeframe, limit)
        candles = [{
            'ts':     c[0],
            'open':   round(c[1] * 365 * 100, 4),
            'close':  round(c[2] * 365 * 100, 4),
            'high':   round(c[3] * 365 * 100, 4),
            'low':    round(c[4] * 365 * 100, 4),
            'volume': c[5],
        } for c in raw if isinstance(c, list) and len(c) >= 6]
        return jsonify(candles)
    except Exception:
        return jsonify([])


@app.route('/api/market/orderbook')
def api_market_orderbook():
    symbol = request.args.get('symbol', 'UST')
    try:
        raw = bfx_client.get_funding_orderbook(symbol)
        asks, bids = [], []
        for entry in raw:
            if not isinstance(entry, list) or len(entry) < 4:
                continue
            rate_annual = round(entry[0] * 365 * 100, 4)
            amount = entry[3]
            if amount > 0:
                asks.append({'rate': rate_annual, 'amount': round(amount, 2)})
            else:
                bids.append({'rate': rate_annual, 'amount': round(abs(amount), 2)})
        return jsonify({
            'asks': sorted(asks, key=lambda x: x['rate']),
            'bids': sorted(bids, key=lambda x: x['rate'], reverse=True),
        })
    except Exception:
        return jsonify({'asks': [], 'bids': []})


@app.route('/api/market/trades')
def api_market_trades():
    symbol = request.args.get('symbol', 'UST')
    limit = min(int(request.args.get('limit', 100)), 500)
    try:
        raw = bfx_client.get_funding_trades(symbol, limit)
        trades = [{
            'id':          t[0],
            'ts':          t[1],
            'amount':      round(abs(t[2]), 2),
            'rate_annual': round(t[3] * 365 * 100, 4),
            'period':      t[4],
        } for t in raw if isinstance(t, list) and len(t) >= 5]
        return jsonify(trades)
    except Exception:
        return jsonify([])


@app.route('/api/telegram/test', methods=['POST'])
def api_telegram_test():
    s = settings.load()
    token = s.get('telegram_bot_token', '').strip()
    chat_id = s.get('telegram_chat_id', '').strip()
    if not token or not chat_id:
        return jsonify({'ok': False, 'error': 'Bot Token 或 Chat ID 未設定'})
    try:
        resp = http_requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': '✅ BFX Lending Bot 測試推播成功！'},
            timeout=10
        )
        data = resp.json()
        if data.get('ok'):
            return jsonify({'ok': True})
        return jsonify({'ok': False, 'error': data.get('description', '未知錯誤')})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/earnings/monthly')
def api_earnings_monthly():
    month = request.args.get('month', datetime.date.today().strftime('%Y-%m'))
    return jsonify(db.get_monthly_earnings(month))


@app.route('/api/earnings/ytd')
def api_earnings_ytd():
    return jsonify(db.get_ytd_stats())


@app.route('/api/earnings/range')
def api_earnings_range():
    months = min(int(request.args.get('months', 1)), 24)
    return jsonify(db.get_earnings_range_total(months))


@app.route('/api/loans/weighted')
def api_loans_weighted():
    PERIOD_BUCKETS = [(120, 31, 120), (30, 3, 30), (2, 0, 2)]
    all_loans = []
    try:
        all_loans = bfx_client.get_funding_loans()
    except Exception:
        pass
    total_lent = sum(l['amount'] for l in all_loans)
    # 取得錢包總餘額（含未放貸部分），用於計算資本效率年化
    total_balance = None
    try:
        wallets = bfx_client.get_wallets()
        total_balance = sum(w['total'] for w in wallets)
    except Exception:
        pass
    # 從 settings 計算各期數上限金額（cap）
    s = settings.load()
    max_amt = s.get('max_amounts', {}).get('UST')
    total_lendable = round(max_amt if (max_amt and max_amt > 0) else (total_balance or 0), 2)
    period_targets = s.get('period_rate_targets', [])

    def _bucket_max_pct(lower, upper):
        for t in period_targets:
            if lower < t['period'] <= upper:
                return t.get('max_pct')
        return None

    bucket_max_pcts = [_bucket_max_pct(lo, hi) for _, lo, hi in PERIOD_BUCKETS]
    explicit_cap_sum = sum(
        mp / 100 * total_lendable for mp in bucket_max_pcts if mp is not None
    )
    null_cap = max(0.0, total_lendable - explicit_cap_sum)

    by_period = []
    for (label, lower, upper), max_pct in zip(PERIOD_BUCKETS, bucket_max_pcts):
        bucket = [l for l in all_loans if lower < l['period_days'] <= upper]
        amt = sum(l['amount'] for l in bucket)
        avg_rate = (sum(l['amount'] * l['rate_annual_pct'] for l in bucket) / amt) if amt > 0 else 0
        cap = round((max_pct / 100 * total_lendable) if max_pct is not None else null_cap, 2)
        by_period.append({
            'period': label,
            'amount': round(amt, 2),
            'rate_avg': round(avg_rate, 4),
            'pct_of_total': round(amt / total_lent * 100, 2) if total_lent > 0 else 0,
            'cap': cap,
            'cap_pct': round(min(amt / cap * 100, 100), 1) if cap > 0 else 0,
        })
    weighted_rate_sum = sum(l['amount'] * l['rate_annual_pct'] for l in all_loans)
    weighted_rate = (weighted_rate_sum / total_lent) if total_lent > 0 else 0
    # 有效年化 = Σ(放貸金額 × 利率) / 總餘額（含未放貸），閒置資金視為0%
    effective_rate = (weighted_rate_sum / total_balance) if total_balance and total_balance > 0 else None
    return jsonify({
        'total_lent': round(total_lent, 2),
        'total_balance': round(total_balance, 2) if total_balance is not None else None,
        'weighted_rate': round(weighted_rate, 4),
        'effective_rate': round(effective_rate, 4) if effective_rate is not None else None,
        'by_period': by_period,
    })


@app.route('/api/utilization/history')
def api_utilization_history():
    hours = min(int(request.args.get('hours', 24)), 8760)
    rows = db.get_utilization_history(hours)
    return jsonify(rows)


@app.route('/api/market/rate-history')
def api_market_rate_history():
    from collections import defaultdict
    symbol = request.args.get('symbol', 'UST')
    BUCKETS = {'120': (31, 120), '30': (3, 30), '2': (0, 2)}
    hours_param = request.args.get('hours')

    if hours_param is not None:
        # 小時粒度模式 — 優先從 DB 快照讀取
        hours = min(int(hours_param), 48)
        series = db.get_trade_rate_history(symbol, hours=hours)
        db_points = max((len(v) for v in series.values()), default=0) if series else 0
        if not series or db_points < max(hours // 2, 2):
            # DB 尚無資料，fallback 到即時 API
            start_ms = int((_time.time() - hours * 3600) * 1000)
            try:
                raw = bfx_client.get_funding_trades(symbol, limit=5000, start_ms=start_ms, sort=1)
            except Exception:
                return jsonify({'periods': [120, 30, 2], 'series': {}, 'mode': 'hourly'})
            bucket_data = {k: defaultdict(list) for k in BUCKETS}
            for t in raw:
                if not isinstance(t, list) or len(t) < 5:
                    continue
                t_period = int(t[4])
                t_rate = round(t[3] * 365 * 100, 4)
                slot = datetime.datetime.fromtimestamp(t[1] / 1000, tz=TZ_OFFSET).strftime('%m-%d %H:00')
                for bk, (lo, hi) in BUCKETS.items():
                    if lo < t_period <= hi:
                        bucket_data[bk][slot].append(t_rate)
                        break
            series = {}
            for bk, slot_rates in bucket_data.items():
                series[bk] = sorted([
                    {'date': s, 'max_rate': max(rates), 'avg_rate': round(sum(rates) / len(rates), 4), 'count': len(rates)}
                    for s, rates in slot_rates.items()
                ], key=lambda x: x['date'])
        return jsonify({'periods': [120, 30, 2], 'series': series, 'mode': 'hourly'})

    # 日線粒度模式 — 從 DB 快照讀取
    days = min(int(request.args.get('days', 30)), 90)
    series = db.get_trade_rate_history(symbol, days=days)
    db_days = max((len(v) for v in series.values()), default=0) if series else 0
    if not series or db_days < max(days // 2, 2):
        # DB 尚無資料，fallback 到即時 API
        start_ms = int((_time.time() - days * 86400) * 1000)
        try:
            raw = bfx_client.get_funding_trades(symbol, limit=10000, start_ms=start_ms, sort=1)
        except Exception:
            return jsonify({'periods': [120, 30, 2], 'series': {}})
        bucket_data = {k: defaultdict(list) for k in BUCKETS}
        for t in raw:
            if not isinstance(t, list) or len(t) < 5:
                continue
            t_period = int(t[4])
            t_rate = round(t[3] * 365 * 100, 4)
            date_str = datetime.datetime.fromtimestamp(t[1] / 1000, tz=TZ_OFFSET).strftime('%Y-%m-%d')
            for bk, (lo, hi) in BUCKETS.items():
                if lo < t_period <= hi:
                    bucket_data[bk][date_str].append(t_rate)
                    break
        series = {}
        for bk, day_rates in bucket_data.items():
            series[bk] = sorted([
                {'date': d, 'max_rate': max(rates), 'min_rate': min(rates), 'count': len(rates)}
                for d, rates in day_rates.items()
            ], key=lambda x: x['date'])
    return jsonify({'periods': [120, 30, 2], 'series': series})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
