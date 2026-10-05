import re
import os

with open("pro_bot_v5_pi.py", "r", encoding="utf-8") as f:
    c = f.read()

# 1. Update telegram command
c = c.replace(
"""            elif txt == "/closeall":
                for c in state["active_symbols"]:
                    if state["active_pos"].get(c) == "LONG":   state["stop_losses"][c] = 1e9
                    elif state["active_pos"].get(c) == "SHORT": state["stop_losses"][c] = -1.0
                tg_send("🚨 VÉSZKAPCSOLÓ aktiválva!"); save_state()""",
"""            elif txt == "/panic" or txt == "/closeall":
                tg_send("🚨 VÉSZLEÁLLÍTÁS: Minden pozíció azonnali piaci zárása a tőzsdén!")
                for c in list(state["active_symbols"]):
                    pos = state["active_pos"].get(c)
                    if pos:
                        side = "SELL" if pos == "LONG" else "BUY"
                        p = LIVE_PRICES.get(c, 0)
                        qty = get_qty(c, state["stakes"].get(c, 0), p) if p > 0 else 0
                        if qty > 0:
                            execute_order(c, side, qty, order_type="MARKET", purpose="EXIT")
                        state["active_pos"][c] = None
                save_state()
                tg_send("✅ Minden pozíció likvidálva.")""")

c = c.replace("/status /coins /closeall /confirm", "/status /coins /panic /confirm")

# 2. Add reconcile_positions_with_exchange
reconcile_func = """
def reconcile_positions_with_exchange():
    \"\"\"Összehangolja a belső állapotot a Binance valós nyitott pozícióival.\"\"\"
    if not LIVE_TRADING:
        return
    try:
        positions = _bget("/fapi/v2/positionRisk", signed=True, fb=False)
        if not positions:
            return
        
        real_positions = {}
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            sym = p.get("symbol")
            if abs(amt) > 0 and sym in state["active_symbols"]:
                real_positions[sym] = "LONG" if amt > 0 else "SHORT"

        with STATE_LOCK:
            for sym in state["active_symbols"]:
                internal_side = state["active_pos"].get(sym)
                real_side = real_positions.get(sym)

                if internal_side and not real_side:
                    log.warning("⚠️ Ghost position detektálva: %s belsőleg %s, de tőzsdén zárt!", sym, internal_side)
                    state["active_pos"][sym] = None
                    tg_send(f"⚠️ EGYEZTETÉS: {sym} pozíció a tőzsdén lezárult (külső zárás/stop), belső állapot frissítve.")
                elif not internal_side and real_side:
                    log.error("🚨 Árva pozíció a tőzsdén: %s %s létezik, de a bot nem tartja nyilván!", sym, real_side)
                    tg_send(f"🚨 KRITIKUS: {sym} {real_side} létezik a Binance-on, de nincs belső nyilvántartásban!")
            save_state()
    except Exception as e:
        log.error("Reconciliation hiba: %s", e)

"""
# insert before def main()
c = c.replace("def main():", reconcile_func + "def main():")

# 3. Add to schedule
c = c.replace(
"""    schedule.every(7).days.do(
        lambda: threading.Thread(target=update_active_symbols, daemon=True).start())""",
"""    schedule.every(7).days.do(
        lambda: threading.Thread(target=update_active_symbols, daemon=True).start())
    schedule.every(3).minutes.do(
        lambda: threading.Thread(target=reconcile_positions_with_exchange, daemon=True).start())"""
)

# 4. Add heartbeat
c = c.replace(
"""        try:
            schedule.run_pending()
            tg_check()""",
"""        try:
            with open("bot_heartbeat.ts", "w") as f:
                f.write(str(time.time()))
            
            schedule.run_pending()
            tg_check()"""
)

with open("pro_bot_v5_pi.py", "w", encoding="utf-8") as f:
    f.write(c)

print("Patch applied")
