from fastapi import FastAPI, Request
from datetime import datetime, timezone

app = FastAPI()

signals_log = []


@app.get("/")
def home():
    return {
        "status": "online",
        "message": "Trading agent webhook is running",
        "signals_received": len(signals_log),
    }


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()

    signal = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }

    signals_log.append(signal)

    print("=== NEW TRADINGVIEW SIGNAL ===")
    print(signal)

    return {
        "status": "received",
        "message": "Signal received successfully",
        "signal": signal,
    }


@app.get("/signals")
def get_signals():
    return {
        "count": len(signals_log),
        "signals": signals_log[-20:],
    }
