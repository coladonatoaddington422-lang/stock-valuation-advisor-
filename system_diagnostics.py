"""Runtime diagnostics for Tushare permissions and Neon read/write connectivity."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from uuid import uuid4
import os

from fastapi import APIRouter
import tushare as ts

from screener import _db, DATABASE_URL

router = APIRouter()
SH = ZoneInfo("Asia/Shanghai")


@router.get("/api/system/self-check")
def system_self_check():
    checks = {}
    # Verify actual database write, read, and delete in one committed transaction.
    try:
        marker = str(uuid4())
        with _db() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS system_self_check (
                check_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL
            )""")
            now = datetime.now(SH).isoformat(timespec="seconds")
            con.execute("INSERT INTO system_self_check(check_id, checked_at) VALUES (?, ?)", (marker, now))
            row = con.execute("SELECT check_id, checked_at FROM system_self_check WHERE check_id=?", (marker,)).fetchone()
            if not row or row["check_id"] != marker:
                raise RuntimeError("写入后读取结果不一致")
            con.execute("DELETE FROM system_self_check WHERE check_id=?", (marker,))
        checks["database"] = {"ok": True, "backend": "Neon PostgreSQL" if DATABASE_URL else "SQLite", "write": True, "read": True, "delete": True}
    except Exception as exc:
        checks["database"] = {"ok": False, "backend": "Neon PostgreSQL" if DATABASE_URL else "SQLite", "error": str(exc)[:300]}

    token = os.getenv("TUSHARE_TOKEN", "").strip()
    checks["token_configured"] = bool(token)
    checks["tushare"] = {"ok": False, "trade_cal": False, "stock_basic": False, "daily": False, "daily_basic": False}
    if token:
        try:
            ts.set_token(token)
            pro = ts.pro_api()
            today = datetime.now(SH)
            start = (today - timedelta(days=20)).strftime("%Y%m%d")
            end = today.strftime("%Y%m%d")
            cal = pro.trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1", fields="cal_date,is_open")
            if cal is None or cal.empty:
                raise RuntimeError("trade_cal 未返回交易日")
            checks["tushare"]["trade_cal"] = True
            day = str(cal.sort_values("cal_date").iloc[-1]["cal_date"])
            try:
                stocks = pro.stock_basic(exchange="", list_status="L", fields="ts_code", limit=1)
                checks["tushare"]["stock_basic"] = stocks is not None and not stocks.empty
            except Exception as exc:
                checks["tushare"]["stock_basic_error"] = str(exc)[:220]
            try:
                daily = pro.daily(trade_date=day, fields="ts_code,trade_date,close", limit=1)
                checks["tushare"]["daily"] = daily is not None and not daily.empty
            except Exception as exc:
                checks["tushare"]["daily_error"] = str(exc)[:220]
            try:
                basic = pro.daily_basic(trade_date=day, fields="ts_code,trade_date,pe_ttm,pb", limit=1)
                checks["tushare"]["daily_basic"] = basic is not None and not basic.empty
            except Exception as exc:
                checks["tushare"]["daily_basic_error"] = str(exc)[:220]
            checks["tushare"]["tested_trade_date"] = day
            checks["tushare"]["ok"] = checks["tushare"]["trade_cal"] and checks["tushare"]["stock_basic"] and checks["tushare"]["daily"]
        except Exception as exc:
            checks["tushare"]["error"] = str(exc)[:300]
    else:
        checks["tushare"]["error"] = "Render 环境变量 TUSHARE_TOKEN 未配置"

    try:
        with _db() as con:
            dates = con.execute("SELECT COUNT(DISTINCT trade_date) AS n, MAX(trade_date) AS latest FROM daily").fetchone()
            stocks = con.execute("SELECT COUNT(DISTINCT ts_code) AS n FROM daily").fetchone()
        checks["cache"] = {"trade_dates": dates["n"], "latest_trade_date": dates["latest"], "stocks": stocks["n"]}
    except Exception as exc:
        checks["cache"] = {"error": str(exc)[:250]}
    return {"ok": bool(checks["database"].get("ok") and checks["tushare"].get("ok")), "checks": checks}
