"""Paper vs live desk mode for the current IST day."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config

IST = ZoneInfo("Asia/Kolkata")
PATH = Path(__file__).resolve().parent / "data" / "desk_mode.json"


def today_ist():
    return str(datetime.now(IST).date())


def _default_mode():
    return "paper" if getattr(config, "PAPER_TRADE", False) else "live"


def load_desk_mode():
    if not PATH.exists():
        return _default_mode()
    try:
        doc = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return _default_mode()
    mode = str(doc.get("mode") or "").lower()
    return "paper" if mode == "paper" else "live"


def apply_to_config(mode):
    paper = str(mode).lower() == "paper"
    config.PAPER_TRADE = paper
    if not paper:
        config.TRADING_ENABLED = True
    return "paper" if paper else "live"


def apply_saved_desk_mode(bot_state):
    mode = apply_to_config(load_desk_mode())
    bot_state["desk_mode"] = mode
    bot_state["paper_trade"] = mode == "paper"
    bot_state["desk_mode_date"] = today_ist()
    return mode


def set_desk_mode(mode, bot_state):
    chosen = apply_to_config(mode)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(
        json.dumps(
            {"mode": chosen, "date": today_ist()},
            indent=2,
        ),
        encoding="utf-8",
    )
    bot_state["desk_mode"] = chosen
    bot_state["paper_trade"] = chosen == "paper"
    bot_state["desk_mode_date"] = today_ist()
    return chosen
