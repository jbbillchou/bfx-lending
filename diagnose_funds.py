# 資金對帳：找出錢包總額中「不在可用、掛單、放貸」的那部分資金去了哪裡
# 用法：docker compose exec dashboard python3 /app/diagnose_funds.py

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'dashboard'))

import bfx_client
from config import SYMBOLS
import settings as _settings_module


def _sum_endpoint(kind, symbol):
    resp = bfx_client._post_with_retry(f'v2/auth/r/funding/{kind}/f{symbol}')
    rows = [r for r in resp.json() if isinstance(r, list)]
    return len(rows), sum(abs(r[bfx_client.CREDIT_AMOUNT if kind != 'offers' else bfx_client.OFFER_AMOUNT]) for r in rows)


s = _settings_module.load()
for w in bfx_client.get_wallets():
    sym = w['symbol']
    total = w['total'] or 0.0
    available = w['available'] or 0.0
    n_off, offers = _sum_endpoint('offers', sym)
    n_cr, credits = _sum_endpoint('credits', sym)
    n_ln, loans = _sum_endpoint('loans', sym)
    unexplained = total - available - offers - credits - loans

    print(f'=== {sym} ===')
    print(f'錢包總額 total          : {total:12.2f}')
    print(f'可用 available          : {available:12.2f}')
    print(f'掛單中 offers   ({n_off:3d} 筆): {offers:12.2f}')
    print(f'放貸 credits    ({n_cr:3d} 筆): {credits:12.2f}  ← 舊版 Dashboard 只算這個')
    print(f'放貸 loans      ({n_ln:3d} 筆): {loans:12.2f}  ← 舊版 Dashboard 漏算')
    print(f'未解釋差額              : {unexplained:12.2f}')
    if total > 0:
        print(f'舊版使用率 credits/total        : {credits / total * 100:6.1f}%')
        print(f'實際使用率 (credits+loans)/total: {(credits + loans) / total * 100:6.1f}%')
    max_amt = s.get('max_amounts', {}).get(sym)
    if max_amt:
        print(f'max_amounts 上限        : {max_amt:12.2f}  （超過此額度的資金不會被掛出）')
    if available >= 150:
        print('⚠️ 可用餘額 >= 最低掛單金額卻未掛出：檢查 max_amounts、symbol_enabled、或 lending.py log')
    print()
