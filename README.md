# 股票估值助手 V1.0

A-share stock valuation advisor PWA. FastAPI backend and mobile-first installable web app.

## Run locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Open http://127.0.0.1:8000

## Deploy

Designed for Render Python web service. Build: `pip install -r requirements.txt`; Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`.

## Notes

Market data is fetched from Eastmoney public quote endpoints. Availability and fields may change. Missing fundamentals are shown as unavailable rather than fabricated. This tool is for research only, not investment advice.
