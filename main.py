from fastapi import FastAPI, Request
from datetime import datetime, timezone
from pybit.unified_trading import HTTP

app = FastAPI()

signals_log = []

# Public Bybit connection. No API key needed for market data.
bybit = HTTP(testnet=False)


@app.get("/")
def home():
    return {
        "status": "online",
        "message": "Trading agent webhook is running",
        "signals_received": len(signals_log),
        "bybit_public_data": "enabled",
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


@app.get("/bybit/ticker/{symbol}")
def get_bybit_ticker(symbol: str = "BTCUSDT"):
    symbol = symbol.upper()

    response = bybit.get_tickers(
        category="linear",
        symbol=symbol,
    )

    return {
        "status": "ok",
        "symbol": symbol,
        "source": "bybit",
        "data": response,
    }


@app.get("/bybit/kline/{symbol}")
def get_bybit_kline(symbol: str = "BTCUSDT", interval: str = "60", limit: int = 50):
    symbol = symbol.upper()

    response = bybit.get_kline(
        category="linear",
        symbol=symbol,
        interval=interval,
        limit=limit,
    )

    candles = response.get("result", {}).get("list", [])

    # Bybit returns candles newest first, so reverse for normal oldest-to-newest order.
    candles = list(reversed(candles))

    parsed = []
    for c in candles:
        parsed.append({
            "start_time": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
            "turnover": float(c[6]),
        })

    return {
        "status": "ok",
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
        "candles": parsed,
    }
