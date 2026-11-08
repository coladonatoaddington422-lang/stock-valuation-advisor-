from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import requests, re, time

ROOT = Path(__file__).parent
app = FastAPI(title="股票估值助手", version="1.0.0")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent":"Mozilla/5.0 Chrome/120 Safari/537.36","Referer":"https://quote.eastmoney.com/"})
CACHE = {}
CACHE_SECONDS = 45

def secid_for(code: str):
    code = re.sub(r"\\D", "", code)
    if len(code) != 6: raise ValueError("请输入6位A股代码")
    if code.startswith(("4","8")): raise ValueError("V1.0暂不支持北交所股票")
    return f'{"1" if code.startswith(("5","6","9")) else "0"}.{code}'

def em_get(url, params):
    r=SESSION.get(url,params=params,timeout=12); r.raise_for_status(); return r.json()

def num(v):
    try: return None if v is None or v in ("","-") else float(v)
    except (ValueError,TypeError): return None

def classify(name, code):
    known={
    "601899":("多金属矿业",["NAV","EV/EBITDA","PB"],"铜、金等多金属业务并存。重点看各矿种储量、产量、现金成本、资本开支及铜金价格情景。"),
    "600547":("黄金矿业",["NAV","PB","EV/EBITDA"],"重点看可采储量、品位、单位成本、产量和金价。"),
    "601088":("煤炭/综合能源",["PE","EV/EBITDA","DCF","股息率"],"看中周期煤价、吨煤成本、长协比例、资本开支和分红；低PE可能来自周期利润高点。"),
    "600938":("油气",["DCF","EV/EBITDA","PB","股息率"],"用不同油价情景测算现金流，结合储量替代率、桶油成本、资本开支和分红。"),
    "000792":("盐湖资源/化工",["NAV","EV/EBITDA","PB","PE"],"看锂钾资源储量与开发成本、产品价格、产能爬坡和资本开支。"),
    "600096":("磷化工/化肥",["PE","EV/EBITDA","PB"],"看磷矿资源、产品价差、成本曲线、产能利用率和资本开支。"),
    "603993":("多金属矿业",["NAV","EV/EBITDA","PB"],"铜钴钼钨等业务应分矿种评估储量、成本和价格情景，再汇总NAV。")}
    if code in known:return known[code]
    n=(name or "").lower()
    groups=[
    (["银行"],"银行",["PB","PE"],"重点检查资产质量、ROE、不良率、拨备覆盖率和资本充足率。"),
    (["保险"],"保险",["PB","DCF"],"结合内含价值、偿付能力和投资收益，普通PE/PB不能单独定价。"),
    (["黄金","金矿"],"黄金矿业",["NAV","PB","EV/EBITDA"],"资源储量、品位、开采成本和金价共同决定价值。"),
    (["铜","钼","钨","锂","稀土","镍","铝","锌","铅","矿业"],"有色/矿业",["NAV","EV/EBITDA","PB"],"看资源储量、单位现金成本、资本开支和商品价格情景。"),
    (["煤炭","能源"],"煤炭/能源",["PE","EV/EBITDA","DCF"],"看中周期利润、吨煤成本、资本开支、负债和分红。"),
    (["石油","海油","石化","油气"],"油气",["DCF","EV/EBITDA","PB"],"用油价情景测算现金流，结合储量、成本和资本开支。"),
    (["盐湖","钾肥","磷化工","化肥","农化"],"化肥/资源化工",["PE","EV/EBITDA","NAV"],"看产品价差、成本曲线、产能利用率和资源禀赋。"),
    (["电力","水电","核电"],"公用事业",["DCF","PE","股息率"],"关注电价机制、利用小时、负债和资本开支。"),
    (["软件","互联网","网络科技"],"软件/互联网",["PE","PS","DCF"],"看收入质量、毛利率、现金流和客户留存。"),
    (["食品","饮料","白酒","乳业","家电","服装"],"消费",["PE","DCF"],"看品牌力、ROIC、现金流和增长持续性。"),
    (["医药","生物","制药","医疗"],"医药",["PE","DCF","PS"],"成熟药企看PE/DCF；研发型企业需评估管线成功率。"),
    (["钢铁","水泥","建材"],"传统重资产/周期制造",["PB","EV/EBITDA","PE"],"关注产能周期、吨毛利、资产减值、负债和资本开支。")]
    for keys,sector,methods,why in groups:
        if any(k in n for k in keys):return sector,methods,why
    return "待确认行业",["PE","PB","PS","DCF"],"仅凭名称无法可靠判断商业模式。请核对主营业务后再确认估值方法。"

@app.get("/api/health")
def health(): return {"ok":True,"version":"1.0.0"}

@app.get("/api/search")
def search(q:str):
    q=q.strip()
    if not q:return {"items":[]}
    try:
        data=em_get("https://searchapi.eastmoney.com/api/suggest/get",{"input":q,"type":14,"count":10,"token":"","userId":""})
        raw=(data.get("QuotationCodeTable") or {}).get("Data") or data.get("data") or []
        return {"items":[{"code":str(x.get("Code") or x.get("code") or ""),"name":str(x.get("Name") or x.get("name") or "")} for x in raw if (x.get("Code") or x.get("code")) and (x.get("Name") or x.get("name"))]}
    except Exception as e:return {"items":[],"warning":f"搜索接口暂不可用：{str(e)[:120]}"}

@app.get("/api/stock/{code}")
def stock(code:str):
    code=re.sub(r"\\D","",code)
    if code in CACHE and time.time()-CACHE[code][0]<CACHE_SECONDS:return CACHE[code][1]
    try:secid=secid_for(code)
    except ValueError as e:raise HTTPException(400,str(e))
    try:
        js=em_get("https://push2.eastmoney.com/api/qt/stock/get",{"secid":secid,"fields":"f43,f57,f58,f116,f162,f164,f167,f170","fltt":2,"invt":2,"ut":"fa5fd1943c7b386f172d6893dbfba10b"})
        d=js.get("data")
        if not d:raise HTTPException(404,"未找到该股票，请检查代码")
        name=d.get("f58") or ""; sector,methods,why=classify(name,code)
        result={"code":code,"name":name,"sector":sector,"methods":methods,"why":why,"price":num(d.get("f43")),"change_pct":num(d.get("f170")),"pe_ttm":num(d.get("f162")),"pb":num(d.get("f167")),"roe":num(d.get("f164")),"market_cap":num(d.get("f116")),"revenue":None,"net_profit":None,"ps":None,"ev_ebitda":None,"source":"东方财富公开行情接口","as_of":time.strftime("%Y-%m-%d %H:%M:%S"),"data_note":"营收、净利润、PS、EBITDA及资源储量未在本版可靠取数，因此不编造。行情接口可能延迟、限流或变更。"}
        CACHE[code]=(time.time(),result);return result
    except HTTPException:raise
    except Exception as e:raise HTTPException(502,f"行情数据暂时获取失败：{str(e)[:160]}")

@app.get("/")
def home():return FileResponse(ROOT/"static"/"index.html")

app.mount("/",StaticFiles(directory=ROOT/"static",html=True),name="static")
