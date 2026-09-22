import json
import requests

import config
from auditor import load_rules_document
from trade_memory import LIVE_MEMORY


def load_learned_rules(symbol):
    doc = load_rules_document()
    target = str(symbol or "").upper()
    active = []
    for rule in doc.get("rules") or []:
        if str(rule.get("status", "active")).lower() not in ("active", ""):
            continue
        affected = str(rule.get("affected_symbol") or "ALL").upper()
        if affected in ("ALL", "*", target) or target in affected:
            active.append(rule)
    return active


def _apply_rule_penalty(decision, rules, market_data):
    score = int(decision.get("confidence_score") or 0)
    logic = str(decision.get("logic") or "")
    deductions = []
    h1 = market_data.get("h1_data") or {}
    d1 = market_data.get("daily_data") or {}
    rel = float(h1.get("rel_volume") or d1.get("rel_volume") or 1)
    for rule in rules:
        setup = str(rule.get("setup") or "").lower()
        points = int(rule.get("confidence_reduction_points") or 0)
        hit = False
        if "low-volume" in setup or "low volume" in setup:
            hit = rel < 0.9
        elif "atr" in setup or "volatil" in setup:
            hit = True
        elif "correlation" in setup:
            hit = True
        else:
            hit = True
        if hit and points:
            score -= points
            deductions.append(
                f"-{points} {rule.get('affected_symbol')}:{rule.get('setup')}"
            )
    score = max(0, min(100, score))
    if deductions:
        logic = (logic + " Learned-rule deductions: " + "; ".join(deductions)).strip()
    decision["confidence_score"] = score
    decision["logic"] = logic
    return decision


def get_ai_decision(market_data, symbol, extra_context="", book="NSE_EQ", memory=None):
    rules = load_learned_rules(symbol)
    rule_lines = []
    for rule in rules:
        rule_lines.append(
            f"- {rule.get('affected_symbol')}: {rule.get('setup')} "
            f"(penalty {rule.get('confidence_reduction_points')}, "
            f"n={rule.get('sample_size')}). {rule.get('evidence')}"
        )
    rules_block = "\n".join(rule_lines) if rule_lines else "None."

    buy_only = bool(getattr(config, "BUY_ONLY", True))
    signals = '"BUY" | "HOLD"' if buy_only else '"BUY" | "SELL" | "HOLD"'
    system_prompt = f"""
You are an elite quantitative hedge-fund trader analyzing {symbol} on Indian
NSE cash and MCX via Dhan. Treat the symbol as the broker instrument.

ACTIVE RISK AUDIT RULES (mandatory confidence penalties when conditions match):
{rules_block}

Analyze Daily for structure and H1 for momentum.
Return raw JSON only:
{{
    "signal": {signals},
    "confidence_score": 0,
    "stop_loss": 0.0,
    "take_profit": 0.0,
    "logic": "concise explanation including indicators and learned-rule deductions"
}}
{"Prefer BUY only when daily trend and opening momentum agree. Do not recommend SELL." if buy_only else ""}
Cash and MCX are separate books. Do not HOLD MCX because cash lost, or vice versa.
"""

    memory = memory if memory is not None else LIVE_MEMORY
    memory_block = memory.prompt_block(book)
    h1 = market_data.get("h1_data") or {}
    d1 = market_data.get("daily_data") or {}

    user_prompt = f"""
Symbol: {symbol}
{extra_context}
{memory_block}

EQUITY: {market_data.get("equity")}
BID: {market_data.get("bid")} ASK: {market_data.get("ask") or market_data.get("ask_price")}
SPREAD: {market_data.get("spread")}

DAILY EMA200={d1.get("ema200")} RSI={d1.get("rsi14")} ATR={d1.get("atr14")} REL_VOL={d1.get("rel_volume")} TREND={d1.get("trend_10")}
H1 EMA200={h1.get("ema200")} RSI={h1.get("rsi14")} ATR={h1.get("atr14")} REL_VOL={h1.get("rel_volume")} TREND={h1.get("trend_10")}

DAILY CSV:
{market_data.get("daily_csv")}

H1 CSV:
{market_data.get("hourly_csv")}
"""

    url = str(config.DEEPSEEK_API_BASE).rstrip("/") + "/chat/completions"
    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
    }
    print(f"[AI] Requesting decision for {symbol}.")
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        decision = json.loads(response.json()["choices"][0]["message"]["content"])
    except Exception as error:
        print(f"[AI] DeepSeek request failed for {symbol}: {error}")
        return {
            "signal": "HOLD",
            "confidence_score": 0,
            "stop_loss": 0.0,
            "take_profit": 0.0,
            "logic": f"HOLD: model request failed ({error}).",
        }
    if not isinstance(decision, dict):
        return {"signal": "HOLD", "confidence_score": 0, "logic": "Invalid model payload."}

    decision = _apply_rule_penalty(decision, rules, market_data)
    try:
        confidence = int(float(decision.get("confidence_score", 0)))
    except (TypeError, ValueError):
        confidence = 0
    confidence = max(0, min(100, confidence))
    signal = str(decision.get("signal", "HOLD")).strip().upper()
    if confidence < config.CONFIDENCE_THRESHOLD:
        signal = "HOLD"
        decision["logic"] = (
            f"HOLD: confidence {confidence} below {config.CONFIDENCE_THRESHOLD}. "
            + str(decision.get("logic") or "")
        )
    if buy_only and signal == "SELL":
        signal = "HOLD"
        decision["logic"] = "HOLD: buy-only desk blocked SELL. " + str(decision.get("logic") or "")
    decision["signal"] = signal
    decision["confidence_score"] = confidence

    price = float(market_data.get("ask") or market_data.get("ask_price") or 0)
    atr = float((h1.get("atr14") or d1.get("atr14") or price * 0.01) or 0)
    try:
        sl = float(decision.get("stop_loss") or 0)
        tp = float(decision.get("take_profit") or 0)
    except (TypeError, ValueError):
        sl, tp = 0.0, 0.0
    if signal == "BUY" and not (sl < price < tp):
        sl = price - 1.5 * atr
        tp = price + float(config.REWARD_RATIO) * 1.5 * atr
    if signal == "SELL" and not (tp < price < sl):
        sl = price + 1.5 * atr
        tp = price - float(config.REWARD_RATIO) * 1.5 * atr
    decision["stop_loss"] = sl
    decision["take_profit"] = tp
    return decision
