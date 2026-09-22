import json
from pathlib import Path

import config


JOURNAL_PATH = Path(__file__).resolve().parent / "trade_journal.json"


class TradeMemory:
    """Closed-trade log for in-context hints. This is not model fine-tuning."""

    def __init__(self, path=None, persist=True):
        self.path = Path(path) if path else JOURNAL_PATH
        self.persist = persist
        self.trades = []
        if persist and self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(payload, list):
                    self.trades = payload
            except (json.JSONDecodeError, OSError):
                self.trades = []

    def record(self, trade):
        book = str(trade.get("kind") or trade.get("book") or "NSE_EQ")
        row = {
            "date": trade.get("entry_date") or trade.get("date") or "",
            "symbol": trade.get("symbol") or trade.get("display") or "",
            "kind": "MCX" if book.upper() == "MCX" else "NSE_EQ",
            "signal": trade.get("signal", "BUY"),
            "confidence": trade.get("confidence"),
            "pnl": trade.get("pnl"),
            "reason": trade.get("reason") or trade.get("result") or "",
        }
        self.trades.append(row)
        if self.persist:
            self.path.write_text(
                json.dumps(self.trades[-500:], indent=2),
                encoding="utf-8",
            )

    def prompt_block(self, book):
        book = "MCX" if str(book).upper() == "MCX" else "NSE_EQ"
        limit = int(getattr(config, "TRADE_MEMORY_TRADES", 12))
        same = [row for row in self.trades if row.get("kind") == book][-limit:]
        if not same:
            return (
                f"BOOK: {book} is independent of the other book. "
                "No closed trades in this book yet."
            )

        lines = [
            f"BOOK: {book}. Cash and MCX do not block each other.",
            "Recent closed trades in THIS book only (outcomes, not orders):",
        ]
        for row in same:
            pnl = row.get("pnl")
            try:
                pnl_text = f"{float(pnl):+.0f}"
            except (TypeError, ValueError):
                pnl_text = str(pnl)
            lines.append(
                f"- {row.get('date')} {row.get('symbol')} "
                f"{row.get('signal')} conf={row.get('confidence')} "
                f"pnl={pnl_text} exit={row.get('reason')}"
            )
        lines.append(
            "Use these outcomes as hints for THIS book only. "
            "Do not veto an MCX setup because cash lost, or vice versa."
        )
        return "\n".join(lines)


LIVE_MEMORY = TradeMemory(persist=True)
