#!/usr/bin/env python3
import json, time, os

def show_status():
    while True:
        os.system('clear' if os.name == 'posix' else 'cls')
        try:
            with open("../data/bot_state_paper.json") as f:
                s = json.load(f)
            print("=" * 55)
            print(f"  PRO-BOT V5 MONITOR  |  Tőke: ${s.get('total_capital', 0):.2f}")
            print(f"  Csúcs: ${s.get('peak_capital', 0):.2f}  |  Rezsim: {s.get('current_regime', 'N/A')}")
            print("=" * 55)
            print(f"{'COIN':<10} {'POZÍCIÓ':<8} {'ENTRY':<10} {'STOP':<10} {'REZSIM':<12}")
            print("-" * 55)
            for c in s.get("active_symbols", []):
                pos = s.get("active_pos", {}).get(c) or "FLAT"
                entry = s.get("entry_prices", {}).get(c, 0.0)
                stop = s.get("stop_losses", {}).get(c, 0.0)
                reg = s.get("regimes", {}).get(c, "N/A")
                print(f"{c:<10} {pos:<8} {entry:<10.4f} {stop:<10.4f} {reg:<12}")
            print("=" * 55)
            print("Frissítés 2 mp-enként (Kilépés: Ctrl+C)")
        except Exception as e:
            print(f"Olvasási hiba: {e}")
        time.sleep(2)

if __name__ == "__main__":
    show_status()
