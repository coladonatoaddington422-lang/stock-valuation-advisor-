"""A-share breakout screener: Tongdaxin formula + six-factor score + walk-forward backtest.

Tushare token is read from TUSHARE_TOKEN. Data is cached in SQLite.
The Tongdaxin SMA/EMA recurrences are implemented explicitly for reproducibility.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
from zoneinfo import ZoneInfo
import time
import threading
from datetime import datetime, timedelta
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import tushare as ts
try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter()
ROOT = Path(__file__).parent
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = Path(os.getenv("SCREENER_DB_PATH", str(ROOT / "data" / "screener.sqlite3")))
if not DATABASE_URL:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
CACHE: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 180
SHANGHAI = ZoneInfo("Asia/Shanghai")


class _DBConnection:
    """Small compatibility layer: existing qmark SQL works with SQLite and psycopg."""
    def __init__(self, raw, postgres=False):
        self.raw = raw
        self.postgres = postgres

    def _sql(self, sql):
        return sql.replace("?", "%s") if self.postgres else sql

    def execute(self, sql, params=()):
        return self.raw.execute(self._sql(sql), params)

    def executemany(self, sql, params):
        return self.raw.executemany(self._sql(sql), params)


@contextmanager
def _db():
    if DATABASE_URL:
        if psycopg is None:
            raise RuntimeError("已配置 DATABASE_URL，但未安装 psycopg[binary]。")
        con = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=15)
        wrapped = _DBConnection(con, postgres=True)
    else:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        wrapped = _DBConnection(con)
    try:
        yield wrapped
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _read_df(sql, params=()):
    with _db() as con:
        rows = con.execute(sql, params).fetchall()
    return pd.DataFrame([dict(row) for row in rows]) if rows else pd.DataFrame()


def _query_all(pro, method_name: str, **kwargs):
    """Paginate Tushare endpoints whose default response limit may truncate the A-share universe."""
    method = getattr(pro, method_name)
    frames = []
    offset = 0
    page_size = 5000
    while True:
        page = method(**kwargs, limit=page_size, offset=offset)
        if page is None or page.empty:
            break
        frames.append(page)
        if len(page) < page_size:
            break
        offset += page_size
        if offset >= 20000:
            break
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _init_db():
    with _db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS daily (
            ts_code TEXT NOT NULL, trade_date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, pre_close REAL,
            pct_chg REAL, vol REAL, amount REAL,
            turnover_rate REAL, volume_ratio REAL, pe_ttm REAL, pb REAL,
            total_mv REAL, circ_mv REAL,
            PRIMARY KEY(ts_code, trade_date))""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(trade_date)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_daily_code_date ON daily(ts_code, trade_date)")
        con.execute("""CREATE TABLE IF NOT EXISTS trade_date_cache (
            id INTEGER PRIMARY KEY CHECK(id=1), dates_json TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS scan_cache (
            id INTEGER PRIMARY KEY CHECK(id=1), created_at TEXT, payload TEXT)""")


_init_db()


def _pro():
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        raise HTTPException(503, "尚未配置 TUSHARE_TOKEN。请在 Render 环境变量中添加你的 Tushare Token。")
    ts.set_token(token)
    return ts.pro_api()


def _num(v):
    try:
        if pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _trade_dates(pro, days=750):
    """Load the trading-session calendar from Neon first; call Tushare daily only once."""
    with _db() as con:
        cached = con.execute("SELECT dates_json FROM trade_date_cache WHERE id=1").fetchone()
    if cached:
        try:
            saved = json.loads(cached["dates_json"])
            if isinstance(saved, list) and len(saved) >= min(days, 500):
                return saved[-days:]
        except Exception:
            pass

    end_dt = datetime.now(SHANGHAI)
    end = end_dt.strftime("%Y%m%d")
    start = (end_dt - timedelta(days=max(1100, days * 2))).strftime("%Y%m%d")
    try:
        bars = pro.daily(ts_code="000001.SZ", start_date=start, end_date=end,
                         fields="trade_date", limit=6000)
    except Exception as exc:
        raise RuntimeError(f"获取交易日期失败（Tushare daily，000001.SZ）：{str(exc)[:500]}") from exc
    if bars is None or bars.empty or "trade_date" not in bars.columns:
        raise RuntimeError("获取交易日期失败：Tushare daily 返回空数据或缺少 trade_date 字段。")
    dates = sorted(bars["trade_date"].astype(str).unique().tolist())[-days:]
    if len(dates) < min(days, 500):
        raise RuntimeError(f"获取交易日期数量不足：需要约{days}天，实际仅取得{len(dates)}天。")
    with _db() as con:
        con.execute("""INSERT INTO trade_date_cache(id, dates_json, updated_at)
            VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET
            dates_json=excluded.dates_json, updated_at=excluded.updated_at""",
            (json.dumps(dates, ensure_ascii=False), datetime.now(SHANGHAI).isoformat(timespec="seconds")))
    return dates


def _stock_meta(pro):
    # stock_basic is not included in the 120-point daily-only tier.
    # Build the universe from cached daily bars; use code as a safe display-name fallback.
    with _db() as con:
        rows = con.execute("SELECT DISTINCT ts_code FROM daily").fetchall()
    codes = [r["ts_code"] for r in rows if str(r["ts_code"]).endswith((".SH", ".SZ", ".BJ"))]
    if not codes:
        raise HTTPException(409, "行情缓存中没有股票代码，请先完成历史行情初始化。")
    return pd.DataFrame([{"ts_code": code, "name": code.split(".")[0],
                          "industry": "", "market": code.split(".")[-1]} for code in codes])


@router.get("/api/screener/status")
def screener_status():
    token_set = bool(os.getenv("TUSHARE_TOKEN", "").strip())
    with _db() as con:
        dates = con.execute("SELECT COUNT(DISTINCT trade_date) n, MAX(trade_date) latest FROM daily").fetchone()
        stocks = con.execute("SELECT COUNT(DISTINCT ts_code) n FROM daily").fetchone()
    return {
        "ok": True, "token_configured": token_set,
        "cached_trade_dates": dates["n"], "latest_trade_date": dates["latest"],
        "cached_stocks": stocks["n"], "database": "Neon PostgreSQL" if DATABASE_URL else "本地 SQLite",
        "required_history_days": 500, "target_history_days": 750,
        "note": "daily_basic 权限不足时仍可计算价格类指标；估值/基本面代理分会显示数据缺失。Neon PostgreSQL 用于持久化行情和任务进度；免费 Web 实例休眠或重启时，正在执行的后台线程可能中断，重新启动任务可续跑。"
    }


@router.post("/api/screener/backfill")
def screener_backfill(body: dict):
    """Fetch a small batch of recent market-wide trading dates. Repeat until remaining=0."""
    pro = _pro()
    batch = max(1, min(int(body.get("batch", 20)), 40))
    dates = _trade_dates(pro, 750)
    with _db() as con:
        existing = {r["trade_date"] for r in con.execute("SELECT DISTINCT trade_date FROM daily")}
    missing = [d for d in dates if d not in existing]
    target = missing[-batch:]
    if not target:
        return {"ok": True, "fetched": 0, "remaining": 0, "message": "最近750个交易日的日线缓存已齐备。"}
    fetched, rows_saved, daily_basic_ok, errors = 0, 0, 0, []
    for day in target:
        try:
            d = _query_all(pro, "daily", trade_date=day)
            if d is None or d.empty:
                errors.append({"date": day, "error": "daily无数据"})
                continue
            # 120-point tier does not include daily_basic. Skip it rather than
            # issuing a permission-failing request for every historical trading day.
            for col in ("turnover_rate", "volume_ratio", "pe_ttm", "pb", "total_mv", "circ_mv"):
                if col not in d.columns:
                    d[col] = np.nan
            cols = ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
                    "pct_chg", "vol", "amount", "turnover_rate", "volume_ratio",
                    "pe_ttm", "pb", "total_mv", "circ_mv"]
            d = d[cols].replace({np.nan: None})
            with _db() as con:
                con.executemany("""INSERT INTO daily
                    (ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount,
                     turnover_rate,volume_ratio,pe_ttm,pb,total_mv,circ_mv)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT (ts_code,trade_date) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                    pre_close=excluded.pre_close, pct_chg=excluded.pct_chg, vol=excluded.vol,
                    amount=excluded.amount, turnover_rate=excluded.turnover_rate,
                    volume_ratio=excluded.volume_ratio, pe_ttm=excluded.pe_ttm, pb=excluded.pb,
                    total_mv=excluded.total_mv, circ_mv=excluded.circ_mv""",
                    [tuple(row) for row in d.itertuples(index=False, name=None)])
            fetched += 1
            rows_saved += len(d)
            time.sleep(0.12)
        except Exception as e:
            errors.append({"date": day, "error": str(e)[:220]})
    with _db() as con:
        remaining = sum(1 for d in dates if con.execute("SELECT 1 FROM daily WHERE trade_date=? LIMIT 1", (d,)).fetchone() is None)
    CACHE.clear()
    return {"ok": True, "fetched": fetched, "rows_saved": rows_saved,
            "daily_basic_days_available": daily_basic_ok, "remaining": remaining,
            "errors": errors[:8], "message": "继续点击初始化，直到剩余交易日为0；若 daily_basic 权限不足，价格信号仍可计算。"} 


def _ema(s: pd.Series, n: int) -> pd.Series:
    # Tongdaxin EMA: Y=(2*X+(N-1)*Y')/(N+1), seeded with first valid value.
    return s.ewm(span=n, adjust=False, min_periods=1).mean()


def _sma_tdx(s: pd.Series, n: int, m: int) -> pd.Series:
    # Tongdaxin SMA(X,N,M): Y=(M*X+(N-M)*Y')/N.
    vals = s.astype(float).to_numpy()
    out = np.full(len(vals), np.nan, dtype=float)
    prev = np.nan
    for i, x in enumerate(vals):
        if np.isnan(x):
            continue
        prev = x if np.isnan(prev) else (m * x + (n - m) * prev) / n
        out[i] = prev
    return pd.Series(out, index=s.index)


def calc_original_formula(df: pd.DataFrame) -> pd.DataFrame:
    """Translate the supplied Tongdaxin formula, retaining the current-bar LLV condition."""
    x = df.sort_values("trade_date").copy()
    h, l, c = x["high"].astype(float), x["low"].astype(float), x["close"].astype(float)
    v1 = _ema(h.rolling(500, min_periods=500).max(), 21)
    v2 = _ema(h.rolling(250, min_periods=250).max(), 21)
    v3 = _ema(h.rolling(90, min_periods=90).max(), 21)
    v4 = _ema(l.rolling(500, min_periods=500).min(), 21)
    v5 = _ema(l.rolling(250, min_periods=250).min(), 21)
    v6 = _ema(l.rolling(90, min_periods=90).min(), 21)
    v7 = _ema((v4*.96 + v5*.96 + v6*.96 + v1*.558 + v2*.558 + v3*.558)/6, 21)
    v8 = _ema((v4*1.25 + v5*1.23 + v6*1.2 + v1*.55 + v2*.55 + v3*.65)/6, 21)
    v9 = _ema((v4*1.3 + v5*1.3 + v6*1.3 + v1*.68 + v2*.68 + v3*.68)/6, 21)
    va = _ema((v7*3 + v8*2 + v9)/6 * 1.738, 21)
    prev_low = l.shift(1)
    delta = l - prev_low
    vc_num = _sma_tdx((l-prev_low).abs(), 3, 1)
    vc_den = _sma_tdx(delta.clip(lower=0), 3, 1)
    vc = (vc_num / vc_den.replace(0, np.nan) * 100).replace([np.inf, -np.inf], np.nan).fillna(0)
    vd_input = pd.Series(np.where(c * 1.35 <= va, vc * 10, vc / 10), index=x.index)
    vd = _ema(vd_input, 3)
    ve = l.rolling(30, min_periods=30).min()
    vf = vd.rolling(30, min_periods=30).max()
    ma58 = c.rolling(58, min_periods=58).mean()
    var10 = (ma58 > 0).astype(float)
    raw = pd.Series(np.where(l <= ve, (vd + vf * 2)/2, 0.0), index=x.index)
    capital = _ema(raw, 3) / 618 * var10
    x["formula_capital"] = capital
    x["formula_signal"] = (capital > 0) & capital.notna()
    x["var_a"] = va
    x["var_d"] = vd
    return x


def _pct(a, b):
    if b is None or pd.isna(b) or b == 0 or pd.isna(a):
        return None
    return float((a / b - 1) * 100)


def _score_one(g: pd.DataFrame, market_return_20: float | None):
    g = calc_original_formula(g)
    r = g.iloc[-1]
    if len(g) < 500 or pd.isna(r["formula_capital"]):
        return None
    close = g["close"].astype(float)
    high = g["high"].astype(float)
    low = g["low"].astype(float)
    vol = g["vol"].astype(float)
    amount = g["amount"].astype(float)
    ma5 = close.rolling(5).mean().iloc[-1]
    ma20s = close.rolling(20).mean()
    ma60s = close.rolling(60).mean()
    ma20, ma60 = ma20s.iloc[-1], ma60s.iloc[-1]
    ma20_prev = ma20s.iloc[-6] if len(ma20s) >= 6 else np.nan
    vol5, vol20 = vol.rolling(5).mean().iloc[-1], vol.iloc[-21:-1].mean()
    amount5 = amount.iloc[-6:-1].mean()
    ret5 = _pct(close.iloc[-1], close.iloc[-6]) if len(close) >= 6 else None
    ret20 = _pct(close.iloc[-1], close.iloc[-21]) if len(close) >= 21 else None
    ret60 = _pct(close.iloc[-1], close.iloc[-61]) if len(close) >= 61 else None
    high20_prev = high.iloc[-21:-1].max()
    high60 = high.iloc[-60:].max()
    low60 = low.iloc[-60:].min()
    range20 = (high.iloc[-20:].max() - low.iloc[-20:].min()) / max(close.iloc[-1], 1e-9)
    range60 = (high.iloc[-60:].max() - low.iloc[-60:].min()) / max(close.iloc[-1], 1e-9)
    vol_ratio = float(vol.iloc[-1] / vol20) if vol20 and not pd.isna(vol20) else None
    amount_ratio = float(amount.iloc[-1] / amount5) if amount5 and not pd.isna(amount5) else None
    formula_now = bool(r["formula_signal"])
    formula_recent = bool(g["formula_signal"].tail(5).any())

    # 1) Formula/capital activity: max 20.
    capital_score = 12 if formula_now else (8 if formula_recent else 0)
    if amount_ratio is not None and amount_ratio >= 1.2 and float(r["pct_chg"]) > 0:
        capital_score += 4
    if ret5 is not None and ret5 > 0:
        capital_score += 2
    if formula_now and float(r["formula_capital"]) > float(g["formula_capital"].tail(60).quantile(.7)):
        capital_score += 2
    capital_score = min(20, capital_score)

    # 2) Trend structure: max 20.
    trend = 0
    if close.iloc[-1] > ma20: trend += 7
    if ma20 > ma60: trend += 6
    if ma20_prev and not pd.isna(ma20_prev) and ma20 > ma20_prev: trend += 5
    if ret60 is not None and ret60 > -5: trend += 2

    # 3) Volume quality: max 15.
    volume_score = 0
    if vol_ratio is not None and 1.2 <= vol_ratio <= 3.5: volume_score += 6
    if amount_ratio is not None and 1.2 <= amount_ratio <= 3.5: volume_score += 4
    if vol_ratio is not None and vol_ratio < 0.8 and ret5 is not None and ret5 < 0: volume_score += 3
    if r.get("turnover_rate") is not None and not pd.isna(r.get("turnover_rate")):
        tr = float(r["turnover_rate"])
        if 1 <= tr <= 12: volume_score += 2

    # 4) Pressure/base structure: max 15.
    pressure = 0
    distance_to_high = _pct(high60, close.iloc[-1])
    if distance_to_high is not None and 0 <= distance_to_high <= 8: pressure += 6
    if range60 > 0 and range20 / range60 < .55: pressure += 5
    if close.iloc[-1] >= high20_prev: pressure += 4

    # 5) Relative strength: cross-sectional market excess return proxy, max 15.
    excess = (ret20 - market_return_20) if ret20 is not None and market_return_20 is not None else None
    relative = 0
    if excess is not None:
        if excess >= 10: relative = 15
        elif excess >= 5: relative = 12
        elif excess >= 2: relative = 9
        elif excess >= 0: relative = 6
        elif excess >= -5: relative = 3

    # 6) Fundamental/valuation risk proxy: max 15. Not a substitute for financial statements.
    fundamental = 7  # neutral base when fields are unavailable
    reasons = ["财务质量尚未接入逐家公司报表，当前为估值/风险代理分，不代表完整基本面判断"]
    pe, pb = _num(r.get("pe_ttm")), _num(r.get("pb"))
    if pe is not None and pe > 0:
        fundamental += 3
        reasons.append("PE(TTM)为正")
    if pb is not None and 0 < pb <= 5:
        fundamental += 3
        reasons.append("PB不高于5")
    total_mv = _num(r.get("total_mv"))
    if total_mv is not None and total_mv >= 500000:  # Tushare total_mv unit: 10k CNY; threshold ~= RMB 5bn
        fundamental += 2
        reasons.append("总市值代理不低于50亿元")
    fundamental = min(15, fundamental)

    total = int(round(capital_score + trend + volume_score + pressure + relative + fundamental))
    # Three-stage state machine: confirmed breakout > preparation > watch/potential.
    breakout = (close.iloc[-1] > high20_prev and vol_ratio is not None and vol_ratio >= 1.5
                and float(r["pct_chg"]) > 0 and close.iloc[-1] >= (low.iloc[-1] + .7*(high.iloc[-1]-low.iloc[-1])))
    prep = (formula_recent and close.iloc[-1] > ma20 and ma20 >= ma20_prev
            and distance_to_high is not None and distance_to_high <= 12
            and (vol_ratio is None or vol_ratio <= 2.8))
    if breakout:
        stage = "突破确认"
        stage_note = "收盘突破前20日高点，且放量上涨；仍需防范次日回落和假突破。"
    elif prep:
        stage = "启动准备"
        stage_note = "近期出现公式信号，趋势转强并接近阶段压力位；等待有效突破。"
    elif formula_now or formula_recent:
        stage = "潜伏观察"
        stage_note = "出现公式资金异动，但趋势或突破条件尚未充分确认。"
    else:
        stage = "普通观察"
        stage_note = "未出现近期资金公式信号，不列入优先候选。"
    return {
        "formula_signal": formula_now, "formula_recent": formula_recent,
        "formula_value": round(float(r["formula_capital"]), 5),
        "score": total, "capital_score": int(capital_score), "trend_score": int(trend),
        "volume_score": int(volume_score), "pressure_score": int(pressure),
        "relative_score": int(relative), "fundamental_score": int(fundamental),
        "stage": stage, "stage_note": stage_note,
        "price": round(float(r["close"]), 2), "pct_chg": round(float(r["pct_chg"]), 2),
        "trade_date": str(r["trade_date"]), "ret5": round(ret5, 2) if ret5 is not None else None,
        "ret20": round(ret20, 2) if ret20 is not None else None,
        "ret60": round(ret60, 2) if ret60 is not None else None,
        "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
        "amount_ratio": round(amount_ratio, 2) if amount_ratio is not None else None,
        "distance_to_60d_high": round(distance_to_high, 2) if distance_to_high is not None else None,
        "pe_ttm": pe, "pb": pb, "fundamental_notes": reasons,
        "return_excess_20d": round(excess, 2) if excess is not None else None
    }


def _cached_universe(min_rows=500):
    """Return eligible codes and a small recent-date slice; avoid loading millions of rows into RAM."""
    with _db() as con:
        latest_row = con.execute("SELECT MAX(trade_date) latest FROM daily").fetchone()
        latest_date = latest_row["latest"]
        if not latest_date:
            raise HTTPException(409, "本地还没有行情缓存。请先点击“初始化行情”，分批拉取最近520个交易日。")
        codes = [r["ts_code"] for r in con.execute(
            "SELECT ts_code FROM daily GROUP BY ts_code HAVING COUNT(*)>=? AND MAX(trade_date)=?",
            (min_rows, latest_date))]
        date_rows = con.execute(
            "SELECT DISTINCT trade_date FROM daily ORDER BY trade_date DESC LIMIT 21").fetchall()
        recent_dates = [r["trade_date"] for r in date_rows]
        placeholders = ",".join("?" for _ in recent_dates)
        recent = _read_df(
            f"SELECT ts_code,trade_date,close FROM daily WHERE trade_date IN ({placeholders})",
            tuple(recent_dates)) if recent_dates else pd.DataFrame()
    return latest_date, codes, recent, recent_dates


def _load_stock(code):
    return _read_df("SELECT * FROM daily WHERE ts_code=? ORDER BY trade_date", (code,))


@router.post("/api/screener/scan")
def screener_scan(body: dict = {}):
    force = bool(body.get("force", False))
    if not force and "latest" in CACHE and time.time() - CACHE["latest"][0] < CACHE_TTL:
        return CACHE["latest"][1]
    latest_date, codes, recent, recent_dates = _cached_universe(500)
    meta = _stock_meta(_pro()).set_index("ts_code")
    codes = [code for code in codes if code in meta.index]
    returns = []
    if len(recent_dates) >= 21 and not recent.empty:
        pivot = recent.pivot_table(index="ts_code", columns="trade_date", values="close", aggfunc="last")
        newest, oldest = recent_dates[0], recent_dates[-1]
        if newest in pivot.columns and oldest in pivot.columns:
            rr = (pivot[newest] / pivot[oldest] - 1) * 100
            returns = rr.replace([np.inf, -np.inf], np.nan).dropna().tolist()
    market_ret = float(np.mean(returns)) if returns else None
    rows = []
    for code in codes:
        g = _load_stock(code)
        if g.empty or str(g["trade_date"].iloc[-1]) != str(latest_date):
            continue
        try:
            result = _score_one(g, market_ret)
            if result is None: continue
            info = meta.loc[code]
            result.update({"ts_code": code, "name": str(info["name"]),
                           "industry": "" if pd.isna(info.get("industry")) else str(info.get("industry")),
                           "market": "" if pd.isna(info.get("market")) else str(info.get("market"))})
            rows.append(result)
        except Exception:
            continue
    rows.sort(key=lambda z: (z["score"], z["formula_signal"], z["ret20"] if z["ret20"] is not None else -999), reverse=True)
    min_score = max(0, min(int(body.get("min_score", 70)), 100))
    candidates = [x for x in rows if x["score"] >= min_score and (x["formula_recent"] or x["stage"] == "突破确认")]
    result = {"ok": True, "trade_date": latest_date, "universe_scanned": len(rows),
              "minimum_score": min_score, "candidate_count": len(candidates), "market_return_20d_proxy": round(market_ret, 2) if market_ret is not None else None,
              "score_weights": {"资金公式":20,"趋势结构":20,"成交量":15,"筹码压力":15,"相对强度":15,"基本面风险代理":15},
              "fundamental_disclaimer": "基本面维度目前使用PE/PB和上市风险代理，未接入完整财报质量评分；请勿将总分视为盈利概率。",
              "results": candidates[:20], "all_results_count": len(rows)}
    with _db() as con:
        con.execute("""INSERT INTO scan_cache(id,created_at,payload) VALUES(1,?,?)
                    ON CONFLICT (id) DO UPDATE SET
                    created_at=excluded.created_at, payload=excluded.payload""",
                    (datetime.now(SHANGHAI).isoformat(timespec="seconds"), json.dumps(result, ensure_ascii=False)))
    CACHE["latest"] = (time.time(), result)
    return result


@router.post("/api/screener/backtest")
def screener_backtest(body: dict):
    with _db() as con:
        codes = [r["ts_code"] for r in con.execute(
            "SELECT ts_code FROM daily GROUP BY ts_code HAVING COUNT(*)>=520")]
    start = str(body.get("start_date", ""))  # YYYYMMDD optional
    end = str(body.get("end_date", ""))      # YYYYMMDD optional
    horizon = max(5, min(int(body.get("horizon", 10)), 20))
    min_score = max(0, min(int(body.get("min_score", 70)), 100))
    records = []
    for code in codes:
        g = _load_stock(code).sort_values("trade_date").reset_index(drop=True)
        if len(g) < 520:
            continue
        g = calc_original_formula(g)
        close = g["close"].astype(float).to_numpy()
        high = g["high"].astype(float).to_numpy()
        low = g["low"].astype(float).to_numpy()
        open_ = g["open"].astype(float).to_numpy()
        dates = g["trade_date"].astype(str).to_numpy()
        # Use only historical rows with a full 500-bar lookback and enough future bars.
        last_signal = -999
        for i in range(500, len(g)-horizon):
            if start and dates[i] < start: continue
            if end and dates[i] > end: continue
            if not bool(g["formula_signal"].iloc[i]): continue
            if i - last_signal < 20: continue  # avoid repeated overlapping signals
            hist = g.iloc[:i+1]
            # Point-in-time six-dimension approximation; all features use data up to signal day i.
            c_hist = hist["close"].astype(float)
            h_hist = hist["high"].astype(float)
            l_hist = hist["low"].astype(float)
            v_hist = hist["vol"].astype(float)
            ma20 = c_hist.tail(20).mean()
            ma60 = c_hist.tail(60).mean()
            ma20_prev = c_hist.iloc[-25:-5].mean() if len(c_hist) >= 25 else ma20
            vol20 = v_hist.iloc[-21:-1].mean()
            vol_ratio = float(v_hist.iloc[-1] / vol20) if vol20 else 0
            amount5 = hist["amount"].iloc[-6:-1].mean()
            amount_ratio = float(hist["amount"].iloc[-1] / amount5) if amount5 else 0
            ret20 = (close[i] / close[i-20] - 1) * 100 if close[i-20] else 0
            ret5 = (close[i] / close[i-5] - 1) * 100 if close[i-5] else 0
            # Capital formula score, max 20.
            formula_recent = bool(hist["formula_signal"].tail(5).any())
            capital_score = 12
            if amount_ratio >= 1.2 and ret5 > 0: capital_score += 4
            if ret5 > 0: capital_score += 2
            if float(hist["formula_capital"].iloc[-1]) > float(hist["formula_capital"].tail(60).quantile(.7)): capital_score += 2
            capital_score = min(20, capital_score)
            trend_score = (7 if close[i] > ma20 else 0) + (6 if ma20 > ma60 else 0) + (5 if ma20 >= ma20_prev else 0) + (2 if ret20 > -5 else 0)
            volume_score = (6 if 1.2 <= vol_ratio <= 3.5 else 0) + (4 if 1.2 <= amount_ratio <= 3.5 else 0)
            if vol_ratio < .8 and ret5 < 0: volume_score += 3
            high20_prev = h_hist.iloc[-21:-1].max()
            high60 = h_hist.tail(60).max()
            low60 = l_hist.tail(60).min()
            range20 = (h_hist.tail(20).max() - l_hist.tail(20).min()) / max(close[i], 1e-9)
            range60 = (high60 - low60) / max(close[i], 1e-9)
            pressure_score = (6 if 0 <= (high60/close[i]-1)*100 <= 8 else 0)
            pressure_score += 5 if range60 > 0 and range20/range60 < .55 else 0
            pressure_score += 4 if close[i] >= high20_prev else 0
            relative_score = 15 if ret20 >= 10 else 12 if ret20 >= 5 else 9 if ret20 >= 2 else 6 if ret20 >= 0 else 3 if ret20 >= -5 else 0
            fundamental_score = 7  # neutral placeholder: no point-in-time statements in this backtest cache
            score = int(round(capital_score + trend_score + volume_score + pressure_score + relative_score + fundamental_score))
            if score < min_score: continue
            stage = "突破确认" if close[i] > high20_prev and vol_ratio >= 1.5 and float(hist["pct_chg"].iloc[-1]) > 0 else "启动准备" if formula_recent and close[i] > ma20 and ma20 >= ma20_prev else "潜伏观察"
            entry = open_[i+1]  # signal is known after close; theoretical entry is next session open
            future_close = close[i+1:i+horizon+1]
            future_high = high[i+1:i+horizon+1]
            future_low = low[i+1:i+horizon+1]
            if entry <= 0 or len(future_close) < horizon: continue
            records.append({"ts_code": code, "signal_date": dates[i], "score_proxy": score, "stage": stage,
                            "horizon_return_pct": (future_close[-1]/entry-1)*100,
                            "max_runup_pct": (np.max(future_high)/entry-1)*100,
                            "max_drawdown_pct": (np.min(future_low)/entry-1)*100})
            last_signal = i
    if not records:
        return {"ok": True, "count": 0, "message": "所选区间没有满足条件的历史信号。请先完成行情初始化，或调整日期/最低评分。"}
    r = pd.DataFrame(records)
    win = r["horizon_return_pct"] > 0
    result = {"ok": True, "count": len(r), "horizon": horizon, "min_score": min_score,
              "win_rate_pct": round(float(win.mean()*100), 2),
              "avg_return_pct": round(float(r["horizon_return_pct"].mean()), 2),
              "median_return_pct": round(float(r["horizon_return_pct"].median()), 2),
              "avg_max_runup_pct": round(float(r["max_runup_pct"].mean()), 2),
              "avg_max_drawdown_pct": round(float(r["max_drawdown_pct"].mean()), 2),
              "stage_summary": r.groupby("stage")["horizon_return_pct"].agg(["count","mean"]).round(2).reset_index().to_dict("records"),
              "best_20": r.sort_values("horizon_return_pct", ascending=False).head(20).replace({np.nan: None}).to_dict("records"),
              "caveat": "这是按历史收盘信号、下一交易日开盘理论买入的简化统计；未扣除手续费、滑点、涨跌停无法成交影响，且当前缓存/股票池可能存在幸存者偏差。回测使用点时六维近似评分，基本面分为中性占位，相对强度以个股收益代理，和实时评分仍有差异。"}
    return result


@router.get("/api/screener/export")
def screener_export():
    with _db() as con:
        row = con.execute("SELECT payload,created_at FROM scan_cache WHERE id=1").fetchone()
    if not row:
        raise HTTPException(409, "请先运行一次全市场扫描，再导出结果。")
    data = json.loads(row["payload"])
    df = pd.DataFrame(data.get("results", []))
    if df.empty:
        raise HTTPException(404, "当前没有候选股可导出。")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="候选股票")
        pd.DataFrame([{"扫描日期": data.get("trade_date"), "扫描股票数": data.get("universe_scanned"),
                       "候选数量": data.get("candidate_count"), "评分说明": data.get("fundamental_disclaimer")}]).to_excel(
                           writer, index=False, sheet_name="说明")
    buf.seek(0)
    filename = f"stock_screener_{data.get('trade_date','latest')}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------- Automatic initialization + scan runner ----------------
# Progress is persisted in the configured database. A process restart interrupts
# the in-process worker; the next manual start resumes from idempotent cached rows.
_AUTO_LOCK = threading.Lock()
_AUTO_THREAD = None

def _auto_job_init():
    with _db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS auto_job (
            id INTEGER PRIMARY KEY CHECK(id=1), state TEXT, phase TEXT,
            batches_done INTEGER DEFAULT 0, fetched_days INTEGER DEFAULT 0,
            rows_saved INTEGER DEFAULT 0, remaining INTEGER,
            started_at TEXT, updated_at TEXT, message TEXT, result_json TEXT
        )""")

_auto_job_init()
with _db() as _con:
    _con.execute("""UPDATE auto_job SET state='interrupted', phase='interrupted',
                    message='服务进程曾重启；已保存的行情不会丢失，重新点击自动初始化即可续跑。'
                    WHERE id=1 AND state='running'""")

def _auto_job_update(**values):
    allowed = {"state","phase","batches_done","fetched_days","rows_saved",
               "remaining","started_at","updated_at","message","result_json"}
    values = {k:v for k,v in values.items() if k in allowed}
    values["updated_at"] = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    with _db() as con:
        row = con.execute("SELECT id FROM auto_job WHERE id=1").fetchone()
        if row:
            assignments = ", ".join(f"{k}=?" for k in values)
            con.execute(f"UPDATE auto_job SET {assignments} WHERE id=1", tuple(values.values()))
        else:
            cols = ", ".join(["id"] + list(values.keys()))
            marks = ", ".join(["?"] * (len(values)+1))
            con.execute(f"INSERT INTO auto_job ({cols}) VALUES ({marks})", tuple([1]+list(values.values())))

def _auto_job_read():
    with _db() as con:
        row = con.execute("SELECT * FROM auto_job WHERE id=1").fetchone()
    if not row:
        return {"state":"idle","phase":"idle","batches_done":0,"fetched_days":0,
                "rows_saved":0,"remaining":None,"message":"尚未启动自动任务。"}
    return dict(row)

def _auto_runner():
    try:
        _auto_job_update(state="running", phase="backfill", message="正在分批下载最近750个交易日行情。")
        while True:
            result = screener_backfill({"batch":20})
            job = _auto_job_read()
            _auto_job_update(
                state="running", phase="backfill",
                batches_done=int(job.get("batches_done") or 0)+1,
                fetched_days=int(job.get("fetched_days") or 0)+int(result.get("fetched") or 0),
                rows_saved=int(job.get("rows_saved") or 0)+int(result.get("rows_saved") or 0),
                remaining=int(result.get("remaining") or 0),
                message=result.get("message") or "行情批次已完成。"
            )
            if int(result.get("remaining") or 0) == 0:
                break
            if int(result.get("fetched") or 0) == 0:
                detail = result.get("errors") or []
                raise RuntimeError("本批没有成功写入行情；具体接口错误：" +
                                   json.dumps(detail[:8], ensure_ascii=False, default=str)[:1800])
        _auto_job_update(state="running", phase="scan", message="历史行情已齐备，正在执行全市场扫描。")
        scan_result = screener_scan({"force": True})
        _auto_job_update(state="completed", phase="completed",
                         remaining=0, message="行情初始化和全市场扫描均已完成。",
                         result_json=json.dumps(scan_result, ensure_ascii=False, default=str))
    except Exception as exc:
        _auto_job_update(state="failed", phase="failed", message=str(exc)[:1000])
    finally:
        with _AUTO_LOCK:
            global _AUTO_THREAD
            _AUTO_THREAD = None

@router.post("/api/screener/auto-run")
def screener_auto_run():
    global _AUTO_THREAD
    if not os.getenv("TUSHARE_TOKEN", "").strip():
        raise HTTPException(503, "未检测到 TUSHARE_TOKEN，请先在 Render 服务 Environment 中配置。")
    with _AUTO_LOCK:
        job = _auto_job_read()
        if job.get("state") == "running" and _AUTO_THREAD is not None and _AUTO_THREAD.is_alive():
            return {"ok":True,"started":False,"job":job,"message":"自动任务已经在运行。"}
        if job.get("state") not in ("interrupted", "failed", "running"):
            _auto_job_update(state="running", phase="starting", batches_done=0,
                             fetched_days=0, rows_saved=0, remaining=750,
                             started_at=datetime.now(SHANGHAI).isoformat(timespec="seconds"),
                             message="自动任务已启动。")
        else:
            _auto_job_update(state="running", phase="starting",
                             message="正在从数据库中已保存的行情进度继续执行。")
        _AUTO_THREAD = threading.Thread(target=_auto_runner, daemon=True, name="screener-auto-runner")
        _AUTO_THREAD.start()
    return {"ok":True,"started":True,"message":"自动初始化与扫描任务已启动，可通过 /api/screener/auto-status 查看进度。"}

@router.get("/api/screener/auto-status")
def screener_auto_status():
    job = _auto_job_read()
    result = None
    if job.get("result_json"):
        try:
            result = json.loads(job["result_json"])
        except Exception:
            result = None
    job["result"] = result
    return {"ok":True,"job":job}
