from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from datetime import datetime, timezone
from pybit.unified_trading import HTTP
from openai import OpenAI
import os
import json
import html
import time

app = FastAPI()
ai_brief_cache = {}
signals_log = []

# Public Bybit connection. No API key needed for market data.
bybit = HTTP(testnet=False)

# OpenAI client.
# The key must be stored in Render Environment Variables as OPENAI_API_KEY.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Cache AI brief to avoid spending money every dashboard refresh.
AI_CACHE_TTL_SECONDS = int(os.getenv("AI_CACHE_TTL_SECONDS", "600"))
ai_brief_cache = {}


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


# =====================================================
# SMART MONEY CONCEPT HELPERS
# =====================================================

def find_swing_highs_lows(candles, left=3, right=3):
    swing_highs = []
    swing_lows = []

    for i in range(left, len(candles) - right):
        current_high = candles[i]["high"]
        current_low = candles[i]["low"]

        is_high = True
        is_low = True

        for j in range(i - left, i + right + 1):
            if j == i:
                continue

            if candles[j]["high"] >= current_high:
                is_high = False

            if candles[j]["low"] <= current_low:
                is_low = False

        if is_high:
            swing_highs.append({
                "index": i,
                "price": current_high,
                "time": candles[i]["start_time"],
            })

        if is_low:
            swing_lows.append({
                "index": i,
                "price": current_low,
                "time": candles[i]["start_time"],
            })

    return swing_highs, swing_lows


def detect_liquidity_sweep(candles, swing_highs, swing_lows):
    last = candles[-1]
    close = last["close"]
    high = last["high"]
    low = last["low"]

    recent_high = swing_highs[-1]["price"] if swing_highs else None
    recent_low = swing_lows[-1]["price"] if swing_lows else None

    high_sweep = False
    low_sweep = False

    if recent_high is not None:
        high_sweep = high > recent_high and close < recent_high

    if recent_low is not None:
        low_sweep = low < recent_low and close > recent_low

    return {
        "recent_high": recent_high,
        "recent_low": recent_low,
        "high_sweep": high_sweep,
        "low_sweep": low_sweep,
    }


def detect_market_structure(candles, swing_highs, swing_lows):
    close = candles[-1]["close"]

    last_high = swing_highs[-1]["price"] if swing_highs else None
    last_low = swing_lows[-1]["price"] if swing_lows else None

    prev_high = swing_highs[-2]["price"] if len(swing_highs) >= 2 else None
    prev_low = swing_lows[-2]["price"] if len(swing_lows) >= 2 else None

    bos_bullish = last_high is not None and close > last_high
    bos_bearish = last_low is not None and close < last_low

    structure = "NEUTRAL"

    if last_high is not None and prev_high is not None and last_low is not None and prev_low is not None:
        if last_high > prev_high and last_low > prev_low:
            structure = "BULLISH"
        elif last_high < prev_high and last_low < prev_low:
            structure = "BEARISH"
        else:
            structure = "MIXED / TRANSITION"

    return {
        "structure": structure,
        "last_swing_high": last_high,
        "last_swing_low": last_low,
        "bos_bullish": bos_bullish,
        "bos_bearish": bos_bearish,
    }


def detect_fvg(candles):
    bullish_fvgs = []
    bearish_fvgs = []

    for i in range(2, len(candles)):
        c1 = candles[i - 2]
        c3 = candles[i]

        # Bullish imbalance: current low is above candle-2 high.
        if c3["low"] > c1["high"]:
            bullish_fvgs.append({
                "index": i,
                "low": c1["high"],
                "high": c3["low"],
                "mid": (c1["high"] + c3["low"]) / 2,
            })

        # Bearish imbalance: current high is below candle-2 low.
        if c3["high"] < c1["low"]:
            bearish_fvgs.append({
                "index": i,
                "low": c3["high"],
                "high": c1["low"],
                "mid": (c3["high"] + c1["low"]) / 2,
            })

    price = candles[-1]["close"]

    last_bullish_fvg = bullish_fvgs[-1] if bullish_fvgs else None
    last_bearish_fvg = bearish_fvgs[-1] if bearish_fvgs else None

    inside_bullish_fvg = False
    inside_bearish_fvg = False

    if last_bullish_fvg:
        inside_bullish_fvg = last_bullish_fvg["low"] <= price <= last_bullish_fvg["high"]

    if last_bearish_fvg:
        inside_bearish_fvg = last_bearish_fvg["low"] <= price <= last_bearish_fvg["high"]

    return {
        "bullish_fvg_active": last_bullish_fvg is not None,
        "bearish_fvg_active": last_bearish_fvg is not None,
        "inside_bullish_fvg": inside_bullish_fvg,
        "inside_bearish_fvg": inside_bearish_fvg,
        "last_bullish_fvg": last_bullish_fvg,
        "last_bearish_fvg": last_bearish_fvg,
    }


def detect_order_blocks(candles, lookback=40):
    price = candles[-1]["close"]

    bullish_ob = None
    bearish_ob = None

    recent = candles[-lookback:]

    for i in range(2, len(recent)):
        prev = recent[i - 1]
        curr = recent[i]

        # Bullish OB approximation:
        # last bearish candle before strong bullish displacement.
        if prev["close"] < prev["open"] and curr["close"] > curr["open"]:
            body = abs(curr["close"] - curr["open"])
            candle_range = curr["high"] - curr["low"]

            if candle_range > 0 and body / candle_range > 0.55 and curr["close"] > prev["high"]:
                bullish_ob = {
                    "low": prev["low"],
                    "high": prev["high"],
                    "mid": (prev["low"] + prev["high"]) / 2,
                }

        # Bearish OB approximation:
        # last bullish candle before strong bearish displacement.
        if prev["close"] > prev["open"] and curr["close"] < curr["open"]:
            body = abs(curr["close"] - curr["open"])
            candle_range = curr["high"] - curr["low"]

            if candle_range > 0 and body / candle_range > 0.55 and curr["close"] < prev["low"]:
                bearish_ob = {
                    "low": prev["low"],
                    "high": prev["high"],
                    "mid": (prev["low"] + prev["high"]) / 2,
                }

    inside_bullish_ob = False
    inside_bearish_ob = False

    if bullish_ob:
        inside_bullish_ob = bullish_ob["low"] <= price <= bullish_ob["high"]

    if bearish_ob:
        inside_bearish_ob = bearish_ob["low"] <= price <= bearish_ob["high"]

    return {
        "bullish_ob_active": bullish_ob is not None,
        "bearish_ob_active": bearish_ob is not None,
        "inside_bullish_ob": inside_bullish_ob,
        "inside_bearish_ob": inside_bearish_ob,
        "bullish_ob": bullish_ob,
        "bearish_ob": bearish_ob,
    }


def detect_premium_discount(candles, lookback=100):
    recent = candles[-lookback:]

    range_high = max(c["high"] for c in recent)
    range_low = min(c["low"] for c in recent)
    equilibrium = (range_high + range_low) / 2
    price = candles[-1]["close"]

    if price < equilibrium:
        zone = "DISCOUNT"
    elif price > equilibrium:
        zone = "PREMIUM"
    else:
        zone = "EQUILIBRIUM"

    return {
        "range_high": range_high,
        "range_low": range_low,
        "equilibrium": equilibrium,
        "pd_zone": zone,
    }


def calculate_smc_score(bias, liquidity, structure, fvg, ob, pd):
    score = 0
    notes = []

    if bias == "LONG":
        if liquidity["low_sweep"]:
            score += 12
            notes.append("Bullish liquidity sweep detected")

        if structure["bos_bullish"]:
            score += 12
            notes.append("Bullish BOS detected")

        if structure["structure"] == "BULLISH":
            score += 8
            notes.append("Market structure is bullish")

        if fvg["inside_bullish_fvg"]:
            score += 8
            notes.append("Price is inside bullish FVG")

        if ob["inside_bullish_ob"]:
            score += 8
            notes.append("Price is inside bullish order block")

        if pd["pd_zone"] == "DISCOUNT":
            score += 10
            notes.append("Price is in discount zone")

    elif bias == "SHORT":
        if liquidity["high_sweep"]:
            score += 12
            notes.append("Bearish liquidity sweep detected")

        if structure["bos_bearish"]:
            score += 12
            notes.append("Bearish BOS detected")

        if structure["structure"] == "BEARISH":
            score += 8
            notes.append("Market structure is bearish")

        if fvg["inside_bearish_fvg"]:
            score += 8
            notes.append("Price is inside bearish FVG")

        if ob["inside_bearish_ob"]:
            score += 8
            notes.append("Price is inside bearish order block")

        if pd["pd_zone"] == "PREMIUM":
            score += 10
            notes.append("Price is in premium zone")

    return {
        "smc_score": min(score, 50),
        "smc_notes": notes,
    }


# =====================================================
# AI MARKET BRIEF
# =====================================================

def compact_market_data_for_ai(data):
    return {
        "symbol": data.get("symbol"),
        "timeframe": data.get("interval"),
        "price": round(data.get("price", 0), 2),
        "decision": data.get("decision"),
        "action": data.get("action"),
        "reason": data.get("reason"),
        "trend": data.get("trend"),
        "bias": data.get("bias"),
        "extended_from_ema50": data.get("extended"),
        "ema50": round(data.get("ema50", 0), 2),
        "ema200": round(data.get("ema200", 0), 2),
        "rsi14": round(data.get("rsi14", 0), 2),
        "atr14": round(data.get("atr14", 0), 2),
        "distance_from_ema50_atr": round(data.get("distance_from_ema50_atr", 0), 2),
        "distance_from_ema50_pct": round(data.get("distance_from_ema50_pct", 0), 2),
        "volume_strong": data.get("volume_strong"),
        "market_structure": data.get("market_structure"),
        "pd_zone": data.get("pd_zone"),
        "bos_bullish": data.get("bos_bullish"),
        "bos_bearish": data.get("bos_bearish"),
        "low_sweep": data.get("low_sweep"),
        "high_sweep": data.get("high_sweep"),
        "inside_bullish_fvg": data.get("inside_bullish_fvg"),
        "inside_bearish_fvg": data.get("inside_bearish_fvg"),
        "inside_bullish_ob": data.get("inside_bullish_ob"),
        "inside_bearish_ob": data.get("inside_bearish_ob"),
        "smc_score": data.get("smc_score"),
        "technical_score": data.get("technical_score"),
        "risk_score": data.get("risk_score"),
        "total_score": data.get("total_score"),
        "entry": data.get("entry"),
        "stop_loss": data.get("stop_loss"),
        "tp1": data.get("tp1"),
        "tp2": data.get("tp2"),
        "tp3": data.get("tp3"),
        "suggested_position": data.get("suggested_position"),
        "suggested_leverage": data.get("suggested_leverage"),
    }


def generate_ai_market_brief(data):
    cache_key = f"{data.get('symbol')}:{data.get('interval')}:{data.get('decision')}:{data.get('total_score')}:{data.get('smc_score')}"
    now = time.time()

    cached = ai_brief_cache.get(cache_key)
    if cached and now - cached["created_at"] < AI_CACHE_TTL_SECONDS:
        return {
            "status": "cached",
            "brief": cached["brief"],
            "updated_at": cached["updated_at"],
            "model": cached["model"],
        }

    if not openai_client:
        return {
            "status": "disabled",
            "brief": (
                "AI Market Brief is not available because OPENAI_API_KEY is not configured. "
                "Check Render Environment Variables."
            ),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "model": None,
        }

    market_data = compact_market_data_for_ai(data)

    system_prompt = """
You are a cautious crypto trading analysis assistant.

You do NOT give financial advice.
You do NOT guarantee profits.
You do NOT encourage high leverage.
You analyze only the provided structured market data.

Your job:
1. Explain the current market situation clearly.
2. Identify whether the setup is actionable or should be watched.
3. Explain what confirmation is missing.
4. Describe what to wait for.
5. Define invalidation.
6. Give a risk warning.

Use concise trader language.
Be skeptical.
Do not overhype the setup.
If SMC confirmation is weak, say it clearly.
If the agent says WAIT, do not turn it into an entry signal.

Return the answer in this exact format:

Market Scenario:
...

Entry Quality:
...

What To Wait For:
...

Invalidation:
...

Risk Warning:
...
"""

    user_prompt = f"""
Analyze this trading-agent output.

Market data JSON:
{json.dumps(market_data, indent=2)}

Important rules:
- If decision is LONG BIAS or SHORT BIAS, treat it as watch-only, not an entry.
- If SMC score is below 30/50, say SMC confirmation is not strong enough for a smart signal.
- If decision is SMART LONG or SMART SHORT, still mention that the trade requires risk control and confirmation.
- Keep it practical and concise.
"""

    try:
        response = openai_client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            max_output_tokens=700,
        )

        brief = response.output_text.strip()

        result = {
            "status": "ok",
            "brief": brief,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "model": OPENAI_MODEL,
        }

        ai_brief_cache[cache_key] = {
            "brief": brief,
            "created_at": now,
            "updated_at": result["updated_at"],
            "model": OPENAI_MODEL,
        }

        return result

    except Exception as e:
        return {
            "status": "error",
            "brief": f"AI Market Brief error: {str(e)}",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "model": OPENAI_MODEL,
        }


# =====================================================
# MAIN ANALYSIS ENGINE
# =====================================================

def analyze_market(symbol="BTCUSDT", interval="60"):
    candles = get_bybit_candles(symbol=symbol, interval=interval, limit=250)

    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    current_price = closes[-1]

    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)
    rsi14 = rsi(closes, 14)
    atr14 = atr(candles, 14)
    volume_sma20 = sma(volumes, 20)

    if ema50 is None or ema200 is None or rsi14 is None or atr14 is None or volume_sma20 is None:
        return {
            "status": "error",
            "message": "Not enough data for analysis",
        }

    distance_from_ema50_atr = abs(current_price - ema50) / atr14
    distance_from_ema50_pct = abs(current_price - ema50) / current_price * 100

    bullish_trend = current_price > ema50 > ema200
    bearish_trend = current_price < ema50 < ema200

    extended = distance_from_ema50_atr > 1.2
    volume_strong = volumes[-1] > volume_sma20

    if bullish_trend:
        trend = "BULLISH"
        bias = "LONG"
    elif bearish_trend:
        trend = "BEARISH"
        bias = "SHORT"
    else:
        trend = "NEUTRAL / CHOP"
        bias = "NONE"

    # SMC analysis
    swing_highs, swing_lows = find_swing_highs_lows(candles)
    liquidity = detect_liquidity_sweep(candles, swing_highs, swing_lows)
    structure = detect_market_structure(candles, swing_highs, swing_lows)
    fvg = detect_fvg(candles)
    ob = detect_order_blocks(candles)
    pd = detect_premium_discount(candles)

    smc = calculate_smc_score(bias, liquidity, structure, fvg, ob, pd)

    # Technical score: max 35
    technical_score = 0

    if bias == "LONG":
        if bullish_trend:
            technical_score += 15
        if 45 <= rsi14 <= 72:
            technical_score += 8
        if not extended:
            technical_score += 7
        if volume_strong:
            technical_score += 5

    elif bias == "SHORT":
        if bearish_trend:
            technical_score += 15
        if 28 <= rsi14 <= 55:
            technical_score += 8
        if not extended:
            technical_score += 7
        if volume_strong:
            technical_score += 5

    # Risk score: max 15
    risk_score = 0

    if atr14 > 0:
        if distance_from_ema50_atr <= 0.8:
            risk_score = 15
        elif distance_from_ema50_atr <= 1.2:
            risk_score = 10
        elif distance_from_ema50_atr <= 1.8:
            risk_score = 6
        else:
            risk_score = 3

    total_score = smc["smc_score"] + technical_score + risk_score

    # Entry / SL / TP estimate
    if bias == "LONG":
        entry = current_price
        stop_loss = current_price - atr14 * 1.5
        tp1 = current_price + atr14 * 1.5
        tp2 = current_price + atr14 * 2.5
        tp3 = current_price + atr14 * 4.0
        target = tp2
        potential_pct = (target - current_price) / current_price * 100

    elif bias == "SHORT":
        entry = current_price
        stop_loss = current_price + atr14 * 1.5
        tp1 = current_price - atr14 * 1.5
        tp2 = current_price - atr14 * 2.5
        tp3 = current_price - atr14 * 4.0
        target = tp2
        potential_pct = (current_price - target) / current_price * 100

    else:
        entry = None
        stop_loss = None
        tp1 = None
        tp2 = None
        tp3 = None
        target = None
        potential_pct = 0

    # Decision logic
    min_smc_for_smart_signal = 30
    min_total_for_smart_signal = 75

    if trend == "NEUTRAL / CHOP":
        decision = "NO TRADE"
        action = "WAIT"
        reason = "Market is not in a clean trend. Avoid trading in chop."
        position = "$0"
        leverage = "None"

    elif extended and smc["smc_score"] < min_smc_for_smart_signal:
        decision = "DO NOT CHASE"
        action = "WAIT FOR PULLBACK"
        reason = "Trend exists, but price is extended from EMA50 and SMC confirmation is not strong enough."

        if bias == "LONG":
            if pd["pd_zone"] == "PREMIUM":
                reason += " Price is in premium zone; buying here is expensive."
            reason += " Wait for pullback into discount, bullish OB/FVG, liquidity sweep, or bullish rejection."

        elif bias == "SHORT":
            if pd["pd_zone"] == "DISCOUNT":
                reason += " Price is in discount zone; shorting here is late."
            reason += " Wait for pullback into premium, bearish OB/FVG, liquidity sweep, or bearish rejection."

        position = "$0"
        leverage = "None"

    elif bias == "LONG" and total_score >= min_total_for_smart_signal and smc["smc_score"] >= min_smc_for_smart_signal:
        decision = "SMART LONG"
        action = "PREPARE LONG"
        reason = "Bullish trend with acceptable technical, risk, and SMC confirmation."

        if smc["smc_notes"]:
            reason += " " + "; ".join(smc["smc_notes"]) + "."

        position = "Small test position only"
        leverage = "Low / conservative"

    elif bias == "SHORT" and total_score >= min_total_for_smart_signal and smc["smc_score"] >= min_smc_for_smart_signal:
        decision = "SMART SHORT"
        action = "PREPARE SHORT"
        reason = "Bearish trend with acceptable technical, risk, and SMC confirmation."

        if smc["smc_notes"]:
            reason += " " + "; ".join(smc["smc_notes"]) + "."

        position = "Small test position only"
        leverage = "Low / conservative"

    elif bias == "LONG" and total_score >= min_total_for_smart_signal and smc["smc_score"] < min_smc_for_smart_signal:
        decision = "LONG BIAS"
        action = "WAIT FOR SMC TRIGGER"
        reason = (
            "Bullish trend and risk conditions are good, but SMC confirmation is still not strong enough. "
            "Do not chase. Wait for low sweep, bullish BOS, reaction from bullish FVG/OB, or pullback into discount."
        )
        position = "Not yet"
        leverage = "Not yet"

    elif bias == "SHORT" and total_score >= min_total_for_smart_signal and smc["smc_score"] < min_smc_for_smart_signal:
        decision = "SHORT BIAS"
        action = "WAIT FOR SMC TRIGGER"
        reason = (
            "Bearish trend and risk conditions are good, but SMC confirmation is still not strong enough. "
            "Do not chase. Wait for high sweep, bearish BOS, reaction from bearish FVG/OB, or pullback into premium."
        )
        position = "Not yet"
        leverage = "Not yet"

    elif bias == "LONG":
        decision = "LONG BIAS"
        action = "WAIT FOR LONG TRIGGER"
        reason = (
            "Bullish trend detected, but full confirmation is not strong enough yet. "
            "Wait for pullback, low sweep, bullish OB/FVG reaction, or better risk location."
        )
        position = "Not yet"
        leverage = "Not yet"

    elif bias == "SHORT":
        decision = "SHORT BIAS"
        action = "WAIT FOR SHORT TRIGGER"
        reason = (
            "Bearish trend detected, but full confirmation is not strong enough yet. "
            "Wait for pullback, high sweep, bearish OB/FVG reaction, or better risk location."
        )
        position = "Not yet"
        leverage = "Not yet"

    else:
        decision = "WAIT"
        action = "NO ACTION"
        reason = "No clear setup."
        position = "$0"
        leverage = "None"

    return {
        "status": "ok",
        "symbol": symbol,
        "interval": interval,
        "price": current_price,

        "ema50": ema50,
        "ema200": ema200,
        "rsi14": rsi14,
        "atr14": atr14,
        "volume": volumes[-1],
        "volume_sma20": volume_sma20,
        "volume_strong": volume_strong,

        "distance_from_ema50_atr": distance_from_ema50_atr,
        "distance_from_ema50_pct": distance_from_ema50_pct,

        "trend": trend,
        "bias": bias,
        "extended": extended,

        "market_structure": structure["structure"],
        "bos_bullish": structure["bos_bullish"],
        "bos_bearish": structure["bos_bearish"],
        "last_swing_high": structure["last_swing_high"],
        "last_swing_low": structure["last_swing_low"],

        "recent_high": liquidity["recent_high"],
        "recent_low": liquidity["recent_low"],
        "high_sweep": liquidity["high_sweep"],
        "low_sweep": liquidity["low_sweep"],

        "pd_zone": pd["pd_zone"],
        "range_high": pd["range_high"],
        "range_low": pd["range_low"],
        "equilibrium": pd["equilibrium"],

        "bullish_fvg_active": fvg["bullish_fvg_active"],
        "bearish_fvg_active": fvg["bearish_fvg_active"],
        "inside_bullish_fvg": fvg["inside_bullish_fvg"],
        "inside_bearish_fvg": fvg["inside_bearish_fvg"],

        "bullish_ob_active": ob["bullish_ob_active"],
        "bearish_ob_active": ob["bearish_ob_active"],
        "inside_bullish_ob": ob["inside_bullish_ob"],
        "inside_bearish_ob": ob["inside_bearish_ob"],

        "smc_score": smc["smc_score"],
        "smc_notes": smc["smc_notes"],
        "technical_score": technical_score,
        "risk_score": risk_score,
        "total_score": total_score,

        "decision": decision,
        "action": action,
        "reason": reason,

        "entry": entry,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
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
        "openai_key_configured": OPENAI_API_KEY is not None,
        "ai_model": OPENAI_MODEL,
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
def get_analysis(symbol: str = "BTCUSDT", interval: str = "60", ai: bool = False):
    symbol = symbol.upper()
    data = analyze_market(symbol=symbol, interval=interval)

    if ai and data.get("status") == "ok":
        data["ai_market_brief"] = generate_ai_market_brief(data)

    return data


@app.get("/bybit/ai/{symbol}")
def get_ai_analysis(symbol: str = "BTCUSDT", interval: str = "60"):
    symbol = symbol.upper()
    data = analyze_market(symbol=symbol, interval=interval)

    if data.get("status") != "ok":
        return data

    return {
        "status": "ok",
        "symbol": symbol,
        "interval": interval,
        "analysis": data,
        "ai_market_brief": generate_ai_market_brief(data),
    }


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

    ai_brief = generate_ai_market_brief(data)

    decision = data["decision"]

    if decision == "DO NOT CHASE":
        decision_color = "#7c3aed"
    elif decision == "NO TRADE":
        decision_color = "#6b7280"
    elif decision == "SMART LONG":
        decision_color = "#16a34a"
    elif decision == "SMART SHORT":
        decision_color = "#dc2626"
    elif decision == "LONG BIAS":
        decision_color = "#15803d"
    elif decision == "SHORT BIAS":
        decision_color = "#b91c1c"
    else:
        decision_color = "#2563eb"

    def fmt(value, decimals=2):
        if value is None:
            return "-"
        return f"{value:,.{decimals}f}"

    def yes_no(value):
        return "YES" if value else "NO"

    def text_to_html(value):
        safe = html.escape(value or "")
        return safe.replace("\n", "<br>")

    smc_notes_html = ""

    if data["smc_notes"]:
        smc_notes_html = "<ul>"
        for note in data["smc_notes"]:
            smc_notes_html += f"<li>{html.escape(note)}</li>"
        smc_notes_html += "</ul>"
    else:
        smc_notes_html = "<p>No strong SMC confirmation yet.</p>"

    ai_status = ai_brief.get("status")
    ai_model = ai_brief.get("model") or "not configured"
    ai_updated = ai_brief.get("updated_at")
    ai_text = text_to_html(ai_brief.get("brief", ""))

    html_page = f"""
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
                max-width: 980px;
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
            .grid3 {{
                display: grid;
                grid-template-columns: repeat(3, 1fr);
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
            .score {{
                font-size: 24px;
                font-weight: bold;
            }}
            .ai {{
                background: #0b1220;
                border-left: 4px solid #38bdf8;
                padding: 16px;
                border-radius: 12px;
                line-height: 1.65;
                font-size: 16px;
            }}
            .small {{
                color: #9ca3af;
                font-size: 13px;
            }}
            a {{
                color: #60a5fa;
            }}
            ul {{
                line-height: 1.7;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Trading Agent Dashboard</h1>

            <div class="card">
                <div class="decision">{data["decision"]}</div>
                <h2>Action: {data["action"]}</h2>
                <p class="reason">{html.escape(data["reason"])}</p>
            </div>

            <div class="card">
                <h2>AI Market Brief</h2>
                <div class="ai">{ai_text}</div>
                <p class="small">AI status: {html.escape(str(ai_status))} | Model: {html.escape(str(ai_model))} | Updated UTC: {html.escape(str(ai_updated))}</p>
            </div>

            <div class="card">
                <h2>Signal Quality Score</h2>
                <div class="grid3">
                    <div class="item">
                        <div class="label">SMC Score</div>
                        <div class="score">{data["smc_score"]} / 50</div>
                    </div>
                    <div class="item">
                        <div class="label">Technical Score</div>
                        <div class="score">{data["technical_score"]} / 35</div>
                    </div>
                    <div class="item">
                        <div class="label">Risk Score</div>
                        <div class="score">{data["risk_score"]} / 15</div>
                    </div>
                    <div class="item">
                        <div class="label">Total Score</div>
                        <div class="score">{data["total_score"]} / 100</div>
                    </div>
                </div>
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
                        <div class="value">{yes_no(data["extended"])}</div>
                    </div>
                </div>
            </div>

            <div class="card">
                <h2>Smart Money Concepts</h2>
                <div class="grid">
                    <div class="item">
                        <div class="label">Market Structure</div>
                        <div class="value">{data["market_structure"]}</div>
                    </div>
                    <div class="item">
                        <div class="label">PD Zone</div>
                        <div class="value">{data["pd_zone"]}</div>
                    </div>
                    <div class="item">
                        <div class="label">Bullish BOS</div>
                        <div class="value">{yes_no(data["bos_bullish"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">Bearish BOS</div>
                        <div class="value">{yes_no(data["bos_bearish"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">Low Sweep</div>
                        <div class="value">{yes_no(data["low_sweep"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">High Sweep</div>
                        <div class="value">{yes_no(data["high_sweep"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">Inside Bullish FVG</div>
                        <div class="value">{yes_no(data["inside_bullish_fvg"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">Inside Bearish FVG</div>
                        <div class="value">{yes_no(data["inside_bearish_fvg"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">Inside Bullish OB</div>
                        <div class="value">{yes_no(data["inside_bullish_ob"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">Inside Bearish OB</div>
                        <div class="value">{yes_no(data["inside_bearish_ob"])}</div>
                    </div>
                </div>

                <h3>SMC Notes</h3>
                {smc_notes_html}
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
                    <div class="item">
                        <div class="label">Volume Strong</div>
                        <div class="value">{yes_no(data["volume_strong"])}</div>
                    </div>
                </div>
            </div>

            <div class="card">
                <h2>Trade Plan Estimate</h2>
                <div class="grid">
                    <div class="item">
                        <div class="label">Entry Estimate</div>
                        <div class="value">{fmt(data["entry"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">Stop Loss</div>
                        <div class="value">{fmt(data["stop_loss"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">TP1</div>
                        <div class="value">{fmt(data["tp1"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">TP2</div>
                        <div class="value">{fmt(data["tp2"])}</div>
                    </div>
                    <div class="item">
                        <div class="label">TP3</div>
                        <div class="value">{fmt(data["tp3"])}</div>
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
                <p>Raw JSON: <a href="/bybit/analyze/{data["symbol"]}?interval={data["interval"]}&ai=true">Open analysis JSON with AI</a></p>
                <p>AI JSON: <a href="/bybit/ai/{data["symbol"]}?interval={data["interval"]}">Open AI analysis JSON</a></p>
            </div>
        </div>
    </body>
    </html>
    """

    return html_page
