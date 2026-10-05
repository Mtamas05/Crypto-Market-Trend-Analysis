import time
import os
import requests
import subprocess
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

def tg_send(msg):
    try:
        requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            params={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10
        )
    except Exception:
        pass

def main():
    print("Bot Watchdog (Dead Man's Switch) indítva...")
    heartbeat_file = "bot_heartbeat.ts"
    
    while True:
        try:
            if os.path.exists(heartbeat_file):
                with open(heartbeat_file, "r") as f:
                    content = f.read().strip()
                    if content:
                        last_hb = float(content)
                        if time.time() - last_hb > 90:
                            print("🚨 Heartbeat hiba, a bot fagyott!")
                            tg_send("🚨 A bot nem válaszol (>90mp heartbeat nélkül). Watchdog újraindítja a folyamatot!")
                            
                            # Rendszer-specifikus kilövés (Linux)
                            subprocess.run("pkill -9 -f pro_bot_v5_pi.py", shell=True)
                            
                            # A systemd 'Restart=always' miatt a szolgáltatás magától újra fog indulni.
                            # Töröljük a régi heartbeat fájlt, hogy ne öljön feleslegesen amíg a bot indul
                            os.remove(heartbeat_file)
            
            time.sleep(120)  # 2 percenként ellenőriz
            
        except Exception as e:
            print(f"Watchdog hiba: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
