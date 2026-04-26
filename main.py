from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from datetime import datetime, timezone
from pybit.unified_trading import HTTP

app = FastAPI()

signals_log = []

# Public Bybit connection. No API key needed for market data.
bybit = HTTP(testnet=False)


# =====================================================
# BASIC HELPERS
# =====================================================

def sma(values, length):
    if len(values) < length:
        return None
    return sum(values[-length:]) / length


def ema(values, length):
    if len(values) < length:
        return None

    k = 2 / (length + 1)
    ema_value = sum(values[:length]) / length

    for price in values[length:]:
        ema_value = price * k + ema_value * (1 - k)

    return ema_value


def rsi(values, length=14):
    if len(values) <= length:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))

    avg_gain = sum(gains[-length:]) / length
    avg_loss = sum(losses[-length:]) / length

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles, length=14):
    if len(candles) <= length:
        return None

    true_ranges = []

    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        true_ranges.append(tr)

    return sum(true_ranges[-length:]) / length


def get_bybit_candles(symbol="BTCUSDT", interval="60", limit=200):
    response = bybit.get_kline(
        category="linear",
        symbol=symbol,
        interval=interval,
        limit=limit,
    )

    raw_candles = response.get("result", {}).get("list", [])
    raw_candles = list(reversed(raw_candles))

    candles = []
    for c in raw_candles:
        candles.append({
            "start_time": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
            "turnover": float(c[6]),
        })

    return candles


def analyze_market(symbol="BTCUSDT", interval="60"):
    candles = get_bybit_candles(symbol=symbol, interval=interval, limit=200)

    closes = [c["close"] for c in candles]
    current_price = closes[-1]

    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)
    rsi14 = rsi(closes, 14)
    atr14 = atr(candles, 14)

    if ema50 is None or ema200 is None or rsi14 is None or atr14 is None:
        return {
            "status": "error",
            "message": "Not enough data for analysis",
        }

    distance_from_ema50_atr = abs(current_price - ema50) / atr14
    distance_from_ema50_pct = abs(current_price - ema50) / current_price * 100

    bullish_trend = current_price > ema50 > ema200
    bearish_trend = current_price < ema50 < ema200

    extended = distance_from_ema50_atr > 1.2

    if bullish_trend:
        trend = "BULLISH"
        bias = "LONG"
    elif bearish_trend:
        trend = "BEARISH"
        bias = "SHORT"
    else:
        trend = "NEUTRAL / CHOP"
        bias = "NONE"

    if trend == "NEUTRAL / CHOP":
        decision = "NO TRADE"
        action = "WAIT"
        reason = "Market is not in a clean trend. Avoid trading in chop."
        position = "$0"
        leverage = "None"
    elif extended:
        decision = "DO NOT CHASE"
        action = "WAIT FOR PULLBACK"
        reason = "Trend exists, but price is too extended from EMA 50. Entry is late."
        position = "$0"
        leverage = "None"
    elif bias == "LONG":
        decision = "LONG BIAS"
        action = "WAIT FOR LONG TRIGGER"
        reason = "Bullish trend detected. Wait for pullback to EMA/support and bullish confirmation."
        position = "Not yet"
        leverage = "Not yet"
    elif bias == "SHORT":
        decision = "SHORT BIAS"
        action = "WAIT FOR SHORT TRIGGER"
        reason = "Bearish trend detected. Wait for pullback to EMA/resistance and bearish confirmation."
        position = "Not yet"
        leverage = "Not yet"
    else:
        decision = "WAIT"
        action = "NO ACTION"
        reason = "No clear setup."
        position = "$0"
        leverage = "None"

    # Simple potential estimate using 2 ATR as expected move.
    if bias == "LONG":
        target = current_price + atr14 * 2
        potential_pct = (target - current_price) / current_price * 100
    elif bias == "SHORT":
        target = current_price - atr14 * 2
        potential_pct = (current_price - target) / current_price * 100
    else:
        target = None
        potential_pct = 0

    return {
        "status": "ok",
        "symbol": symbol,
        "interval": interval,
        "price": current_price,
        "ema50": ema50,
        "ema200": ema200,
        "rsi14": rsi14,
        "atr14": atr14,
        "distance_from_ema50_atr": distance_from_ema50_atr,
        "distance_from_ema50_pct": distance_from_ema50_pct,
        "trend": trend,
        "bias": bias,
        "extended": extended,
        "decision": decision,
        "action": action,
        "reason": reason,
        "target": target,
        "potential_pct": potential_pct,
        "suggested_position": position,
        "suggested_leverage": leverage,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


# =====================================================
# ROUTES
# =====================================================

@app.get("/")
def home():
    return {
        "status": "online",
        "message": "Trading agent webhook is running",
        "signals_received": len(signals_log),
        "bybit_public_data": "enabled",
        "dashboard": "/dashboard",
    }


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()

    signal = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }

    signals_log.append(signal)

    print("=== NEW SIGNAL ===")
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
    candles = get_bybit_candles(symbol=symbol, interval=interval, limit=limit)

    return {
        "status": "ok",
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
        "candles": candles,
    }


@app.get("/bybit/analyze/{symbol}")
def get_analysis(symbol: str = "BTCUSDT", interval: str = "60"):
    symbol = symbol.upper()
    return analyze_market(symbol=symbol, interval=interval)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(symbol: str = "BTCUSDT", interval: str = "60"):
    data = analyze_market(symbol=symbol.upper(), interval=interval)

    if data["status"] != "ok":
        return f"""
        <html>
        <body>
            <h1>Trading Agent Dashboard</h1>
            <p>Error: {data.get("message")}</p>
        </body>
        </html>
        """

    decision = data["decision"]

    if decision == "DO NOT CHASE":
        decision_color = "#7c3aed"
    elif decision == "NO TRADE":
        decision_color = "#6b7280"
    elif decision == "LONG BIAS":
        decision_color = "#16a34a"
    elif decision == "SHORT BIAS":
        decision_color = "#dc2626"
    else:
        decision_color = "#2563eb"

    def fmt(value, decimals=2):
        if value is None:
            return "-"
        return f"{value:,.{decimals}f}"

    html = f"""
    <html>
    <head>
        <title>Trading Agent Dashboard</title>
        <meta http-equiv="refresh" content="30">
        <style>
            body {{
                font-family: Arial, sans-serif;
                background: #0f172a;
                color: #e5e7eb;
                padding: 24px;
            }}
            .container {{
                max-width: 900px;
                margin: auto;
            }}
            .card {{
                background: #111827;
                border: 1px solid #374151;
                border-radius: 16px;
                padding: 20px;
                margin-bottom: 18px;
                box-shadow: 0 10px 25px rgba(0,0,0,0.25);
            }}
            .decision {{
                background: {decision_color};
                color: white;
                padding: 18px;
                border-radius: 14px;
                font-size: 28px;
                font-weight: bold;
                text-align: center;
            }}
            .grid {{
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 12px;
            }}
            .item {{
                background: #1f2937;
                padding: 12px;
                border-radius: 10px;
            }}
            .label {{
                color: #9ca3af;
                font-size: 13px;
            }}
            .value {{
                font-size: 20px;
                font-weight: bold;
                margin-top: 4px;
            }}
            .reason {{
                font-size: 18px;
                line-height: 1.5;
            }}
            a {{
                color: #60a5fa;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Trading Agent Dashboard</h1>

            <div class="card">
                <div class="decision">{data["decision"]}</div>
                <h2>Action: {data["action"]}</h2>
                <p class="reason">{data["reason"]}</p>
            </div>

            <div class="card">
                <h2>{data["symbol"]} / Timeframe: {data["interval"]}</h2>
                <div class="grid">
                    <div class="item">
                        <div class="label">Current Price</div>
                        <div class="value">{fmt(data["price"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">Trend</div>
                        <div class="value">{data["trend"]}</div>
                    </div>
                    <div class="item">
                        <div class="label">Bias</div>
                        <div class="value">{data["bias"]}</div>
                    </div>
                    <div class="item">
                        <div class="label">Extended From EMA50</div>
                        <div class="value">{'YES' if data["extended"] else 'NO'}</div>
                    </div>
                </div>
            </div>

            <div class="card">
                <h2>Market Metrics</h2>
                <div class="grid">
                    <div class="item">
                        <div class="label">EMA 50</div>
                        <div class="value">{fmt(data["ema50"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">EMA 200</div>
                        <div class="value">{fmt(data["ema200"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">RSI 14</div>
                        <div class="value">{fmt(data["rsi14"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">ATR 14</div>
                        <div class="value">{fmt(data["atr14"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">Distance From EMA50</div>
                        <div class="value">{fmt(data["distance_from_ema50_atr"])} ATR</div>
                    </div>
                    <div class="item">
                        <div class="label">Distance From EMA50 %</div>
                        <div class="value">{fmt(data["distance_from_ema50_pct"])}%</div>
                    </div>
                </div>
            </div>

            <div class="card">
                <h2>Potential Estimate</h2>
                <div class="grid">
                    <div class="item">
                        <div class="label">Estimated Target</div>
                        <div class="value">{fmt(data["target"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">Potential Move</div>
                        <div class="value">{fmt(data["potential_pct"])}%</div>
                    </div>
                    <div class="item">
                        <div class="label">Suggested Position</div>
                        <div class="value">{data["suggested_position"]}</div>
                    </div>
                    <div class="item">
                        <div class="label">Suggested Leverage</div>
                        <div class="value">{data["suggested_leverage"]}</div>
                    </div>
                </div>
            </div>

            <div class="card">
                <p>Updated UTC: {data["updated_at"]}</p>
                <p>Raw JSON: <a href="/bybit/analyze/{data["symbol"]}?interval={data["interval"]}">Open analysis JSON</a></p>
            </div>
        </div>
    </body>
    </html>
    """

    return html
