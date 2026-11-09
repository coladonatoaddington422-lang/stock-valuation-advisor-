from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from datetime import datetime, timedelta
import requests, re, time, statistics

ROOT=Path(__file__).parent
app=FastAPI(title="股票估值助手",version="2.4.0")
S=requests.Session()
S.headers.update({"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36","Referer":"https://quote.eastmoney.com/"})
CACHE={}
CACHE_TTL=300

def clean_code(code):
    code=re.sub(r"\D","",str(code))
    if len(code)!=6: raise ValueError("请输入6位A股代码")
    if code.startswith(("4","8","9","200")): raise ValueError("暂不支持北交所、B股及非A股代码")
    if not code.startswith(("000","001","002","003","300","301","600","601","603","605","688","689")): raise ValueError("暂不支持该代码段，请输入沪深A股股票代码")
    return code

def secid(code): return ("1." if code.startswith(("5","6","9")) else "0.")+code

def em_get(url,params):
    r=S.get(url,params=params,timeout=15); r.raise_for_status(); return r.json()

def n(v):
    try:
        if v is None or v in ("","-"): return None
        return float(v)
    except (ValueError,TypeError): return None

def classify(name,code):
    # Priority classification: explicit business-model overrides precede broad keywords.
    cycle_codes={"000792","601899","600547","601088","600938","603993","600096","601857","600188","600348","000630","000983","601600","600362","600489","601168","601212","600111","000933"}
    dividend_codes={"600900","600025","600886","601985","600011","600023","600028","600941","601728","600050","601988","601398","601939","601288","600919","601166"}
    growth_codes={"300750","300760","688981","688012","688256","002230","300454","002415","300059","300124","688111"}
    if code in cycle_codes:
        return "周期资源股","按主营资源/商品周期属性识别；重点看商品价格、成本曲线、储量、资本开支和周期位置",["PB历史分位","NAV资产净值"],"周期资源股禁止使用PE历史分位直接定价；高景气利润会让PE显得异常低。"
    if code in dividend_codes:
        return "稳定高股息/公用事业","按稳定现金流、分红属性识别；重点看股息率、分红率、负债和资本开支",["PE历史分位","PB历史分位"],"需检查股息率、自由现金流和分红可持续性，不能只看静态股息率。"
    if code in growth_codes:
        return "成长股","按成长型行业代表公司识别；重点看收入增长、利润率、ROE/ROIC和经营现金流",["PE历史分位"],"成长预期变化快，历史估值分位不能替代盈利预测。"
    nm=(name or "").lower()
    cyc=["盐湖","紫金","山东黄金","中国海油","洛阳钼业","云天化","矿","有色","黄金","铜","铝","锂","稀土","煤炭","石油","油气","化肥","磷化工","钢铁","航运"]
    div=["水电","电力","高速","公路","港口","燃气","运营商","银行","公用事业"]
    grow=["软件","半导体","芯片","人工智能","机器人","通信设备","医药","生物","新能源","消费电子","计算机"]
    if any(k in nm for k in cyc): return "周期资源股","根据股票名称关键词初筛；请核对主营业务",["PB历史分位","NAV资产净值"],"周期股不使用PE历史分位作为买入依据；名称关键词分类需核对主营业务。"
    if any(k in nm for k in div): return "稳定高股息/公用事业","根据股票名称关键词初筛；请核对主营业务",["PE历史分位","PB历史分位"],"需检查分红、自由现金流、负债和资本开支。"
    if any(k in nm for k in grow): return "成长股","根据股票名称关键词初筛；请核对主营业务",["PE历史分位"],"需验证收入增速、利润率和现金流；成长预期下修会使历史分位失真。"
    return "待人工确认","暂未匹配可靠行业分类",["不自动估值"],"无法可靠识别行业，暂不生成买入区间。请先确认主营业务。"

def get_quote(code):
    js=em_get("https://push2.eastmoney.com/api/qt/stock/get",{"secid":secid(code),"fields":"f43,f57,f58,f116,f162,f164,f167,f170,f173","fltt":2,"invt":2,"ut":"fa5fd1943c7b386f172d6893dbfba10b"})
    d=js.get("data")
    if not d: raise HTTPException(404,"未找到股票，请检查代码")
    return {"code":code,"name":d.get("f58") or "","price":n(d.get("f43")),"pe_ttm":n(d.get("f164")),"pe_dynamic":n(d.get("f162")),"pb":n(d.get("f167")),"roe":n(d.get("f173")),"market_cap":n(d.get("f116")),"change_pct":n(d.get("f170"))}

def get_history(code):
    # AkShare's current interface is stock_a_indicator_lg(symbol=...), not stock_a_indicator_lg(symbol=...).
    # Its trade_date is commonly the DataFrame index; normalize it before calculating windows.
    try:
        import akshare as ak
        df=ak.stock_a_indicator_lg(symbol=code)
        if df is None or len(df)==0: return None
        # Normalize tabular output and older versions with trade_date in the index.
        if "trade_date" not in [str(x).strip().lower() for x in df.columns] and getattr(df.index,"name",None):
            df=df.reset_index()
        else:
            df=df.reset_index(drop=True)
        cols={str(col).strip().lower():col for col in df.columns}
        date_col=cols.get("trade_date") or cols.get("日期") or cols.get("index")
        pe_col=cols.get("pe_ttm") or cols.get("pe")
        pb_col=cols.get("pb")
        if not date_col or not pb_col: return None
        rows=[]
        today=datetime.now().date()
        cutoff3=today-timedelta(days=365*3)
        cutoff5=today-timedelta(days=365*5)
        for _,row in df.iterrows():
            try:
                dt=row[date_col]
                day=dt.date() if hasattr(dt,"date") else datetime.strptime(str(dt)[:10],"%Y-%m-%d").date()
            except Exception: continue
            pe=n(row[pe_col]) if pe_col else None
            pb=n(row[pb_col])
            rows.append({"date":day.isoformat(),"pe":pe if pe is not None and pe>0 else None,"pb":pb if pb is not None and pb>0 else None})
        rows.sort(key=lambda x:x["date"])
        rows=[x for x in rows if x["pb"] is not None or x["pe"] is not None]
        if not rows:return None
        return {"all":rows,"3y":[x for x in rows if datetime.strptime(x["date"],"%Y-%m-%d").date()>=cutoff3],"5y":[x for x in rows if datetime.strptime(x["date"],"%Y-%m-%d").date()>=cutoff5]}
    except Exception:
        return None

def percentile(values,current):
    vals=sorted(x for x in values if x is not None and x>0)
    if not vals or current is None or current<=0:return None
    return round(100*sum(1 for x in vals if x<=current)/len(vals),1)

def quantile(values,q):
    vals=sorted(x for x in values if x is not None and x>0)
    if len(vals)<60:return None
    i=(len(vals)-1)*q; lo=int(i); hi=min(lo+1,len(vals)-1); frac=i-lo
    return vals[lo]*(1-frac)+vals[hi]*frac

def hist_summary(history,metric,current):
    if not history:return None
    out={}
    for period in ("3y","5y"):
        vals=[x.get(metric) for x in history.get(period,[])]
        valid=[x for x in vals if x is not None and x>0]
        out[period]={"count":len(valid),"percentile":percentile(valid,current),"q10":round(quantile(valid,.1),3) if quantile(valid,.1) is not None else None,"q20":round(quantile(valid,.2),3) if quantile(valid,.2) is not None else None,"q40":round(quantile(valid,.4),3) if quantile(valid,.4) is not None else None,"median":round(quantile(valid,.5),3) if quantile(valid,.5) is not None else None,"q60":round(quantile(valid,.6),3) if quantile(valid,.6) is not None else None,"q80":round(quantile(valid,.8),3) if quantile(valid,.8) is not None else None}
    return out

def make_valuation(q,kind,history):
    price=q["price"]; pe=q["pe_ttm"]; pb=q["pb"]
    pe_h=hist_summary(history,"pe",pe); pb_h=hist_summary(history,"pb",pb)
    chosen=[]; assumptions=[]; warnings=[]
    metrics=[]
    if kind=="周期资源股":
        chosen=["PB历史分位 + NAV资产净值"]
        metrics=[("PB",pb,pb_h)]
        assumptions.append("周期资源股禁止使用PE历史分位定价；仅用PB历史分位作历史参照，NAV需另行核验资源项目净值。")
        warnings.append("资源股PB换算假设每股净资产短期不变；若商品价格、资产减值或资本开支变化，区间会失真。NAV需核验储量、可采比例、成本、税费、资本开支、权益比例及净负债。")
    elif kind=="稳定高股息/公用事业":
        chosen=["PE + PB历史分位结合"]
        metrics=[("PE",pe,pe_h),("PB",pb,pb_h)]
        assumptions.extend(["PE换算假设当前TTM盈利水平可持续。","PB换算假设当前每股净资产口径稳定。"])
        warnings.append("需核验分红率、自由现金流、债务和资本开支；盈利或净资产口径突变会使历史分位失真。")
    elif kind=="成长股":
        chosen=["PE历史分位为主"]
        metrics=[("PE",pe,pe_h)]
        assumptions.append("PE换算假设当前盈利可持续；若处于高增长、亏损或利润拐点阶段，历史PE比较可能失效。")
        warnings.append("需核验收入增速、利润率、ROE/ROIC、稀释融资和经营现金流；成长预期下修会使历史分位失去参考意义。")
    else:
        chosen=["不自动生成价格区间"]
        warnings.append("行业未可靠识别，不输出合理价值或历史买入区间。请先确认主营业务。")
    fair_l=[]; fair_h=[]; buy_l=[]; buy_h=[]; detail=[]
    for metric,current,hist in metrics:
        if current is None or current<=0 or not hist: continue
        for period in ("3y","5y"):
            z=hist.get(period)
            if not z: continue
            # q40-q60 is a historical middle valuation band (reference fair-value band).
            # q10-q20 is the lower historical valuation zone (entry reference).
            if z.get("q40") is not None and z.get("q60") is not None:
                fair_l.append(price*z["q40"]/current); fair_h.append(price*z["q60"]/current)
            if z.get("q10") is not None and z.get("q20") is not None:
                buy_l.append(price*z["q10"]/current); buy_h.append(price*z["q20"]/current)
            detail.append(f"{metric}-{period}: 当前分位{z.get('percentile')}%，q10={z.get('q10')}，q20={z.get('q20')}，q40={z.get('q40')}，q60={z.get('q60')}")
    # Combine available periods/methods by median to avoid one window dominating.
    med=lambda xs: statistics.median(xs) if xs else None
    fair_low=med(fair_l); fair_high=med(fair_h)
    buy_low=med(buy_l); buy_high=med(buy_h)
    if fair_low is not None:
        assumptions.append("合理价值参考区间：将近3年/5年适用估值指标的历史40%—60%分位，按当前盈利或净资产口径换算为股价；这是历史中枢参考，不是DCF内在价值。")
    if buy_low is not None:
        assumptions.append("历史低估买入参考区间：将适用指标近3年/5年历史10%—20%分位换算为股价；低估区间不等于保证安全或未来上涨。")
    return {"current_price":price,
        "fair_value_low":round(fair_low,2) if fair_low is not None else None,
        "fair_value_high":round(fair_high,2) if fair_high is not None else None,
        "buy_low":round(buy_low,2) if buy_low is not None else None,
        "buy_high":round(buy_high,2) if buy_high is not None else None,
        "methods":chosen,"assumptions":assumptions,"warnings":warnings,
        "history_detail":detail,"pe_history":pe_h,"pb_history":pb_h,
        "history_source":"AkShare / 乐咕乐股个股历史估值序列" if history else None,
        "history_available":bool(history)}

@app.get("/api/health")
def health(): return {"ok":True,"version":"2.3.0","features":["automatic_classification","historical_3y_5y_percentiles","historical_fair_value_band","historical_low_entry_band","dcf_tutorial","nav_tutorial"]}

@app.get("/api/stock/{code}")
def stock(code:str):
    try: code=clean_code(code)
    except ValueError as e: raise HTTPException(400,str(e))
    if code in CACHE and time.time()-CACHE[code][0]<CACHE_TTL:return CACHE[code][1]
    q=get_quote(code)
    kind,basis,_,rule=classify(q["name"],code)
    history=get_history(code)
    valuation=make_valuation(q,kind,history)
    result={**q,"type":kind,"classification":kind,"classification_basis":basis,"classification_rule":rule,"valuation":valuation,"valuation_methods":valuation.get("methods",[]),"fair_value_range":[valuation.get("fair_value_low"),valuation.get("fair_value_high")],"historical_low_buy_range":[valuation.get("buy_low"),valuation.get("buy_high")],"as_of":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"risk_disclaimer":"估值仅为历史统计和模型假设，不构成投资建议。历史低估区间不代表未来一定上涨，也不是公允价值。"}
    CACHE[code]=(time.time(),result)
    return result

@app.get("/api/tutorial")
def tutorial():
    return {"title":"盐湖股份（000792）DCF / NAV教学演示","note":"以下全部为教学假设，不是盐湖股份实际财务预测、储量评估或目标价。请用最新年报、公告和行情替换参数。","dcf":{"fcf0":70,"growth":5,"discount":10,"terminal_growth":2,"years":5,"net_cash":300,"shares":54.3},"nav":{"potash_value":800,"lithium_value":700,"other_assets":100,"net_cash":300,"other_liabilities":100,"shares":54.3},"steps":["DCF：预测未来5年自由现金流，逐年按折现率折现；终值=第5年FCF×(1+永续增长率)/(折现率-永续增长率)，再折现回今天。","企业价值=显性期现金流现值+终值现值；股权价值=企业价值+净现金；每股价值=股权价值/总股本。","NAV：分别估算钾肥、锂资源项目的税后资产净值，扣除开发资本开支、税费、净负债、少数股东权益等，再除以总股本。","盐湖股份是钾肥+锂资源的周期型公司，NAV适合拆分资源资产；DCF可用于相对稳定、可预测的分部现金流，但商品价格需用周期中枢和多情景预测。"]}

@app.post("/api/calculate/dcf")
def calculate_dcf(body:dict):
    fcf=float(body.get("fcf",0)); growth=float(body.get("growth",0))/100; discount=float(body.get("discount",0))/100; tg=float(body.get("terminal_growth",0))/100; years=int(body.get("years",5)); net_cash=float(body.get("net_cash",0)); shares=float(body.get("shares",0))
    if fcf<=0 or shares<=0 or years<1 or years>10 or discount<=tg or tg<0: raise HTTPException(400,"请检查参数：现金流和股本须大于0，折现率必须高于永续增长率。")
    pv=0; flows=[]
    for y in range(1,years+1):
        cf=fcf*((1+growth)**(y-1)); p=cf/((1+discount)**y); pv+=p; flows.append({"year":y,"fcf":round(cf,2),"pv":round(p,2)})
    terminal=flows[-1]["fcf"]*(1+tg)/(discount-tg); terminal_pv=terminal/((1+discount)**years)
    ev=pv+terminal_pv; equity=ev+net_cash
    return {"flows":flows,"explicit_pv":round(pv,2),"terminal_value":round(terminal,2),"terminal_pv":round(terminal_pv,2),"enterprise_value":round(ev,2),"equity_value":round(equity,2),"per_share":round(equity/shares,2),"unit":"亿元；每股价值为元"}

@app.post("/api/calculate/nav")
def calculate_nav(body:dict):
    vals=[float(body.get(k,0)) for k in ("potash_value","lithium_value","other_assets","net_cash","other_liabilities")]
    shares=float(body.get("shares",0))
    if shares<=0 or any(v<0 for v in vals):raise HTTPException(400,"请输入非负资产/负债金额及正数总股本。")
    equity=vals[0]+vals[1]+vals[2]+vals[3]-vals[4]
    return {"potash_value":vals[0],"lithium_value":vals[1],"other_assets":vals[2],"net_cash":vals[3],"other_liabilities":vals[4],"equity_nav":round(equity,2),"per_share":round(equity/shares,2),"unit":"亿元；每股价值为元"}

@app.get("/")
def home(): return FileResponse(ROOT/"static"/"index.html")
app.mount("/",StaticFiles(directory=ROOT/"static",html=True),name="static")
