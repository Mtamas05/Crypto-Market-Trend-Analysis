import sqlite3
import pandas as pd
import matplotlib.pyplot as plt
import requests
import os
import io

DB_FILE = "../data/bot_v5.db"
from config import TELEGRAM_TOKEN as TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

def generate_and_send_weekly_report(db_path=DB_FILE):
    if not os.path.exists(db_path):
        return
        
    try:
        conn = sqlite3.connect(db_path)
        df = pd.read_sql("SELECT ts, capital, peak FROM equity ORDER BY ts ASC", conn)
        conn.close()
        
        if df.empty:
            return
            
        df["ts"] = pd.to_datetime(df["ts"], unit="s")
        df.set_index("ts", inplace=True)
        
        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(10, 6))
        
        ax.plot(df.index, df["capital"], color='#03dac6', label='Equity ($)', linewidth=2)
        ax.plot(df.index, df["peak"], color='#bb86fc', linestyle='--', label='Peak ($)', linewidth=1.5)
        
        ax.fill_between(df.index, df["capital"], df["peak"], where=(df["capital"] < df["peak"]), color='red', alpha=0.3, label='Drawdown')
        
        ax.set_title("📈 Heti PRO-BOT V5 Telemetria", fontsize=16, color='white')
        ax.set_xlabel("Dátum", fontsize=12)
        ax.set_ylabel("Egyenleg (USD)", fontsize=12)
        ax.grid(color='#333333', linestyle='--', linewidth=0.5)
        ax.legend(loc='upper left')
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        buf.seek(0)
        
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            files = {"photo": ("report.png", buf, "image/png")}
            data = {"chat_id": TELEGRAM_CHAT_ID, "caption": "📊 Heti PRO-BOT V5 Equity Riport"}
            requests.post(url, data=data, files=files, timeout=10)
            
        plt.close(fig)
    except Exception as e:
        print(f"Hiba a jelentés generálásakor: {e}")

if __name__ == "__main__":
    generate_and_send_weekly_report()
