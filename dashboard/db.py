import json
import sqlite3
import os
import datetime

TZ8 = datetime.timezone(datetime.timedelta(hours=8))

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'earnings.db')


def _conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS earnings (
                id          TEXT PRIMARY KEY,
                currency    TEXT NOT NULL,
                amount      REAL NOT NULL,
                balance     REAL,
                description TEXT,
                ts          INTEGER NOT NULL,
                date        TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                date             TEXT PRIMARY KEY,
                total_usd        REAL NOT NULL,
                detail           TEXT NOT NULL,
                available_detail TEXT
            )
        """)
        # 向舊版 schema 補欄位（idempotent）
        try:
            con.execute("ALTER TABLE portfolio_snapshots ADD COLUMN available_detail TEXT")
        except Exception:
            pass
        con.execute("""
            CREATE TABLE IF NOT EXISTS lending_executions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              INTEGER NOT NULL,
                date            TEXT NOT NULL,
                symbol          TEXT NOT NULL,
                amount          REAL NOT NULL,
                rate_annual_pct REAL NOT NULL,
                period_days     INTEGER NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS utilization_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              INTEGER NOT NULL,
                date            TEXT NOT NULL,
                detail          TEXT NOT NULL,
                available_detail TEXT NOT NULL,
                loans_detail    TEXT
            )
        """)
        # 向舊版 schema 補欄位（idempotent）
        try:
            con.execute("ALTER TABLE utilization_snapshots ADD COLUMN loans_detail TEXT")
        except Exception:
            pass
        try:
            con.execute("ALTER TABLE utilization_snapshots ADD COLUMN offers_detail TEXT")
        except Exception:
            pass
        try:
            con.execute("ALTER TABLE utilization_snapshots ADD COLUMN weighted_avg_rate REAL")
        except Exception:
            pass
        try:
            con.execute("ALTER TABLE lending_executions ADD COLUMN bfx_id TEXT")
        except Exception:
            pass
        try:
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_lending_bfx_id "
                "ON lending_executions(bfx_id) WHERE bfx_id IS NOT NULL"
            )
        except Exception:
            pass
        _init_trade_rate_snapshots(con)
        con.execute("""
            CREATE TABLE IF NOT EXISTS decision_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              INTEGER NOT NULL,
                date            TEXT NOT NULL,
                symbol          TEXT NOT NULL,
                balance         REAL NOT NULL,
                frr_annual_pct  REAL,
                ask_annual_pct  REAL,
                bid_annual_pct  REAL,
                floor_rate      REAL,
                floor_source    TEXT,
                scan_detail     TEXT,
                action          TEXT NOT NULL,
                allocations     TEXT,
                stdout_log      TEXT,
                run_detail      TEXT
            )
        """)


def _init_trade_rate_snapshots(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS trade_rate_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              INTEGER NOT NULL,
            date            TEXT NOT NULL,
            hour            TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            period_bucket   TEXT NOT NULL,
            max_rate        REAL,
            min_rate        REAL,
            avg_rate        REAL,
            trade_count     INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_trs_hour_sym_bk "
        "ON trade_rate_snapshots(hour, symbol, period_bucket)"
    )


def upsert_earnings(entries):
    if not entries:
        return 0
    with _conn() as con:
        cur = con.executemany(
            """
            INSERT OR IGNORE INTO earnings
                (id, currency, amount, balance, description, ts, date)
            VALUES
                (:id, :currency, :amount, :balance, :description, :ts, :date)
            """,
            entries
        )
        return cur.rowcount


def get_latest_ts():
    with _conn() as con:
        row = con.execute("SELECT MAX(ts) FROM earnings").fetchone()
        return row[0] if row and row[0] else None


def insert_lending_execution(symbol, amount, rate_annual_pct, period_days):
    ts = int(datetime.datetime.now().timestamp() * 1000)
    date = datetime.date.today().isoformat()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO lending_executions (ts, date, symbol, amount, rate_annual_pct, period_days)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ts, date, symbol, amount, rate_annual_pct, period_days)
        )


def upsert_lending_executions_from_credits(entries):
    """從 Bitfinex credits history 補充 lending_executions（bfx_id 去重）。"""
    if not entries:
        return 0
    with _conn() as con:
        cur = con.executemany(
            """
            INSERT OR IGNORE INTO lending_executions
                (ts, date, symbol, amount, rate_annual_pct, period_days, bfx_id)
            VALUES
                (:ts, :date, :symbol, :amount, :rate_annual_pct, :period_days, :bfx_id)
            """,
            entries
        )
        return cur.rowcount


def get_latest_lending_execution_ts(symbol):
    """取得該幣種最新一筆 lending_executions 的 ts（毫秒），供增量同步用。"""
    with _conn() as con:
        row = con.execute(
            "SELECT MAX(ts) FROM lending_executions WHERE symbol = ? AND bfx_id IS NOT NULL",
            (symbol,)
        ).fetchone()
    return row[0] if row and row[0] else None


def get_today_earnings_summary():
    today = datetime.date.today().isoformat()
    month = today[:7]
    with _conn() as con:
        today_rows = con.execute(
            "SELECT currency, SUM(amount) FROM earnings WHERE date = ? GROUP BY currency",
            (today,)
        ).fetchall()
        month_rows = con.execute(
            "SELECT currency, SUM(amount) FROM earnings WHERE date LIKE ? GROUP BY currency",
            (f'{month}%',)
        ).fetchall()
    return {
        'today': {r[0]: round(r[1], 6) for r in today_rows},
        'month': {r[0]: round(r[1], 6) for r in month_rows},
        'date': today,
    }


def get_last_portfolio_snapshot():
    """取得最近一筆 portfolio_snapshot，供 utilization snapshot fallback 使用。"""
    with _conn() as con:
        row = con.execute(
            "SELECT detail, available_detail FROM portfolio_snapshots ORDER BY date DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return {
        'detail': json.loads(row[0]) if row[0] else {},
        'available_detail': json.loads(row[1]) if row[1] else {},
    }


def save_snapshot(date, total_usd, detail, available_detail=None):
    with _conn() as con:
        con.execute(
            """INSERT OR REPLACE INTO portfolio_snapshots
               (date, total_usd, detail, available_detail) VALUES (?, ?, ?, ?)""",
            (date, total_usd, json.dumps(detail),
             json.dumps(available_detail) if available_detail is not None else None)
        )


def insert_utilization_snapshot(detail, available_detail, loans_detail=None, offers_detail=None, weighted_avg_rate=None):
    now8 = datetime.datetime.now(tz=TZ8)
    ts = int(now8.timestamp() * 1000)
    date = now8.date().isoformat()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO utilization_snapshots
                (ts, date, detail, available_detail, loans_detail, offers_detail, weighted_avg_rate)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, date, json.dumps(detail), json.dumps(available_detail),
             json.dumps(loans_detail) if loans_detail is not None else None,
             json.dumps(offers_detail) if offers_detail is not None else None,
             weighted_avg_rate)
        )


def get_monthly_earnings(month):
    """month='2026-04'，回傳該月每日明細 + 月份合計。"""
    with _conn() as con:
        total_rows = con.execute(
            "SELECT currency, SUM(amount) FROM earnings WHERE date LIKE ? GROUP BY currency",
            (f'{month}%',)
        ).fetchall()
        daily_rows = con.execute(
            "SELECT date, currency, SUM(amount) FROM earnings WHERE date LIKE ? "
            "GROUP BY date, currency ORDER BY date DESC",
            (f'{month}%',)
        ).fetchall()
    total = {r[0]: round(r[1], 6) for r in total_rows}
    daily = {}
    for date, currency, amount in daily_rows:
        daily.setdefault(date, {})[currency] = round(amount, 6)
    return {'month': month, 'total': total, 'daily': daily}


def get_monthly_earnings_volume():
    """回傳當月與上月的收益總額（USD-equivalent），用於總覽頁 chip 顯示。"""
    import datetime as _dt
    today = _dt.date.today()
    cur_month = today.strftime('%Y-%m')
    # 上個月
    first_of_month = today.replace(day=1)
    prev_last = first_of_month - _dt.timedelta(days=1)
    prev_month = prev_last.strftime('%Y-%m')
    with _conn() as con:
        cur_row = con.execute(
            "SELECT SUM(amount) FROM earnings WHERE date LIKE ?",
            (f'{cur_month}%',)
        ).fetchone()
        prev_row = con.execute(
            "SELECT SUM(amount) FROM earnings WHERE date LIKE ?",
            (f'{prev_month}%',)
        ).fetchone()
    return {
        'current_month': cur_month,
        'current_volume': round(cur_row[0] or 0, 4),
        'prev_month': prev_month,
        'prev_volume': round(prev_row[0] or 0, 4),
        'current_days': today.day,
    }


def get_ytd_stats():
    """
    回傳收益年化報酬率統計。
    以最早可用的 portfolio_snapshot 作為基準（若無 1/1 快照則取最早一筆）。
    """
    today = datetime.date.today()
    year_start = f'{today.year}-01-01'
    with _conn() as con:
        # 優先取 year_start 當天或更早的快照（年初基準）
        snap_start = con.execute(
            "SELECT date, total_usd FROM portfolio_snapshots "
            "WHERE date <= ? ORDER BY date DESC LIMIT 1",
            (year_start,)
        ).fetchone()
        # 若無年初快照，改用最早一筆（bot 啟動後第一天的餘額）
        if not snap_start:
            snap_start = con.execute(
                "SELECT date, total_usd FROM portfolio_snapshots ORDER BY date ASC LIMIT 1"
            ).fetchone()
        portfolio_start_date = snap_start[0] if snap_start else None
        # 收益窗口對齊快照日，確保分子分母起點一致
        earn_rows = con.execute(
            "SELECT currency, SUM(amount) FROM earnings WHERE date >= ? GROUP BY currency",
            (portfolio_start_date or year_start,)
        ).fetchall()
        snap_now = con.execute(
            "SELECT total_usd FROM portfolio_snapshots ORDER BY date DESC LIMIT 1"
        ).fetchone()
        # 上月底快照
        first_of_this_month = today.replace(day=1)
        prev_month_last = (first_of_this_month - datetime.timedelta(days=1)).isoformat()
        snap_prev = con.execute(
            "SELECT total_usd, date FROM portfolio_snapshots "
            "WHERE date <= ? ORDER BY date DESC LIMIT 1",
            (prev_month_last,)
        ).fetchone()
    ytd_total = {r[0]: round(r[1], 6) for r in earn_rows}
    portfolio_start = snap_start[1] if snap_start else None
    portfolio_now = snap_now[0] if snap_now else None
    portfolio_prev_month = snap_prev[0] if snap_prev else None
    portfolio_prev_month_date = snap_prev[1] if snap_prev else None
    annualized_pct = None
    start_date_obj = datetime.date.fromisoformat(portfolio_start_date) if portfolio_start_date else None
    if start_date_obj and portfolio_start and portfolio_start > 0:
        elapsed_days = max((today - start_date_obj).days, 1)
        earn_since = sum(ytd_total.values())
        annualized_pct = round((earn_since / portfolio_start) * (365 / elapsed_days) * 100, 2)
    return {
        'year': today.year,
        'ytd_total': ytd_total,
        'start_date': portfolio_start_date,
        'annualized_pct': annualized_pct,
        'portfolio_start': portfolio_start,
        'portfolio_now': portfolio_now,
        'portfolio_prev_month': portfolio_prev_month,
        'portfolio_prev_month_date': portfolio_prev_month_date,
    }


def get_earnings_range_total(months):
    """取得最近 N 個月的收益合計 {currency: total}。"""
    cutoff = (datetime.date.today().replace(day=1) - datetime.timedelta(days=1))
    # 往回推 months-1 個月後取該月份第 1 天
    y, m = cutoff.year, cutoff.month
    for _ in range(months - 1):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    start_date = f'{y:04d}-{m:02d}-01'
    with _conn() as con:
        rows = con.execute(
            "SELECT currency, SUM(amount) FROM earnings WHERE date >= ? GROUP BY currency",
            (start_date,)
        ).fetchall()
    return {r[0]: round(r[1], 6) for r in rows}


def insert_decision_log(record):
    with _conn() as con:
        con.execute(
            """
            INSERT INTO decision_logs
                (ts, date, symbol, balance, frr_annual_pct, ask_annual_pct, bid_annual_pct,
                 floor_rate, floor_source, scan_detail, action, allocations, stdout_log, run_detail)
            VALUES
                (:ts, :date, :symbol, :balance, :frr_annual_pct, :ask_annual_pct, :bid_annual_pct,
                 :floor_rate, :floor_source, :scan_detail, :action, :allocations, :stdout_log, :run_detail)
            """,
            record
        )


def get_decision_logs(date_str=None, limit=200):
    with _conn() as con:
        if date_str:
            rows = con.execute(
                """
                SELECT id, ts, date, symbol, balance, frr_annual_pct, ask_annual_pct, bid_annual_pct,
                       floor_rate, floor_source, scan_detail, action, allocations, stdout_log, run_detail
                FROM decision_logs
                WHERE date = ?
                ORDER BY ts DESC
                LIMIT ?
                """,
                (date_str, limit)
            ).fetchall()
        else:
            today = datetime.datetime.now(tz=TZ8).date().isoformat()
            rows = con.execute(
                """
                SELECT id, ts, date, symbol, balance, frr_annual_pct, ask_annual_pct, bid_annual_pct,
                       floor_rate, floor_source, scan_detail, action, allocations, stdout_log, run_detail
                FROM decision_logs
                WHERE date = ?
                ORDER BY ts DESC
                LIMIT ?
                """,
                (today, limit)
            ).fetchall()
    columns = ['id', 'ts', 'date', 'symbol', 'balance', 'frr_annual_pct', 'ask_annual_pct',
               'bid_annual_pct', 'floor_rate', 'floor_source', 'scan_detail', 'action',
               'allocations', 'stdout_log', 'run_detail']
    result = []
    for row in rows:
        d = dict(zip(columns, row))
        for key in ('scan_detail', 'allocations', 'run_detail'):
            if d[key]:
                d[key] = json.loads(d[key])
        result.append(d)
    return result


def get_decision_log_stdout(log_id):
    with _conn() as con:
        row = con.execute(
            "SELECT stdout_log FROM decision_logs WHERE id = ?", (log_id,)
        ).fetchone()
    return row[0] if row else None


def purge_old_decision_logs(days=7):
    cutoff_ms = int((datetime.datetime.now().timestamp() - days * 86400) * 1000)
    with _conn() as con:
        con.execute("DELETE FROM decision_logs WHERE ts < ?", (cutoff_ms,))


def get_utilization_history(hours=24):
    """取得近 N 小時的資金使用率快照，供歷史折線圖使用。
    hours <= 48：逐筆原始資料（小時粒度）
    hours > 48：按日聚合（每日平均），label 為 MM-DD
    """
    cutoff_ms = int((datetime.datetime.now().timestamp() - hours * 3600) * 1000)
    with _conn() as con:
        rows = con.execute(
            """
            SELECT ts, detail, available_detail, loans_detail, offers_detail, weighted_avg_rate
            FROM utilization_snapshots
            WHERE ts >= ?
            ORDER BY ts ASC
            """,
            (cutoff_ms,)
        ).fetchall()

    # --- 解析每一筆原始資料 ---
    parsed = []
    for row in rows:
        ts, detail_j, avail_j, loans_j, offers_j, w_rate = row
        detail = json.loads(detail_j) if detail_j else {}
        loans  = json.loads(loans_j)  if loans_j  else {}
        total_usd = sum(detail.values())
        lent_usd  = sum(loans.values())   # 僅計已成交放貸，與 weighted_avg_rate 一致
        util_pct  = round(lent_usd / total_usd * 100, 1) if total_usd > 0 else 0.0
        parsed.append({
            'ts': ts,
            'date': datetime.datetime.fromtimestamp(ts / 1000, tz=TZ8).strftime('%m-%d'),
            'label': datetime.datetime.fromtimestamp(ts / 1000, tz=TZ8).strftime('%m-%d %H:%M'),
            'util_pct': util_pct,
            'weighted_avg_rate': w_rate,
            'total_usd': round(total_usd, 2),
            'lent_usd': round(lent_usd, 2),
        })

    if hours <= 48:
        # 原始粒度，直接回傳
        return [{**p, 'weighted_avg_rate': round(p['weighted_avg_rate'], 4) if p['weighted_avg_rate'] is not None else None} for p in parsed]

    # 日聚合：按 date 分組，計算當日平均
    from collections import defaultdict
    groups = defaultdict(list)
    for p in parsed:
        groups[p['date']].append(p)

    result = []
    for date in sorted(groups.keys()):
        pts = groups[date]
        avg_util = round(sum(p['util_pct'] for p in pts) / len(pts), 1)
        rates = [p['weighted_avg_rate'] for p in pts if p['weighted_avg_rate'] is not None]
        avg_rate = round(sum(rates) / len(rates), 4) if rates else None
        avg_total = round(sum(p['total_usd'] for p in pts) / len(pts), 2)
        avg_lent  = round(sum(p['lent_usd']  for p in pts) / len(pts), 2)
        result.append({
            'ts': pts[-1]['ts'],
            'label': date,
            'util_pct': avg_util,
            'weighted_avg_rate': avg_rate,
            'total_usd': avg_total,
            'lent_usd': avg_lent,
        })
    return result


def save_trade_rate_snapshot(symbol, bucket_stats):
    """儲存各期數區間的成交利率快照。
    bucket_stats: {bucket_key: {'max': float, 'min': float, 'avg': float, 'count': int}}
    """
    now8 = datetime.datetime.now(tz=TZ8)
    ts = int(now8.timestamp() * 1000)
    date_str = now8.strftime('%Y-%m-%d')
    hour_str = now8.strftime('%Y-%m-%d %H:00')
    with _conn() as con:
        for bk, stats in bucket_stats.items():
            con.execute(
                """
                INSERT OR REPLACE INTO trade_rate_snapshots
                    (ts, date, hour, symbol, period_bucket, max_rate, min_rate, avg_rate, trade_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, date_str, hour_str, symbol, str(bk),
                 stats.get('max'), stats.get('min'), stats.get('avg'), stats.get('count', 0))
            )


def get_trade_rate_history(symbol, hours=None, days=None):
    """查詢利率快照歷史。
    hours: 小時粒度（逐小時）
    days: 日粒度（每日聚合）
    回傳: {bucket_key: [{'date': str, 'max_rate': float, ...}, ...]}
    """
    from collections import defaultdict
    if hours is not None:
        cutoff_ms = int((datetime.datetime.now().timestamp() - hours * 3600) * 1000)
        with _conn() as con:
            rows = con.execute(
                """
                SELECT hour, period_bucket, max_rate, min_rate, avg_rate, trade_count
                FROM trade_rate_snapshots
                WHERE symbol = ? AND ts >= ?
                ORDER BY hour ASC
                """,
                (symbol, cutoff_ms)
            ).fetchall()
        series = defaultdict(list)
        for hour, bk, max_r, min_r, avg_r, cnt in rows:
            label = hour[5:]  # 'MM-DD HH:00'
            series[bk].append({
                'date': label,
                'max_rate': max_r,
                'min_rate': min_r,
                'avg_rate': avg_r,
                'count': cnt,
            })
        return dict(series)
    elif days is not None:
        cutoff_ms = int((datetime.datetime.now().timestamp() - days * 86400) * 1000)
        with _conn() as con:
            rows = con.execute(
                """
                SELECT date, period_bucket, MAX(max_rate), MIN(min_rate),
                       ROUND(AVG(avg_rate), 4), SUM(trade_count)
                FROM trade_rate_snapshots
                WHERE symbol = ? AND ts >= ?
                GROUP BY date, period_bucket
                ORDER BY date ASC
                """,
                (symbol, cutoff_ms)
            ).fetchall()
        series = defaultdict(list)
        for date_str, bk, max_r, min_r, avg_r, cnt in rows:
            series[bk].append({
                'date': date_str,
                'max_rate': max_r,
                'min_rate': min_r,
                'avg_rate': avg_r,
                'count': cnt,
            })
        return dict(series)

