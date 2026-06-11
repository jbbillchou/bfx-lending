import requests
import json
import time
import hmac
import hashlib
import datetime

from config import API_KEY, API_SECRET, BASE_URL, SYMBOLS

# Funding Offers 欄位索引
OFFER_ID = 0
OFFER_MTS_CREATE = 2
OFFER_AMOUNT = 4
OFFER_STATUS = 10
OFFER_RATE = 14
OFFER_PERIOD = 15

# Funding Credits（成交放貸，出借方）欄位索引
CREDIT_AMOUNT = 5
CREDIT_RATE = 11
CREDIT_PERIOD = 12
CREDIT_MTS_OPENING = 13

# Ledger 欄位索引
LEDGER_ID = 0
LEDGER_MTS = 3
LEDGER_AMOUNT = 5
LEDGER_BALANCE = 6
LEDGER_DESC = 8


def _sign_request(api_path, body):
    nonce = str(int(time.time() * 1000000))
    signature = '/api/' + api_path + nonce + json.dumps(body)
    h = hmac.new(API_SECRET.encode(), signature.encode(), hashlib.sha384)
    sig = h.hexdigest()
    headers = {
        'bfx-nonce': nonce,
        'bfx-apikey': API_KEY,
        'bfx-signature': sig,
        'content-type': 'application/json'
    }
    return headers


def _calc_remaining_days(mts_opening, period):
    opening_dt = datetime.datetime.fromtimestamp(mts_opening / 1000)
    expiry_dt = opening_dt + datetime.timedelta(days=period)
    remaining = (expiry_dt - datetime.datetime.now()).total_seconds() / 86400
    return max(0.0, round(remaining, 4))


def _daily_to_annual_pct(daily_rate):
    return round(daily_rate * 365 * 100, 4)


def _post_with_retry(api_path, body=None, retries=2, backoff=1.0):
    """POST with retry on transient server errors (5xx)."""
    if body is None:
        body = {}
    last_resp = None
    for attempt in range(retries + 1):
        headers = _sign_request(api_path, body)
        resp = requests.post(f'{BASE_URL}/{api_path}', headers=headers, data=json.dumps(body))
        if resp.status_code < 500:
            resp.raise_for_status()
            return resp
        last_resp = resp
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))
    last_resp.raise_for_status()


def get_wallets():
    api_path = 'v2/auth/r/wallets'
    response = _post_with_retry(api_path)
    wallets = response.json()
    result = []
    for symbol in SYMBOLS:
        wallet = next((w for w in wallets if w[0] == 'funding' and w[1] == symbol), None)
        total = wallet[2] if wallet else 0.0
        available = wallet[4] if wallet else 0.0
        result.append({'symbol': symbol, 'total': total, 'available': available})
    return result


def get_frr():
    """取得各幣種的 FRR（日利率），回傳 {symbol: annual_pct}。"""
    symbols_param = ','.join(f'f{s}' for s in SYMBOLS)
    response = requests.get(f'{BASE_URL}/v2/tickers', params={'symbols': symbols_param})
    if not response.ok:
        return {}
    result = {}
    for ticker in response.json():
        sym_key = ticker[0]  # e.g. 'fUST'
        sym = sym_key[1:]     # strip leading 'f'
        if sym in SYMBOLS:
            result[sym] = {
                'annual_pct': _daily_to_annual_pct(ticker[1]),
                'daily_pct': round(ticker[1] * 100, 6),
                'market_bid_annual_pct': _daily_to_annual_pct(ticker[2]),
                'market_bid_daily_pct': round(ticker[2] * 100, 6),
                'market_ask_annual_pct': _daily_to_annual_pct(ticker[5]),
                'market_ask_daily_pct': round(ticker[5] * 100, 6),
            }
    return result


def get_funding_offers():
    result = []
    for symbol in SYMBOLS:
        api_path = f'v2/auth/r/funding/offers/f{symbol}'
        try:
            response = _post_with_retry(api_path)
        except Exception:
            continue
        for offer in response.json():
            result.append({
                'id': offer[OFFER_ID],
                'symbol': symbol,
                'amount': abs(offer[OFFER_AMOUNT]),
                'rate_annual_pct': _daily_to_annual_pct(offer[OFFER_RATE]),
                'rate_daily_pct': round(offer[OFFER_RATE] * 100, 6),
                'period_days': offer[OFFER_PERIOD],
                'status': offer[OFFER_STATUS],
                'created_at': offer[OFFER_MTS_CREATE],
            })
    return result


def cancel_offer(offer_id):
    """取消單筆掛單。HTTP 成功即視為成功。"""
    api_path = 'v2/auth/w/funding/offer/cancel'
    body = {'id': offer_id}
    _post_with_retry(api_path, body)
    return True


def submit_offer(symbol, amount, rate_annual_pct, period_days):
    """掛新單。rate_annual_pct 為年化百分比，內部換算成日利率。回傳 offer_id 或 None。"""
    api_path = 'v2/auth/w/funding/offer/submit'
    rate_daily = rate_annual_pct / 365 / 100
    body = {
        'type': 'LIMIT',
        'symbol': f'f{symbol}',
        'amount': str(round(amount, 8)),
        'rate': str(round(rate_daily, 10)),
        'period': int(period_days),
    }
    response = _post_with_retry(api_path, body)
    data = response.json()
    if isinstance(data, list) and len(data) > 4:
        offer_data = data[4]
        if isinstance(offer_data, list) and len(offer_data) > 0:
            return offer_data[0]
    return None


def get_funding_credits_history(symbol, start_ms=None):
    """取得歷史已結束的放貸記錄（借出已結算），用於補充 lending_executions。"""
    api_path = f'v2/auth/r/funding/credits/f{symbol}/hist'
    body = {'limit': 500}
    if start_ms:
        body['start'] = start_ms + 1
    try:
        response = _post_with_retry(api_path, body)
    except Exception:
        return []
    result = []
    for credit in response.json():
        if not isinstance(credit, list) or len(credit) < 14:
            continue
        bfx_id = str(credit[0])
        amount = abs(credit[CREDIT_AMOUNT])
        daily_rate = credit[CREDIT_RATE]
        period = credit[CREDIT_PERIOD]
        mts_opening = credit[CREDIT_MTS_OPENING]
        if not daily_rate or not mts_opening:
            continue
        result.append({
            'bfx_id': bfx_id,
            'symbol': symbol,
            'amount': round(amount, 6),
            'rate_annual_pct': _daily_to_annual_pct(daily_rate),
            'period_days': int(period),
            'ts': mts_opening,
            'date': datetime.datetime.fromtimestamp(mts_opening / 1000).strftime('%Y-%m-%d'),
        })
    return result


def get_ledger_earnings(currency, start_ms=None):
    api_path = f'v2/auth/r/ledgers/{currency}/hist'
    body = {'limit': 500}
    if start_ms:
        body['start'] = start_ms + 1  # +1 避免重複拉同一筆
    try:
        response = _post_with_retry(api_path, body)
    except Exception:
        return []
    result = []
    for entry in response.json():
        desc = entry[LEDGER_DESC] or ''
        if 'Margin Funding Payment' not in desc:
            continue
        ts = entry[LEDGER_MTS]
        result.append({
            'id': str(entry[LEDGER_ID]),
            'currency': currency,
            'amount': entry[LEDGER_AMOUNT],
            'balance': entry[LEDGER_BALANCE],
            'description': desc,
            'ts': ts,
            'date': datetime.datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d'),
        })
    return result


_USD_SYMBOLS = {'USD', 'UST'}


def get_usd_prices():
    """取得各幣種的 USD 現貨價格，回傳 {symbol: price}。"""
    prices = {s: 1.0 for s in SYMBOLS if s in _USD_SYMBOLS}
    non_usd = [s for s in SYMBOLS if s not in _USD_SYMBOLS]
    if non_usd:
        syms_param = ','.join(f't{s}USD' for s in non_usd)
        resp = requests.get(f'{BASE_URL}/v2/tickers', params={'symbols': syms_param})
        if resp.ok:
            for ticker in resp.json():
                key = ticker[0]   # e.g. 'tETHUSD'
                sym = key[1:-3]   # strip leading 't' and trailing 'USD'
                if sym in non_usd:
                    prices[sym] = float(ticker[7])  # last price
    return prices


def get_funding_candles(symbol='UST', timeframe='1h', limit=168):
    """取得 fUST funding 利率 K 線（OHLCV）。回傳格式：[[MTS, OPEN, CLOSE, HIGH, LOW, VOLUME], ...]
    Bitfinex funding candles 需加 :a30:p2:p30 聚合參數才有資料。"""
    url = f'{BASE_URL}/v2/candles/trade:{timeframe}:f{symbol}:a30:p2:p30/hist'
    r = requests.get(url, params={'limit': limit, 'sort': -1})
    r.raise_for_status()
    return list(reversed(r.json()))


def get_funding_orderbook(symbol='UST'):
    """取得 fUST Order Book（P0 精度）。回傳格式：[[RATE, PERIOD, COUNT, AMOUNT], ...]"""
    url = f'{BASE_URL}/v2/book/f{symbol}/P0'
    r = requests.get(url, params={'len': 25})
    r.raise_for_status()
    return r.json()


def get_funding_trades(symbol='UST', limit=100, start_ms=None, end_ms=None, sort=None):
    """取得 fUST 最近成交記錄。回傳格式：[[ID, MTS, AMOUNT, RATE, PERIOD], ...]
    sort: 1=升序(舊→新), -1=降序(新→舊)。預設：有 start_ms 時降序取最新，否則降序。
    """
    url = f'{BASE_URL}/v2/trades/f{symbol}/hist'
    params = {'limit': limit, 'sort': sort if sort is not None else -1}
    if start_ms is not None:
        params['start'] = start_ms
    if end_ms is not None:
        params['end'] = end_ms
    r = requests.get(url, params=params)
    r.raise_for_status()
    return r.json()




def get_my_funding_trades_avg_6h(symbol, hours=6):
    """取得近 N 小時市場放貸成交均值（年化 %）。無成交回傳 None。"""
    start_ms = int((time.time() - hours * 3600) * 1000)
    url = f'{BASE_URL}/v2/trades/f{symbol}/hist'
    r = requests.get(url, params={'start': start_ms, 'limit': 500, 'sort': 1})
    if not r.ok:
        return None
    trades = r.json()
    if not trades or not isinstance(trades, list) or not isinstance(trades[0], list):
        return None
    # trades[i] = [ID, MTS, AMOUNT, RATE, PERIOD]
    rates = [_daily_to_annual_pct(t[3]) for t in trades if len(t) > 3]
    if not rates:
        return None
    return round(sum(rates) / len(rates), 4)


def get_recent_short_term_max_rate(symbol, hours=6, max_period=2):
    """取得近 N 小時、期數 <= max_period 天的最高成交利率（年化 %）。無成交回傳 None。"""
    start_ms = int((time.time() - hours * 3600) * 1000)
    url = f'{BASE_URL}/v2/trades/f{symbol}/hist'
    r = requests.get(url, params={'start': start_ms, 'limit': 500, 'sort': 1})
    if not r.ok:
        return None
    trades = r.json()
    if not trades or not isinstance(trades, list):
        return None
    # trades[i] = [ID, MTS, AMOUNT, RATE, PERIOD]
    rates = [
        _daily_to_annual_pct(t[3])
        for t in trades
        if isinstance(t, list) and len(t) >= 5 and int(t[4]) <= max_period
    ]
    if not rates:
        return None
    return round(max(rates), 4)


def get_recent_trade_max_rates_by_period(symbol, periods, hours=6):
    """
    取得近 N 小時各期數區間的最高成交利率（年化 %）。
    period bucket 邏輯與 lending_engine.get_market_rates_by_period 相同：
      sorted_periods DESC → bucket[i] = (sorted_periods[i+1], sorted_periods[i]]
    回傳: {period: max_rate_annual_pct or None}
    """
    start_ms = int((time.time() - hours * 3600) * 1000)
    trades = get_funding_trades(symbol, limit=5000, start_ms=start_ms)
    if not trades or not isinstance(trades, list):
        return {p: None for p in periods}

    sorted_periods = sorted(periods, reverse=True)
    result = {p: None for p in periods}

    for trade in trades:
        if not isinstance(trade, list) or len(trade) < 5:
            continue
        t_period = int(trade[4])
        t_rate = _daily_to_annual_pct(trade[3])
        for i, p in enumerate(sorted_periods):
            lower = sorted_periods[i + 1] if i + 1 < len(sorted_periods) else 0
            if lower < t_period <= p:
                if result[p] is None or t_rate > result[p]:
                    result[p] = round(t_rate, 4)
                break

    return result


def get_funding_loans():
    result = []
    for symbol in SYMBOLS:
        api_path = f'v2/auth/r/funding/credits/f{symbol}'
        try:
            response = _post_with_retry(api_path)
        except Exception:
            continue
        for credit in response.json():
            period = credit[CREDIT_PERIOD]
            mts_opening = credit[CREDIT_MTS_OPENING]
            remaining = _calc_remaining_days(mts_opening, period)
            _tz8 = datetime.timezone(datetime.timedelta(hours=8))
            opened_at = datetime.datetime.fromtimestamp(mts_opening / 1000, tz=_tz8).strftime('%Y-%m-%d %H:%M')
            amount = abs(credit[CREDIT_AMOUNT])
            daily_rate = credit[CREDIT_RATE]
            expected_earnings = round(amount * daily_rate * period, 6)
            result.append({
                'symbol': symbol,
                'amount': amount,
                'rate_annual_pct': _daily_to_annual_pct(daily_rate),
                'period_days': period,
                'remaining_days': remaining,
                'opened_at': opened_at,
                'expected_earnings': expected_earnings,
            })
    return result
