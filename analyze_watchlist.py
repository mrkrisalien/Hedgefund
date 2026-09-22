"""One-shot: print today's catalyst structure plans."""
from catalyst_setup import analyze_watchlist

if __name__ == "__main__":
    rows = analyze_watchlist()
    for row in rows:
        plan = row.get("plan") or {}
        print(
            f"#{row.get('rank')} {row.get('name')} ({row.get('symbol')}) "
            f"{'OK' if row.get('ok') else 'WAIT'} | {row.get('reason')}"
        )
        if plan:
            print(
                f"    {plan.get('setup_kind')} entry {plan.get('entry')} "
                f"SL {plan.get('sl')} TP {plan.get('tp')} qty {row.get('qty')} "
                f"risk Rs {row.get('risk_rs')}"
            )
