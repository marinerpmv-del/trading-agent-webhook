TRADING AGENT CODE UPDATE — EARLY / CONFIRMED SIGNAL LOGIC
==============================================================

Repository:
https://github.com/marinerpmv-del/trading-agent-webhook

Main file:
https://github.com/marinerpmv-del/trading-agent-webhook/blob/main/main.py

Goal:
Fix the current issue where the agent shows SMC = 0 and Tech = 0 during NEUTRAL / CHOP conditions and therefore misses early bounce/reversal entries.

The new logic allows:
- WATCH LONG
- WATCH SHORT
- EARLY LONG
- EARLY SHORT
- CONFIRMED LONG
- CONFIRMED SHORT
- NO TRADE
- DO NOT CHASE

It also adds:
- directional LONG and SHORT scoring
- early bounce/reversal detection
- blocked_by / missing confirmation explanations
- long_total / short_total comparison


==============================================================
1. REPLACE calculate_smc_score()
==============================================================

Find this function in main.py:

def calculate_smc_score(bias, liquidity, structure, fvg, ob, pd):

Replace the full old function with this:


def calculate_smc_score(bias, liquidity, structure, fvg, ob, pd, candles=None, atr14=None, ema50=None, ema200=None):
    """
    Directional SMC score.
    Important change:
    This function can score LONG and SHORT even when the general EMA trend is neutral/choppy.
    That allows the agent to detect early bounce/reversal setups.
    """

    score = 0
    notes = []

    current_price = candles[-1]["close"] if candles else None
    last_candle = candles[-1] if candles else None
    prev_candle = candles[-2] if candles and len(candles) >= 2 else None

    bullish_reaction = False
    bearish_reaction = False

    if last_candle and prev_candle:
        bullish_reaction = (
            last_candle["close"] > last_candle["open"]
            and last_candle["close"] > prev_candle["close"]
        )

        bearish_reaction = (
            last_candle["close"] < last_candle["open"]
            and last_candle["close"] < prev_candle["close"]
        )

    near_ema200_long = False
    near_ema200_short = False

    if current_price is not None and ema200 is not None and atr14 is not None and atr14 > 0:
        near_ema200_long = abs(current_price - ema200) <= atr14 * 0.8 and current_price >= ema200
        near_ema200_short = abs(current_price - ema200) <= atr14 * 0.8 and current_price <= ema200

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
        elif structure["structure"] == "MIXED / TRANSITION":
            score += 4
            notes.append("Market structure is mixed but may be transitioning")

        if fvg["inside_bullish_fvg"]:
            score += 8
            notes.append("Price is reacting inside bullish FVG")

        if ob["inside_bullish_ob"]:
            score += 8
            notes.append("Price is reacting inside bullish order block")

        if pd["pd_zone"] == "DISCOUNT":
            score += 10
            notes.append("Price is in discount zone")

        if bullish_reaction:
            score += 5
            notes.append("Bullish reaction candle detected")

        if near_ema200_long:
            score += 5
            notes.append("Price is holding near/above EMA200 support")

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
        elif structure["structure"] == "MIXED / TRANSITION":
            score += 4
            notes.append("Market structure is mixed but may be transitioning")

        if fvg["inside_bearish_fvg"]:
            score += 8
            notes.append("Price is reacting inside bearish FVG")

        if ob["inside_bearish_ob"]:
            score += 8
            notes.append("Price is reacting inside bearish order block")

        if pd["pd_zone"] == "PREMIUM":
            score += 10
            notes.append("Price is in premium zone")

        if bearish_reaction:
            score += 5
            notes.append("Bearish reaction candle detected")

        if near_ema200_short:
            score += 5
            notes.append("Price is rejecting near/below EMA200 resistance")

    return {
        "smc_score": min(score, 50),
        "smc_notes": notes,
    }


==============================================================
2. ADD calculate_technical_score()
==============================================================

Insert this function immediately after calculate_smc_score():


def calculate_technical_score(bias, current_price, ema50, ema200, rsi14, extended, volume_strong):
    """
    Directional technical score.
    This allows LONG/SHORT scoring even if the market is not in a perfect EMA trend.
    """

    score = 0
    notes = []

    if bias == "LONG":
        if current_price > ema200:
            score += 8
            notes.append("Price is above EMA200")

        if ema50 > ema200:
            score += 7
            notes.append("EMA50 is above EMA200")

        if current_price > ema50:
            score += 6
            notes.append("Price is above EMA50")
        elif current_price > ema200:
            score += 3
            notes.append("Price is below EMA50 but still above EMA200")

        if 40 <= rsi14 <= 68:
            score += 8
            notes.append("RSI is in acceptable long range")
        elif 35 <= rsi14 < 40:
            score += 4
            notes.append("RSI is weak but recovering")

        if not extended:
            score += 4
            notes.append("Price is not overextended from EMA50")

        if volume_strong:
            score += 2
            notes.append("Volume is stronger than average")

    elif bias == "SHORT":
        if current_price < ema200:
            score += 8
            notes.append("Price is below EMA200")

        if ema50 < ema200:
            score += 7
            notes.append("EMA50 is below EMA200")

        if current_price < ema50:
            score += 6
            notes.append("Price is below EMA50")
        elif current_price < ema200:
            score += 3
            notes.append("Price is above EMA50 but still below EMA200")

        if 32 <= rsi14 <= 60:
            score += 8
            notes.append("RSI is in acceptable short range")
        elif 60 < rsi14 <= 66:
            score += 4
            notes.append("RSI is high but may be rolling over")

        if not extended:
            score += 4
            notes.append("Price is not overextended from EMA50")

        if volume_strong:
            score += 2
            notes.append("Volume is stronger than average")

    return {
        "technical_score": min(score, 35),
        "technical_notes": notes,
    }


==============================================================
3. REPLACE THE MAIN SCORING / DECISION BLOCK INSIDE analyze_market()
==============================================================

Inside analyze_market(), find this line:

    smc = calculate_smc_score(bias, liquidity, structure, fvg, ob, pd)

Then select everything from that line down to just BEFORE:

    return {
        "status": "ok",

Replace that whole block with this:


    # =====================================================
    # Directional scoring
    # =====================================================
    # Important:
    # We score LONG and SHORT separately.
    # This fixes the old problem where NEUTRAL / CHOP caused SMC = 0 and Tech = 0.

    smc_long = calculate_smc_score(
        "LONG", liquidity, structure, fvg, ob, pd,
        candles=candles, atr14=atr14, ema50=ema50, ema200=ema200
    )

    smc_short = calculate_smc_score(
        "SHORT", liquidity, structure, fvg, ob, pd,
        candles=candles, atr14=atr14, ema50=ema50, ema200=ema200
    )

    tech_long = calculate_technical_score(
        "LONG", current_price, ema50, ema200, rsi14, extended, volume_strong
    )

    tech_short = calculate_technical_score(
        "SHORT", current_price, ema50, ema200, rsi14, extended, volume_strong
    )

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

    long_total = smc_long["smc_score"] + tech_long["technical_score"] + risk_score
    short_total = smc_short["smc_score"] + tech_short["technical_score"] + risk_score

    # Choose best directional setup.
    # In clean EMA trend we respect trend direction more.
    # In chop/neutral we still allow early bounce/reversal setups if SMC is meaningful.

    selected_direction = "NONE"

    if bullish_trend:
        selected_direction = "LONG"
    elif bearish_trend:
        selected_direction = "SHORT"
    else:
        if long_total > short_total and smc_long["smc_score"] >= 15:
            selected_direction = "LONG"
        elif short_total > long_total and smc_short["smc_score"] >= 15:
            selected_direction = "SHORT"
        else:
            selected_direction = "NONE"

    if selected_direction == "LONG":
        bias = "LONG"
        smc = smc_long
        technical = tech_long
        total_score = long_total
    elif selected_direction == "SHORT":
        bias = "SHORT"
        smc = smc_short
        technical = tech_short
        total_score = short_total
    else:
        bias = "NONE"
        smc = {
            "smc_score": max(smc_long["smc_score"], smc_short["smc_score"]),
            "smc_notes": []
        }
        technical = {
            "technical_score": max(tech_long["technical_score"], tech_short["technical_score"]),
            "technical_notes": []
        }
        total_score = smc["smc_score"] + technical["technical_score"] + risk_score

    technical_score = technical["technical_score"]
    technical_notes = technical["technical_notes"]

    # =====================================================
    # Entry / SL / TP estimate
    # =====================================================

    if bias == "LONG":
        entry = current_price

        # Prefer structural stop below recent swing low if available.
        if liquidity["recent_low"]:
            structural_sl = liquidity["recent_low"] - atr14 * 0.25
            atr_sl = current_price - atr14 * 1.5
            stop_loss = min(atr_sl, structural_sl)
        else:
            stop_loss = current_price - atr14 * 1.5

        risk_per_unit = current_price - stop_loss

        tp1 = current_price + risk_per_unit * 1.0
        tp2 = current_price + risk_per_unit * 1.8
        tp3 = current_price + risk_per_unit * 2.8
        target = tp2
        potential_pct = (target - current_price) / current_price * 100

    elif bias == "SHORT":
        entry = current_price

        # Prefer structural stop above recent swing high if available.
        if liquidity["recent_high"]:
            structural_sl = liquidity["recent_high"] + atr14 * 0.25
            atr_sl = current_price + atr14 * 1.5
            stop_loss = max(atr_sl, structural_sl)
        else:
            stop_loss = current_price + atr14 * 1.5

        risk_per_unit = stop_loss - current_price

        tp1 = current_price - risk_per_unit * 1.0
        tp2 = current_price - risk_per_unit * 1.8
        tp3 = current_price - risk_per_unit * 2.8
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

    # =====================================================
    # Setup classification
    # =====================================================

    min_smc_for_confirmed_signal = 30
    min_total_for_confirmed_signal = 72

    min_smc_for_early_signal = 22
    min_total_for_early_signal = 55

    blocked_by = []

    long_early_trigger = (
        bias == "LONG"
        and (
            liquidity["low_sweep"]
            or fvg["inside_bullish_fvg"]
            or ob["inside_bullish_ob"]
            or pd["pd_zone"] == "DISCOUNT"
        )
        and smc["smc_score"] >= min_smc_for_early_signal
    )

    short_early_trigger = (
        bias == "SHORT"
        and (
            liquidity["high_sweep"]
            or fvg["inside_bearish_fvg"]
            or ob["inside_bearish_ob"]
            or pd["pd_zone"] == "PREMIUM"
        )
        and smc["smc_score"] >= min_smc_for_early_signal
    )

    long_confirmed_trigger = (
        bias == "LONG"
        and total_score >= min_total_for_confirmed_signal
        and smc["smc_score"] >= min_smc_for_confirmed_signal
        and (
            structure["bos_bullish"]
            or structure["structure"] == "BULLISH"
            or liquidity["low_sweep"]
        )
    )

    short_confirmed_trigger = (
        bias == "SHORT"
        and total_score >= min_total_for_confirmed_signal
        and smc["smc_score"] >= min_smc_for_confirmed_signal
        and (
            structure["bos_bearish"]
            or structure["structure"] == "BEARISH"
            or liquidity["high_sweep"]
        )
    )

    # Build blocked_by explanations.
    if bias == "LONG":
        if smc["smc_score"] < min_smc_for_early_signal:
            blocked_by.append("SMC score below early long threshold")
        if current_price < ema200:
            blocked_by.append("Price below EMA200")
        if rsi14 < 38:
            blocked_by.append("RSI still weak for long")
        if extended and not liquidity["low_sweep"]:
            blocked_by.append("Price extended without liquidity sweep")
        if not volume_strong:
            blocked_by.append("Volume is not strong")

    elif bias == "SHORT":
        if smc["smc_score"] < min_smc_for_early_signal:
            blocked_by.append("SMC score below early short threshold")
        if current_price > ema200:
            blocked_by.append("Price above EMA200")
        if rsi14 > 62:
            blocked_by.append("RSI still strong for short")
        if extended and not liquidity["high_sweep"]:
            blocked_by.append("Price extended without liquidity sweep")
        if not volume_strong:
            blocked_by.append("Volume is not strong")

    else:
        blocked_by.append("No directional setup selected")
        blocked_by.append("Long and short scores are both weak or unclear")

    # =====================================================
    # Decision logic
    # =====================================================

    if long_confirmed_trigger:
        decision = "CONFIRMED LONG"
        action = "PREPARE LONG"
        reason = "Confirmed long setup: SMC and technical conditions are aligned."
        if smc["smc_notes"]:
            reason += " " + "; ".join(smc["smc_notes"]) + "."
        position = "Small controlled position"
        leverage = "Low / conservative"

    elif short_confirmed_trigger:
        decision = "CONFIRMED SHORT"
        action = "PREPARE SHORT"
        reason = "Confirmed short setup: SMC and technical conditions are aligned."
        if smc["smc_notes"]:
            reason += " " + "; ".join(smc["smc_notes"]) + "."
        position = "Small controlled position"
        leverage = "Low / conservative"

    elif long_early_trigger and total_score >= min_total_for_early_signal:
        decision = "EARLY LONG"
        action = "WATCH / ENTER SMALL ONLY"
        reason = (
            "Early long bounce setup detected. This is not fully confirmed yet, "
            "but price is reacting from a potentially favorable SMC area."
        )
        if smc["smc_notes"]:
            reason += " " + "; ".join(smc["smc_notes"]) + "."
        if not structure["bos_bullish"]:
            blocked_by.append("No bullish BOS confirmation yet")
        position = "Tiny test position only"
        leverage = "Very low / no leverage preferred"

    elif short_early_trigger and total_score >= min_total_for_early_signal:
        decision = "EARLY SHORT"
        action = "WATCH / ENTER SMALL ONLY"
        reason = (
            "Early short rejection setup detected. This is not fully confirmed yet, "
            "but price is reacting from a potentially favorable SMC area."
        )
        if smc["smc_notes"]:
            reason += " " + "; ".join(smc["smc_notes"]) + "."
        if not structure["bos_bearish"]:
            blocked_by.append("No bearish BOS confirmation yet")
        position = "Tiny test position only"
        leverage = "Very low / no leverage preferred"

    elif bias == "LONG" and smc["smc_score"] >= 15:
        decision = "WATCH LONG"
        action = "WAIT FOR LONG TRIGGER"
        reason = (
            "Long conditions are developing, but confirmation is incomplete. "
            "Wait for reclaim, bullish BOS, stronger reaction candle, or volume confirmation."
        )
        position = "Not yet"
        leverage = "Not yet"

    elif bias == "SHORT" and smc["smc_score"] >= 15:
        decision = "WATCH SHORT"
        action = "WAIT FOR SHORT TRIGGER"
        reason = (
            "Short conditions are developing, but confirmation is incomplete. "
            "Wait for rejection, bearish BOS, stronger reaction candle, or volume confirmation."
        )
        position = "Not yet"
        leverage = "Not yet"

    elif trend == "NEUTRAL / CHOP":
        decision = "NO TRADE"
        action = "WAIT"
        reason = "Market is choppy and no useful early or confirmed setup is detected."
        position = "$0"
        leverage = "None"

    elif extended:
        decision = "DO NOT CHASE"
        action = "WAIT FOR PULLBACK"
        reason = "Trend exists, but price is extended from EMA50. Wait for a better location."
        position = "$0"
        leverage = "None"

    else:
        decision = "NO TRADE"
        action = "WAIT"
        reason = "No clean actionable setup."
        position = "$0"
        leverage = "None"


==============================================================
4. UPDATE THE analyze_market() RETURN FIELDS
==============================================================

In the big return block of analyze_market(), find this part:

        "smc_score": smc["smc_score"],
        "smc_notes": smc["smc_notes"],
        "technical_score": technical_score,
        "risk_score": risk_score,
        "total_score": total_score,

Replace it with this:

        "smc_score": smc["smc_score"],
        "smc_notes": smc["smc_notes"],
        "technical_score": technical_score,
        "technical_notes": technical_notes,
        "risk_score": risk_score,
        "total_score": total_score,
        "long_total": long_total,
        "short_total": short_total,
        "long_smc_score": smc_long["smc_score"],
        "short_smc_score": smc_short["smc_score"],
        "long_technical_score": tech_long["technical_score"],
        "short_technical_score": tech_short["technical_score"],
        "blocked_by": blocked_by,


==============================================================
5. ADD blocked_by DISPLAY TO dashboard()
==============================================================

Inside dashboard(), find:

    smc_notes_html = ""

Below the SMC notes block, add this:

    blocked_by_html = ""

    if data.get("blocked_by"):
        blocked_by_html = "<ul>"
        for item in data["blocked_by"]:
            blocked_by_html += f"<li>{html.escape(str(item))}</li>"
        blocked_by_html += "</ul>"
    else:
        blocked_by_html = "<p>No major blockers.</p>"


Then find this card:

            <div class="card">
                <div class="decision">{html.escape(data["decision"])}</div>
                <h2>Action: {html.escape(data["action"])}</h2>
                <p class="reason">{html.escape(data["reason"])}</p>
            </div>

Replace it with this:

            <div class="card">
                <div class="decision">{html.escape(data["decision"])}</div>
                <h2>Action: {html.escape(data["action"])}</h2>
                <p class="reason">{html.escape(data["reason"])}</p>
                <h3>Blocked By / Missing Confirmation</h3>
                {blocked_by_html}
            </div>


==============================================================
6. AFTER EDITING
==============================================================

1. Save / Commit changes in GitHub.
2. Wait for Render to redeploy automatically.
3. Open the dashboard again.
4. Test BTCUSDT on 1H and 5m.
5. Check whether the agent now shows WATCH LONG / EARLY LONG when TradingView sees a bounce.
6. Send a screenshot for review.

Repo:
https://github.com/marinerpmv-del/trading-agent-webhook

main.py:
https://github.com/marinerpmv-del/trading-agent-webhook/blob/main/main.py
