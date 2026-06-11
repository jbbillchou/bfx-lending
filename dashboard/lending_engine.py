"""
放貸決策共用模組。
- sym_val：per-symbol 設定取值
- scan_period_targets：掃描 Order Book，從最長期數往下匹配目標利率
- resolve_min_annual：保底利率取值（支援 auto 模式 = 近 6h 最高成交 ceil 到整數）
"""
import math

import bfx_client as _bfx_client


def sym_val(cfg, symbol, default):
    """從 per-symbol dict 或 scalar 取值，向下相容舊設定格式。"""
    if isinstance(cfg, dict):
        return cfg.get(symbol, default)
    if cfg is not None:
        return cfg
    return default


def resolve_min_annual(settings, symbol, recent_6h_max=None):
    """
    取該幣種保底利率。
    - auto=True：recent_6h_max ceil 到整數
    - auto=False 或 recent_6h_max 為 None/0：回退手動設定 min_annual_rates
    回傳 (min_annual_pct, source)，source ∈ {'manual', 'auto', 'auto_fallback'}

    recent_6h_max 應為 UI 所顯示的「近 6h 最高成交利率」同一個值
    （目前是 period-2 bucket 的最高成交），與 lending.py fallback 口徑一致。
    """
    manual = sym_val(settings.get('min_annual_rates'), symbol, 8.0)
    auto = sym_val(settings.get('min_annual_auto'), symbol, False)
    if not auto:
        return float(manual), 'manual'
    if not recent_6h_max or recent_6h_max <= 0:
        return float(manual), 'auto_fallback'
    return round(float(recent_6h_max), 4), 'auto'


def scan_period_targets(symbol, targets, fallback_rate=None, lent_by_period=None, base_amount=None, recent_trade_rates=None):
    """
    掃描 Order Book BID 側，從最長期數往下找符合目標利率的 BID。

    targets: [{"period": 120, "min_rate": 12.0, "max_pct": 30}, ...]
             會自動依 period DESC 排序
    lent_by_period: {period: already_lent_amount}，用於跳過已滿額的期數
    base_amount: max_pct 的計算基準金額

    回傳:
      {
        "matched": True/False,
        "allocations": [...],   # 每個匹配期數的配置
        "fallback_rate": float,
        "period": int,          # backward compat
        "rate": float,
        "reason": str,
        "best_bid": float|None,
        "scan_detail": [...]
      }
    """
    scan_detail = []
    sorted_targets = sorted(targets, key=lambda t: t['period'], reverse=True)

    # 取得 Order Book
    try:
        book = _bfx_client.get_funding_orderbook(symbol)
    except Exception as e:
        print(f"  [Period] Order Book 取得失敗：{e}")
        # API 失敗 → fallback
        fr = fallback_rate if fallback_rate is not None else 0
        return {
            'matched': False,
            'period': 2,
            'rate': fr,
            'reason': 'api_error',
            'best_bid': None,
            'scan_detail': [],
        }

    # 解析 BID 側（Bitfinex funding book: amount < 0 = 借方 BID）
    bids = []
    for entry in book:
        if not isinstance(entry, list) or len(entry) < 4:
            continue
        if entry[3] < 0:  # 借方 BID
            rate_annual = round(entry[0] * 365 * 100, 4)
            period = int(entry[1])
            amount = abs(entry[3])
            bids.append({'rate_annual': rate_annual, 'period': period, 'amount': amount})

    print(f"  [Period] BID 側共 {len(bids)} 筆")

    fr = fallback_rate if fallback_rate is not None else 0

    # 從最長期數往下掃描
    allocations = []
    for i, target in enumerate(sorted_targets):
        t_period = target['period']
        t_min_rate = target['min_rate']
        t_max_pct = target.get('max_pct')  # None = 無限制（吃完剩餘，停止掃描）

        # 找此期數範圍內的 BID（BID.period <= 我的 ASK 期數，才能撮合）
        matching_bids = [b for b in bids if b['period'] <= t_period]
        best_bid = max((b['rate_annual'] for b in matching_bids), default=None)

        # effective_rate = max(best_bid, 近6h成交) — 以近期成交為錨點下限
        t_recent = (recent_trade_rates or {}).get(t_period)
        candidates = [x for x in [best_bid, t_recent] if x is not None]
        effective_rate = max(candidates) if candidates else None

        matched = effective_rate is not None and effective_rate >= t_min_rate

        detail = {
            'period': t_period,
            'min_rate': t_min_rate,
            'max_pct': t_max_pct,
            'bid_count': len(matching_bids),
            'best_bid': best_bid,
            'recent_6h': t_recent,
            'effective_rate': effective_rate,
            'matched': matched,
        }
        scan_detail.append(detail)

        # 若有設定 max_pct，檢查此期數是否已放滿
        if matched and t_max_pct is not None and lent_by_period is not None and base_amount is not None:
            expected = base_amount * t_max_pct / 100
            already_lent = lent_by_period.get(t_period, 0)
            if already_lent >= expected:
                detail['skipped_full'] = True
                detail['already_lent'] = already_lent
                detail['expected_cap'] = round(expected, 2)
                print(f"  [Period] ⏭ {t_period}天：已放貸 {already_lent:.2f} >= 配額 {expected:.2f}，跳過")
                continue

        if matched:
            # Look-ahead：確認沒有更低期數提供更高 effective_rate（且自身也能匹配）
            lower_targets = sorted_targets[i + 1:]
            better_exists = False
            for lower_t in lower_targets:
                l_period = lower_t['period']
                l_recent = (recent_trade_rates or {}).get(l_period)
                l_bids = [b for b in bids if b['period'] <= l_period]
                l_best_bid = max((b['rate_annual'] for b in l_bids), default=None)
                l_candidates = [x for x in [l_best_bid, l_recent] if x is not None]
                l_effective = max(l_candidates) if l_candidates else None
                if (l_effective is not None
                        and l_effective > effective_rate
                        and l_effective >= lower_t['min_rate']):
                    better_exists = True
                    print(f"  [Period] ⏭ {t_period}天：低期數 {l_period}天 effective {l_effective:.4f}% 更高，跳過")
                    break

            if better_exists:
                detail['skipped_lookahead'] = True
                continue

            pct_label = f"max_pct={t_max_pct}%" if t_max_pct is not None else "無限制"
            rate_note = f" (recent {t_recent:.4f}%)" if t_recent and t_recent > (best_bid or 0) else ""
            print(f"  [Period] ✅ {t_period}天：effective {effective_rate:.4f}%{rate_note}，{pct_label}")
            allocations.append({
                'period': t_period,
                'rate': effective_rate,
                'max_pct': t_max_pct,
                'reason': f'period_{t_period}',
            })
            if t_max_pct is None:
                # 無限制：吃完剩餘資金，不再往下掃描
                break
        else:
            bid_info = f"effective {effective_rate:.4f}%" if effective_rate else "無資料"
            print(f"  [Period] ❌ {t_period}天：{bid_info} < 目標 {t_min_rate:.2f}%")

    if allocations:
        first = allocations[0]
        print(f"  [Period] 共 {len(allocations)} 個配置，fallback_rate={fr:.4f}%")
        return {
            'matched': True,
            'allocations': allocations,
            'fallback_rate': fr,
            'scan_detail': scan_detail,
            # backward compat
            'period': first['period'],
            'rate': first['rate'],
            'reason': first['reason'],
            'best_bid': first['rate'],
        }

    # 全部未匹配 → fallback：2 天 + ASK/FRR
    print(f"  [Period] 全部未匹配 → fallback 2天 {fr:.4f}%")
    return {
        'matched': False,
        'allocations': [],
        'fallback_rate': fr,
        'period': 2,
        'rate': fr,
        'reason': 'fallback',
        'best_bid': None,
        'scan_detail': scan_detail,
    }


def get_market_rates_by_period(symbol, periods):
    """
    取得各期數的市場利率參考（供 UX 顯示）。
    回傳: {period: {"best_bid": float|None, "bid_count": int}}
    """
    try:
        book = _bfx_client.get_funding_orderbook(symbol)
    except Exception:
        return {p: {'best_bid': None, 'bid_count': 0} for p in periods}

    bids = []
    for entry in book:
        if not isinstance(entry, list) or len(entry) < 4:
            continue
        if entry[3] < 0:
            bids.append({
                'rate_annual': round(entry[0] * 365 * 100, 4),
                'period': int(entry[1]),
                'amount': round(abs(entry[3]), 2),
            })

    sorted_periods = sorted(periods, reverse=True)
    result = {}
    for i, p in enumerate(sorted_periods):
        lower = sorted_periods[i + 1] if i + 1 < len(sorted_periods) else 0
        matching = [b for b in bids if lower < b['period'] <= p]
        best = max((b['rate_annual'] for b in matching), default=None)
        worst = min((b['rate_annual'] for b in matching), default=None)
        result[p] = {
            'best_bid': best,
            'min_bid': worst,
            'bid_count': len(matching),
            'bid_orders': sorted(
                [{'rate': b['rate_annual'], 'period': b['period'], 'amount': b['amount']} for b in matching],
                key=lambda x: -x['rate']
            ),
        }
    return result
