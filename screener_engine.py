# -*- coding: utf-8 -*-
"""Tushare-backed A-share breakout screener. Daily bars are cached in SQLite."""
from __future__ import annotations
import os, sqlite3, time, math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import pandas as pd
import numpy as np

DB_PATH = os.getenv("SCREENER_DB", "/tmp/tushare_screener.sqlite3")
INDEX_CODE = os.getenv("SCREENER_INDEX", "000300.SH")
WEIGHTS = {"formula":20, "trend":20, "volume":15, "pressure":15, "relative":15, "fundamental":15}

class ScreenerError(RuntimeError): pass

def _date(s):
    return pd.to_datetime(str(s)).strftime("%Y%m%d")

def _ema(x, n):
    return x.ewm(span=n, adjust=False, min_periods=1).mean()

def _sma_cn(x, n, m):
    # Tongdaxin SMA(X,N,M): Y=(M*X+(N-M)*Y')/N
    a=x.astype(float).to_numpy()
    out=np.full(len(a), np.nan)
    prev=np.nan
    for i,v in enumerate(a):
        if np.isnan(v):
            out[i]=prev
        else:
            prev=v if np.isnan(prev) else (m*v+(n-m)*prev)/n
            out[i]=prev
    return pd.Series(out,index=x.index)

def original_formula(df):
    """Translate the supplied Tongdaxin formula. Input must be chronological raw daily OHLC."""
    x=df.copy()
    h,l,c=x["high"].astype(float),x["low"].astype(float),x["close"].astype(float)
    v1=_ema(h.rolling(500,min_periods=1).max(),21)
    v2=_ema(h.rolling(250,min_periods=1).max(),21)
    v3=_ema(h.rolling(90,min_periods=1).max(),21)
    v4=_ema(l.rolling(500,min_periods=1).min(),21)
    v5=_ema(l.rolling(250,min_periods=1).min(),21)
    v6=_ema(l.rolling(90,min_periods=1).min(),21)
    v7=_ema((v4*.96+v5*.96+v6*.96+v1*.558+v2*.558+v3*.558)/6,21)
    v8=_ema((v4*1.25+v5*1.23+v6*1.2+v1*.55+v2*.55+v3*.65)/6,21)
    v9=_ema((v4*1.3+v5*1.3+v6*1.3+v1*.68+v2*.68+v3*.68)/6,21)
    va=_ema((v7*3+v8*2+v9)/6*1.738,21)
    prev=l.shift(1)
    delta=l-prev
    numerator=_sma_cn((l-prev).abs(),3,1)
    denominator=_sma_cn(delta.clip(lower=0),3,1)
    vc=(numerator/denominator.replace(0,np.nan)*100).replace([np.inf,-np.inf],np.nan).fillna(0)
    vd=_ema(pd.Series(np.where(c*1.35<=va,vc*10,vc/10),index=x.index),3)
    ve=l.rolling(30,min_periods=1).min()
    vf=vd.rolling(30,min_periods=1).max()
    # Var10=IF(MA(CLOSE,58),1,0); for valid positive A-share prices this is 1.
    entry=_ema(pd.Series(np.where(l<=ve,(vd+vf*2)/2,0),index=x.index),3)/618
    x["entry_value"]=entry
    x["entry_signal"]=entry>0
    x["var_a"]=va
    return x

def _safe_float(v, default=None):
    try:
        if v is None or pd.isna(v): return default
        return float(v)
    except Exception: return default

def _fundamental_score(row):
    if not row or row.get("fundamental_status") != "ok":
        return 7.5, "财务数据未取到，暂按中性分；需核验"
    score=0; reasons=[]
    roe=_safe_float(row.get("roe")); rev=_safe_float(row.get("or_yoy")); npg=_safe_float(row.get("netprofit_yoy")); cfo=_safe_float(row.get("ocf_to_or"))
    if roe is not None:
        score += 5 if roe>=15 else 3 if roe>=8 else 1 if roe>0 else 0
        reasons.append(f"ROE {roe:.1f}%")
    if rev is not None:
        score += 4 if rev>=20 else 2 if rev>0 else 0
        reasons.append(f"营收同比 {rev:.1f}%")
    if npg is not None:
        score += 3 if npg>=20 else 1 if npg>0 else 0
        reasons.append(f"净利同比 {npg:.1f}%")
    if cfo is not None:
        score += 3 if cfo>=10 else 1 if cfo>0 else 0
        reasons.append(f"经营现金流/营收 {cfo:.1f}%")
    return min(15,score), "；".join(reasons) if reasons else "财务字段不足"

def score_one(df, index_returns=None, fundamental=None, name=""):
    """Calculate a current row's six dimensions using only data up to that row."""
    x=original_formula(df)
    if len(x)<90: return None
    x=x.dropna(subset=["close","high","low"])
    if len(x)<90: return None
    r=x.iloc[-1]; c=float(r.close); hist=x.iloc[:-1]
    close=x.close; ma20=close.rolling(20).mean(); ma60=close.rolling(60).mean()
    amount=x["amount"].astype(float) if "amount" in x else pd.Series(np.nan,index=x.index)
    avg5=amount.tail(5).mean(); avg20=amount.tail(20).mean()
    # Formula score: exact signal today, recent signal, and positive signal strength.
    sigs=x.entry_signal.fillna(False)
    formula=20 if bool(sigs.iloc[-1]) else 12 if sigs.tail(5).any() else 6 if sigs.tail(10).any() else 0
    # Trend structure: above key averages, averages rising, avoid excessive extension.
    m20=float(ma20.iloc[-1]); m60=float(ma60.iloc[-1])
    m20_prev=float(ma20.iloc[-6]) if len(x)>=25 else m20
    m60_prev=float(ma60.iloc[-6]) if len(x)>=65 else m60
    ext=c/m20-1 if m20 else 0
    trend=(5 if c>m20 else 0)+(5 if m20>m60 else 0)+(5 if m20>m20_prev and m60>=m60_prev else 0)+(5 if -0.02<=ext<=0.12 else 2 if ext<=0.20 else 0)
    # Volume/amount: expansion on up days, moderate expansion and contraction on pullbacks.
    vol=0
    if pd.notna(avg20) and avg20>0:
        ratio=avg5/avg20
        vol += 5 if 1.15<=ratio<=2.8 else 3 if ratio>1 else 0
        if c>float(x.close.iloc[-2]) and float(amount.iloc[-1])>avg20*1.3: vol+=5
        last5=x.tail(5)
        down=last5[last5.close<last5.open]
        if len(down)>0 and down.amount.mean()<avg20: vol+=5
    vol=min(15,vol)
    # Supply/pressure proxy: proximity to prior 60d high and volatility contraction.
    high60=float(x.high.tail(60).max()); low60=float(x.low.tail(60).min())
    dist= c/high60-1 if high60 else -1
    pressure=8 if -0.06<=dist<=0 else 5 if -0.12<=dist<-.06 else 2 if dist<-.12 else 0
    range20=(float(x.high.tail(20).max())-float(x.low.tail(20).min()))/c if c else 1
    range60=(float(x.high.tail(60).max())-float(x.low.tail(60).min()))/c if c else 1
    pressure += 7 if range20<range60*.55 else 4 if range20<range60*.75 else 1
    pressure=min(15,pressure)
    # Relative strength vs CSI 300 (20 trading sessions).
    ret20=c/float(x.close.iloc[-21])-1 if len(x)>=21 and float(x.close.iloc[-21]) else 0
    idxret=_safe_float(index_returns,0) or 0
    relraw=ret20-idxret
    relative=15 if relraw>=.12 else 12 if relraw>=.06 else 9 if relraw>=0 else 4 if relraw>=-.06 else 0
    fundamental_score,fundamental_note=_fundamental_score(fundamental)
    # No hidden look-ahead: this is the last completed row in the supplied dataframe.
    total=round(formula+trend+vol+pressure+relative+fundamental_score,1)
    prior20=float(x.high.iloc[-21:-1].max()) if len(x)>=21 else high60
    breakout=bool(c>prior20 and float(amount.iloc[-1])>avg20*1.3 if pd.notna(avg20) and avg20>0 else False)
    if breakout and c>m20 and relraw>0:
        stage="突破确认"
        stage_code=3
    elif c>=high60*.94 and range20<range60*.8 and c>m20*.97:
        stage="启动准备"
        stage_code=2
    elif bool(sigs.tail(10).any()) and c<=m20*1.08 and c>=low60*1.02:
        stage="底部潜伏"
        stage_code=1
    else:
        stage="观察中"
        stage_code=0
    reasons=[f"资金公式：{'触发' if bool(sigs.iloc[-1]) else '近5/10日有信号' if sigs.tail(10).any() else '未触发'}",
             f"趋势：收盘/MA20={c/m20:.2f}，MA20 {'高于' if m20>m60 else '低于'} MA60",
             f"成交额：5日/20日均值={avg5/avg20:.2f}" if pd.notna(avg20) and avg20>0 else "成交额数据不足",
             f"距60日高点 {dist*100:.1f}%，20日振幅 {range20*100:.1f}%",
             f"相对沪深300超额收益 {relraw*100:.1f}%",
             f"财务质量：{fundamental_note}"]
    return {"score":total,"stage":stage,"stage_code":stage_code,"price":round(c,3),"change_pct":round((c/float(x.close.iloc[-2])-1)*100,2) if len(x)>1 and x.close.iloc[-2] else None,
            "entry_value":round(float(r.entry_value),4),"entry_signal":bool(r.entry_signal),"formula_score":formula,"trend_score":trend,"volume_score":vol,"pressure_score":pressure,"relative_score":relative,"fundamental_score":fundamental_score,
            "ret20":round(ret20*100,2),"relative_excess":round(relraw*100,2),"amount_ratio":round(avg5/avg20,2) if pd.notna(avg20) and avg20>0 else None,
            "resistance20":round(prior20,3),"high60":round(high60,3),"low60":round(low60,3),"reasons":reasons}

class TushareScreener:
    def __init__(self):
        token=os.getenv("TUSHARE_TOKEN","").strip()
        if not token: raise ScreenerError("缺少 TUSHARE_TOKEN 环境变量。请在 Render 环境变量中设置，不要把 Token 写进代码。")
        try:
            import tushare as ts
            self.pro=ts.pro_api(token)
        except Exception as e: raise ScreenerError(f"Tushare SDK 初始化失败：{e}")
        self.db=sqlite3.connect(DB_PATH,check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS daily(ts_code TEXT,trade_date TEXT,open REAL,high REAL,low REAL,close REAL,pre_close REAL,change REAL,pct_chg REAL,vol REAL,amount REAL,PRIMARY KEY(ts_code,trade_date))""")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(trade_date)")
        self.db.commit()

    def _call(self, fn, **kwargs):
        last=None
        for i in range(4):
            try:
                result=fn(**kwargs)
                if result is None: return pd.DataFrame()
                return result
            except Exception as e:
                last=e; time.sleep(1.2*(i+1))
        raise ScreenerError(f"Tushare 接口调用失败（{getattr(fn,'__name__','API')}）：{last}")

    def stock_list(self, include_delisted=False):
        statuses=["L","D","P"] if include_delisted else ["L"]
        frames=[]
        for st in statuses:
            d=self._call(self.pro.stock_basic,exchange="",list_status=st,fields="ts_code,symbol,name,industry,list_date,list_status,market")
            if not d.empty: frames.append(d)
        if not frames: raise ScreenerError("stock_basic 没有返回股票列表，请检查 Token/权限。")
        z=pd.concat(frames,ignore_index=True).drop_duplicates("ts_code")
        z=z[z.ts_code.astype(str).str.endswith((".SH",".SZ"))]
        z=z[~z.ts_code.astype(str).str.startswith(("900","200"))]
        return z

    def calendar(self, start_date, end_date):
        cal=self._call(self.pro.trade_cal,exchange="",start_date=_date(start_date),end_date=_date(end_date),is_open="1",fields="cal_date,is_open")
        if cal.empty: raise ScreenerError("交易日历为空。")
        return sorted(cal.cal_date.astype(str).tolist())

    def ensure_daily(self, dates):
        cur=self.db.cursor()
        missing=[]
        for d in dates:
            exists=cur.execute("SELECT 1 FROM daily WHERE trade_date=? LIMIT 1",(d,)).fetchone()
            if not exists: missing.append(d)
        fetched=0
        for i,d in enumerate(missing,1):
            frame=self._call(self.pro.daily,trade_date=d,fields="ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount")
            if not frame.empty:
                frame=frame.where(pd.notnull(frame),None)
                self.db.executemany("INSERT OR REPLACE INTO daily VALUES(?,?,?,?,?,?,?,?,?,?,?)",frame[["ts_code","trade_date","open","high","low","close","pre_close","change","pct_chg","vol","amount"]].itertuples(index=False,name=None))
                self.db.commit(); fetched+=len(frame)
            if i<len(missing): time.sleep(.22)
        return {"dates_requested":len(dates),"dates_downloaded":len(missing),"rows_downloaded":fetched,"cache_path":DB_PATH}

    def history(self, ts_code, end_date=None, limit=560):
        q="SELECT * FROM daily WHERE ts_code=?"
        args=[ts_code]
        if end_date: q+=" AND trade_date<=?"; args.append(_date(end_date))
        q+=" ORDER BY trade_date DESC LIMIT ?"; args.append(int(limit))
        d=pd.read_sql_query(q,self.db,params=args)
        if d.empty: return d
        return d.sort_values("trade_date").reset_index(drop=True)

    def index_return(self, end_date, lookback=20):
        try:
            d=self._call(self.pro.index_daily,ts_code=INDEX_CODE,end_date=_date(end_date),limit=30,fields="trade_date,close")
            if d.empty or len(d)<lookback+1: return 0.0
            d=d.sort_values("trade_date")
            return float(d.close.iloc[-1]/d.close.iloc[-lookback-1]-1)
        except Exception:
            return 0.0

    def fundamentals(self, ts_code, asof_date):
        try:
            start=(pd.to_datetime(asof_date)-pd.Timedelta(days=1100)).strftime("%Y%m%d")
            d=self._call(self.pro.fina_indicator,ts_code=ts_code,start_date=start,end_date=_date(asof_date),fields="ts_code,ann_date,end_date,roe,or_yoy,netprofit_yoy,ocf_to_or")
            if d.empty: return {"fundamental_status":"unavailable"}
            d=d[d.ann_date.astype(str)<=_date(asof_date)].sort_values(["ann_date","end_date"])
            if d.empty: return {"fundamental_status":"unavailable"}
            r=d.iloc[-1].to_dict(); r["fundamental_status"]="ok"; return r
        except Exception:
            return {"fundamental_status":"unavailable"}

    def scan(self, days=520, limit=30, enrich=80):
        end=datetime.now().strftime("%Y%m%d")
        start=(datetime.now()-timedelta(days=int(days*1.65)+40)).strftime("%Y%m%d")
        dates=self.calendar(start,end)[-int(days):]
        if len(dates)<500: raise ScreenerError(f"完整原始公式需要至少500个交易日，目前交易日历仅返回{len(dates)}天。")
        cache=self.ensure_daily(dates)
        latest=dates[-1]
        universe=self.stock_list(False)
        index_ret=self.index_return(latest)
        # Use a single latest daily_basic snapshot when permission is available; optional, never fabricate.
        basics={}
        try:
            b=self._call(self.pro.daily_basic,trade_date=latest,fields="ts_code,turnover_rate,volume_ratio,pe_ttm,pb,total_mv,circ_mv")
            if not b.empty: basics={r.ts_code:r._asdict() for r in b.itertuples(index=False)}
        except Exception: pass
        prelim=[]
        for row in universe.itertuples(index=False):
            h=self.history(row.ts_code,end_date=latest,limit=days)
            if len(h)<500: continue
            result=score_one(h,index_returns=index_ret,name=row.name)
            if not result: continue
            result.update({"ts_code":row.ts_code,"name":row.name,"industry":getattr(row,"industry","") or "",
                           "trade_date":latest,"data_source":"Tushare Pro daily"})
            if "ST" in str(row.name).upper() or "退" in str(row.name): result["risk_flag"]="ST/退市风险名称过滤"
            else: result["risk_flag"]=""
            basic=basics.get(row.ts_code)
            if basic:
                result["pe_ttm"]=_safe_float(basic.get("pe_ttm")); result["pb"]=_safe_float(basic.get("pb"))
                result["turnover_rate"]=_safe_float(basic.get("turnover_rate"))
            prelim.append(result)
        prelim.sort(key=lambda x:x["score"],reverse=True)
        # Financial quality calls are limited to top technical candidates; unknowns get neutral, explicit labels.
        for item in prelim[:max(0,min(int(enrich),120))]:
            f=self.fundamentals(item["ts_code"],latest)
            fs,note=_fundamental_score(f)
            item["fundamental_score"]=fs
            item["score"]=round(item["score"]-7.5+fs,1)
            item["fundamental_note"]=note
        prelim.sort(key=lambda x:(x["stage_code"],x["score"]),reverse=True)
        # Never present a ranking as a probability of a price explosion.
        return {"trade_date":latest,"count":len(prelim),"results":prelim[:int(limit)],"cache":cache,
                "permissions_note":"daily/stock_basic 与 daily_basic、fina_indicator 的积分/接口权限不同；未授权财务字段显示为未知，不伪造。",
                "method_note":"阶段和评分是研究筛选规则，不是涨停预测或收益保证。原始公式使用未复权日线；与通达信复权设置不一致时信号会不同。"}

    def backtest(self, start_date, end_date, max_symbols=1200, min_score=60):
        s,e=_date(start_date),_date(end_date)
        dates=self.calendar((pd.to_datetime(s)-pd.Timedelta(days=900)).strftime("%Y%m%d"),(pd.to_datetime(e)+pd.Timedelta(days=45)).strftime("%Y%m%d"))
        # At least 500 warm-up sessions before start and 20 sessions after end.
        pos=next((i for i,d in enumerate(dates) if d>=s),None)
        if pos is None or pos<500: raise ScreenerError("回测开始日期前必须至少有500个交易日预热数据。")
        endpos=next((i for i,d in enumerate(dates) if d>=e),len(dates)-1)
        chosen=dates[pos-500:min(len(dates),endpos+22)]
        cache=self.ensure_daily(chosen)
        symbols=self.stock_list(True).ts_code.tolist()[:int(max_symbols)]
        # Index series fetched once for the requested period.
        idx=self._call(self.pro.index_daily,ts_code=INDEX_CODE,start_date=chosen[0],end_date=chosen[-1],fields="trade_date,close")
        idx=idx.sort_values("trade_date") if not idx.empty else pd.DataFrame()
        idxret={}
        if len(idx)>20:
            for i in range(20,len(idx)):
                idxret[idx.trade_date.iloc[i]]=float(idx.close.iloc[i]/idx.close.iloc[i-20]-1)
        trades=[]
        for code in symbols:
            h=self.history(code,limit=len(chosen)+5)
            if len(h)<520: continue
            x=original_formula(h)
            x["ma20"]=x.close.rolling(20).mean(); x["ma60"]=x.close.rolling(60).mean()
            x["avg5"]=x.amount.rolling(5).mean(); x["avg20"]=x.amount.rolling(20).mean()
            x["high20_prev"]=x.high.shift(1).rolling(20).max()
            x["high60"]=x.high.rolling(60).max()
            x["low60"]=x.low.rolling(60).min()
            x["range20"]=(x.high.rolling(20).max()-x.low.rolling(20).min())/x.close
            x["range60"]=(x.high.rolling(60).max()-x.low.rolling(60).min())/x.close
            x["ret20"]=x.close/x.close.shift(20)-1
            x=x.reset_index(drop=True)
            date_to_i={d:i for i,d in enumerate(x.trade_date.astype(str))}
            for d in dates[pos:endpos+1]:
                i=date_to_i.get(d)
                if i is None or i+20>=len(x): continue
                r=x.iloc[i]
                if pd.isna(r.entry_value) or pd.isna(r.ma60): continue
                # Five observable dimensions only; fundamental snapshots are excluded to avoid look-ahead bias.
                sig=bool(r.entry_signal)
                formula=20 if sig else 12 if x.entry_signal.iloc[max(0,i-4):i+1].any() else 6 if x.entry_signal.iloc[max(0,i-9):i+1].any() else 0
                trend=(5 if r.close>r.ma20 else 0)+(5 if r.ma20>r.ma60 else 0)+(5 if r.ma20>x.ma20.iloc[max(0,i-5)] else 0)+(5 if -.02<=r.close/r.ma20-1<=.12 else 2 if r.close/r.ma20-1<=.2 else 0)
                vr=(r.avg5/r.avg20) if r.avg20 and not pd.isna(r.avg20) else 0
                volume=(5 if 1.15<=vr<=2.8 else 3 if vr>1 else 0)+(5 if r.close>r.open and r.amount>r.avg20*1.3 else 0)
                pressure=(8 if -.06<=r.close/r.high60-1<=0 else 5 if -.12<=r.close/r.high60-1<-.06 else 2)+(7 if r.range20<r.range60*.55 else 4 if r.range20<r.range60*.75 else 1)
                excess=(r.ret20 or 0)-(idxret.get(d,0) or 0)
                relative=15 if excess>=.12 else 12 if excess>=.06 else 9 if excess>=0 else 4 if excess>=-.06 else 0
                tech_score=(formula+trend+min(15,volume)+min(15,pressure)+relative)/85*100
                if tech_score<min_score and not sig: continue
                fwd=x.iloc[i+1:i+21]
                entry=float(r.close)
                returns={f"{n}d":round((float(x.close.iloc[i+n])/entry-1)*100,2) if i+n<len(x) else None for n in (5,10,20)}
                max_up=round((float(fwd.high.max())/entry-1)*100,2) if len(fwd) else None
                max_dd=round((float(fwd.low.min())/entry-1)*100,2) if len(fwd) else None
                trades.append({"ts_code":code,"trade_date":d,"score":round(tech_score,1),"formula_signal":sig,**returns,"max_up_20d":max_up,"max_dd_20d":max_dd})
        if not trades: return {"count":0,"trades":[],"summary":{},"cache":cache,"note":"没有产生符合条件的历史信号；可降低最低评分或增加股票数量。"}
        df=pd.DataFrame(trades)
        summary={}
        for col in ("5d","10d","20d"):
            vals=pd.to_numeric(df[col],errors="coerce").dropna()
            summary[col]={"count":int(len(vals)),"win_rate":round(float((vals>0).mean()*100),1),"avg_return":round(float(vals.mean()),2),"median_return":round(float(vals.median()),2)}
        summary["max_up_20d_avg"]=round(float(df.max_up_20d.mean()),2)
        summary["max_dd_20d_avg"]=round(float(df.max_dd_20d.mean()),2)
        summary["warning"]="历史回测仅包含当前可取到的上市/退市股票列表，且未纳入历史ST状态、停牌无法成交、涨跌停成交约束、交易费用和滑点；结果存在幸存者偏差与执行偏差。财务评分未纳入回测以避免未来数据泄漏。"
        return {"count":len(df),"summary":summary,"trades":df.sort_values(["trade_date","score"],ascending=[False,False]).head(500).to_dict("records"),"cache":cache}
