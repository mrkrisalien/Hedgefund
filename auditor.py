"""Self-learning auditor: loss-pattern rules persisted in new_rules.json."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import requests

import config
from memory_store import load_trade_memory, reconcile_closed_trades


def load_rules_document():
    path = config.RULES_FILE
    if not path.exists():
        return {"rules": [], "last_audit_at": None, "trades_analyzed": 0}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("invalid rules document")
        payload.setdefault("rules", [])
        payload.setdefault("last_audit_at", None)
        payload.setdefault("trades_analyzed", 0)
        return payload
    except (json.JSONDecodeError, OSError, ValueError):
        return {"rules": [], "last_audit_at": None, "trades_analyzed": 0}


def save_rules_document(doc):
    config.RULES_FILE.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")


def _infer_realized_pnl(row):
    try:
        stored = float(row.get("realized_pnl"))
    except (TypeError, ValueError):
        stored = None
    if stored not in (None, 0, 0.0):
        return stored
    try:
        entry = float(row.get("entry_price") or row.get("entry") or 0)
        exit_px = float(row.get("exit_price") or row.get("exit") or 0)
        qty = float(row.get("volume") or row.get("qty") or 0)
    except (TypeError, ValueError):
        return stored
    if entry <= 0 or exit_px <= 0 or qty <= 0:
        return stored
    side = str(row.get("side") or row.get("signal") or "BUY").upper()
    if side == "SELL":
        return (entry - exit_px) * qty
    return (exit_px - entry) * qty


def _closed_with_pnl(rows):
    closed = []
    for row in rows:
        if str(row.get("status", "")).upper() != "CLOSED":
            continue
        pnl = _infer_realized_pnl(row)
        if pnl is None:
            continue
        closed.append({**row, "realized_pnl": float(pnl)})
    return closed[-50:]


def _backtest_closed_trades():
    """Use the last year replay when live memory has too few closed fills."""
    names = (
        "backtest_results_green_cut.json",
        "backtest_results_calendar.json",
        "backtest_results_year.json",
        "backtest_results.json",
    )
    for name in names:
        path = config.BASE_DIR / name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        rows = payload.get("trade_log") or []
        out = []
        for row in rows[-80:]:
            try:
                pnl = float(row.get("pnl"))
            except (TypeError, ValueError):
                continue
            out.append(
                {
                    "status": "CLOSED",
                    "symbol": row.get("symbol"),
                    "side": row.get("signal") or "BUY",
                    "entry_price": row.get("entry"),
                    "exit_price": row.get("exit"),
                    "realized_pnl": pnl,
                    "market_context": {
                        "h1_atr": row.get("atr"),
                        "d1_atr": row.get("atr"),
                    },
                    "source": name,
                }
            )
        if out:
            return out
    return []


def run_audit(force=False):
    print("[AUDITOR] Starting audit.")
    reconcile_closed_trades()
    doc = load_rules_document()
    trades = _closed_with_pnl(load_trade_memory())
    source = "memory"
    if force and len(trades) < 3:
        extra = _backtest_closed_trades()
        if extra:
            trades = (trades + extra)[-80:]
            source = "memory+backtest"
    if len(trades) < 2:
        return {
            "ok": False,
            "reason": "insufficient_trades",
            "message": (
                f"Need at least 2 closed trades with P&L. "
                f"Memory has {len(trades)}. Paper fills are not closed Dhan trades."
            ),
            "trades_analyzed": len(trades),
            "document": doc,
        }

    last = doc.get("last_audit_at")
    if last and not force:
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            wait = timedelta(hours=config.AUDIT_COOLDOWN_HOURS)
            if datetime.now(timezone.utc) - last_dt < wait:
                return {
                    "ok": False,
                    "reason": "cooldown",
                    "message": f"Cooldown until {config.AUDIT_COOLDOWN_HOURS}h after last audit ({last}). Use force from the dashboard.",
                    "last_audit_at": last,
                    "document": doc,
                }
        except ValueError:
            pass

    losses = [row for row in trades if row["realized_pnl"] < -0.5]
    if not losses:
        return {
            "ok": True,
            "reason": "no_losses",
            "message": f"Analyzed {len(trades)} closed trades ({source}); none lost more than Rs 0.50, so no new rules.",
            "trades_analyzed": len(trades),
            "document": doc,
        }

    summaries = []
    for row in losses[-20:]:
        ctx = row.get("market_context") or {}
        summaries.append(
            {
                "symbol": row.get("symbol"),
                "side": row.get("side") or row.get("signal"),
                "entry": row.get("entry_price"),
                "exit": row.get("exit_price"),
                "realized_pnl": row.get("realized_pnl"),
                "h1_rel_volume": ctx.get("h1_rel_volume"),
                "d1_rel_volume": ctx.get("d1_rel_volume"),
                "h1_atr": ctx.get("h1_atr"),
                "d1_atr": ctx.get("d1_atr"),
                "correlated": ctx.get("correlated"),
            }
        )

    system = (
        "You are Chief Risk Officer and Quantitative Auditor for an Indian "
        "cash/MCX desk (Dhan). Identify recurring LOSS patterns: low-volume "
        "breakdowns/breakouts, overextended ATR or volatility traps, and "
        "adverse cross-asset correlations. Return raw JSON only: "
        '{"rules":[{"affected_symbol":"RELIANCE or ALL","setup":"short name",'
        '"confidence_reduction_points":20,"sample_size":3,"evidence":"one sentence"}]} '
        "confidence_reduction_points must be 15-30."
    )
    user = json.dumps({"loss_trades": summaries}, default=str)
    url = str(config.DEEPSEEK_API_BASE).rstrip("/") + "/chat/completions"
    try:
        response = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
            },
            json={
                "model": config.DEEPSEEK_MODEL,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=90,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        new_rules = parsed.get("rules") if isinstance(parsed, dict) else parsed
        if not isinstance(new_rules, list):
            new_rules = []
    except Exception as error:
        print(f"[AUDITOR] DeepSeek audit failed: {error}")
        return {
            "ok": False,
            "reason": "deepseek_error",
            "message": f"DeepSeek audit failed: {error}",
            "document": doc,
        }

    merged = list(doc.get("rules") or [])
    for rule in new_rules:
        if not isinstance(rule, dict):
            continue
        points = int(rule.get("confidence_reduction_points") or 20)
        points = max(15, min(30, points))
        merged.append(
            {
                "affected_symbol": str(rule.get("affected_symbol") or "ALL"),
                "setup": str(rule.get("setup") or "unspecified"),
                "confidence_reduction_points": points,
                "sample_size": int(rule.get("sample_size") or len(losses)),
                "evidence": str(rule.get("evidence") or ""),
                "status": "active",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    doc = {
        "rules": merged[-80:],
        "last_audit_at": datetime.now(timezone.utc).isoformat(),
        "trades_analyzed": len(trades),
    }
    save_rules_document(doc)
    print(f"[AUDITOR] Stored {len(new_rules)} new rule(s).")
    return {
        "ok": True,
        "reason": "updated",
        "message": f"Stored {len(new_rules)} rule(s) from {len(losses)} losing trades ({source}).",
        "new_rules": len(new_rules),
        "document": doc,
    }
