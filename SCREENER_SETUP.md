资金异动与起爆点选股模块（V1）

本模块集成在原有 FastAPI 股票估值助手中，不替换原有估值页面和 DCF/NAV 接口。

## 1. 本地运行

```bash
pip install -r requirements.txt
# Windows PowerShell
$env:TUSHARE_TOKEN="你的Tushare Token"
uvicorn main:app --reload
```

打开 http://127.0.0.1:8000/screener 。

## 2. Render 配置

在现有 Web Service 的 Environment 中添加：
- TUSHARE_TOKEN：你自己的 Tushare Token
- SCREENER_DB_PATH：建议设置为 /var/data/screener.sqlite3（只有在已挂载持久化磁盘时使用）

没有配置 Token 时，原估值页面仍可使用；选股 API 会返回明确的配置提示。不要把 Token 写进代码或提交到 GitHub。

Render 免费实例的本地文件系统不适合长期保存行情缓存；如果没有持久化磁盘，实例重启或重新部署后可能需要重新初始化历史行情。持久化磁盘可能产生费用，需在 Render 控制台自行确认。

## 3. 首次初始化

1. 打开 /screener。
2. 点击“检查数据状态”，确认 Token 已配置。
3. 点击“初始化 / 继续拉取20日”，每次获取20个交易日。
4. 重复点击，直到剩余交易日为0（最近750个交易日）。
5. 点击“开始全A股扫描”。
6. 点击“导出 Excel”可下载候选股票表。

每次交易日收盘后，可再次点击初始化以补充最新交易日，再执行扫描。行情缓存按股票代码和交易日去重。

## 4. 模块与 API

- POST /api/screener/backfill：增量拉取历史行情，body 示例 {"batch":20}。
- POST /api/screener/scan：扫描缓存行情，body 示例 {"force":true}。
- POST /api/screener/backtest：历史信号回测，支持 start_date、end_date、horizon、min_score。
- GET /api/screener/export：导出最近一次扫描结果。
- GET /api/screener/status：查看 Token 与本地缓存状态。

## 5. 原公式和评分

原始通达信公式已翻译为 Python，包括多周期 HHV/LLV、EMA、SMA(X,N,M)、VarA、VarD、30日新低条件及最终资金入场 EMA。代码在 screener.py 的 calc_original_formula()。

六维评分权重：

| 维度 | 权重 |
|---|---:|
| 原始资金入场公式与资金异动 | 20 |
| 趋势结构 | 20 |
| 成交量质量 | 15 |
| 筹码区间与压力位代理 | 15 |
| 市场横截面相对强度 | 15 |
| 基本面/估值风险代理 | 15 |

三个阶段：潜伏观察、启动准备、突破确认。阶段和评分用于排序、观察，不代表上涨概率。

## 6. 数据权限和限制

- 行情主数据使用 Tushare daily、trade_cal、stock_basic。
- daily_basic 用于 PE(TTM)、PB、换手率、量比、市值等辅助字段。若账号无相应接口权限，程序保留价格类筛选，缺失字段不伪造。
- daily、daily_basic、stock_basic 使用分页，避免默认返回条数导致全市场数据被截断。
- 本版使用 Tushare 未复权日线。除权除息会影响长周期价格指标；应与通达信相同复权设置对照后再使用。
- 原公式的 SMA 使用通达信递推形式；零分母按0处理。需选择具体股票与通达信逐项比对验证边界差异。
- 当前基本面维度只是 PE/PB 与上市风险代理，不是完整财务质量分析；未对全市场逐家公司拉取资产负债表、现金流和 ROE。
- 相对强度是相对于当日有行情股票的横截面平均收益代理，不是行业指数或沪深300的正式超额收益。
- 回测采用信号日收盘后、下一交易日开盘理论入场；未扣佣金、印花税、滑点、涨跌停成交限制，且存在当前上市股票池带来的幸存者偏差。历史结果不代表未来。

## 7. 安全

Token 只保存在 Render 环境变量或本地环境变量中。不要通过聊天、源码、截图或公开仓库分享 Token。
