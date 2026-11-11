"""Runtime diagnostics for the 120-point Tushare tier and database connectivity."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from uuid import uuid4
import os
import tushare as ts
from fastapi import APIRouter
from screener import _db, DATABASE_URL

router = APIRouter()
SH = ZoneInfo("Asia/Shanghai")


@router.get("/api/system/self-check")
def system_self_check():
    checks = {}
    try:
        marker = str(uuid4())
        with _db() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS system_self_check (
                check_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL
            )""")
            now = datetime.now(SH).isoformat(timespec="seconds")
            con.execute("INSERT INTO system_self_check(check_id, checked_at) VALUES (?, ?)", (marker, now))
            row = con.execute("SELECT check_id FROM system_self_check WHERE check_id=?", (marker,)).fetchone()
            if not row or row["check_id"] != marker:
                raise RuntimeError("写入后读取结果不一致")
            con.execute("DELETE FROM system_self_check WHERE check_id=?", (marker,))
        checks["database"] = {"ok": True, "backend": "Neon PostgreSQL" if DATABASE_URL else "SQLite",
                              "write": True, "read": True, "delete": True}
    except Exception as exc:
        checks["database"] = {"ok": False, "error": str(exc)[:300]}

    token = os.getenv("TUSHARE_TOKEN", "").strip()
    checks["token_configured"] = bool(token)
    checks["tushare"] = {"daily": False, "trade_cal": False,
                         "stock_basic": False, "daily_basic": False}
    if token:
        try:
            ts.set_token(token)
            pro = ts.pro_api()
            end = datetime.now(SH).strftime("%Y%m%d")
            start = (datetime.now(SH) - timedelta(days=20)).strftime("%Y%m%d")
            # The 120-point tier grants non-adjusted daily bars; test it independently.
            try:
                d = pro.daily(ts_code="000001.SZ", start_date=start, end_date=end,
                              fields="ts_code,trade_date,close", limit=6000)
                checks["tushare"]["daily"] = d is not None and not d.empty
                if checks["tushare"]["daily"]:
                    checks["tushare"]["latest_daily_date"] = str(d["trade_date"].max())
            except Exception as exc:
                checks["tushare"]["daily_error"] = str(exc)[:250]
            # Higher-tier endpoints are informational only; they must not block the 120-point workflow.
            for name, call in [
                ("trade_cal", lambda: pro.trade_cal(exchange="SSE", start_date=start, end_date=end,
                                                    is_open="1", fields="cal_date,is_open")),
                ("stock_basic", lambda: pro.stock_basic(exchange="", list_status="L",
                                                        fields="ts_code,name", limit=1)),
                ("daily_basic", lambda: pro.daily_basic(ts_code="000001.SZ", start_date=start,
                                                        end_date=end, fields="ts_code,trade_date,pe_ttm,pb",
                                                        limit=6000)),
            ]:
                try:
                    result = call()
                    checks["tushare"][name] = result is not None and not result.empty
                except Exception as exc:
                    checks["tushare"][name + "_error"] = str(exc)[:250]
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
    checks["note"] = "120积分可先使用daily日线；trade_cal、stock_basic、daily_basic未获权限时不阻止日线初始化。"
    return {"ok": bool(checks["database"].get("ok") and checks["tushare"].get("daily")), "checks": checks}
