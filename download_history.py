#!/usr/bin/env python3
"""
download_history.py — 3 éves historikus adat letöltő
=====================================================
Letölti a Binance Futures API-ról az összes coin
napi / 4h / 1h gyertyaadatait 3 évre visszamenőleg,
és egy SQLite adatbázisba (history.db) menti.

Telepítés:
    pip install requests

Futtatás (egyszeri, indítás előtt):
    python3 download_history.py

Várható futási idő Pi Zero W-n: ~5-15 perc
Várható adatbázis méret:        ~50-80 MB
"""

import time
import sqlite3
import requests
from datetime import datetime, timedelta
from config import BINANCE_API_KEY  # Importáljuk a Binance API kulcsot

# ── Konfiguráció ──────────────────────────────────────────────────
SYMBOLS      = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "FETUSDT"]
TIMEFRAMES   = ["1d", "4h", "1h"]
YEARS_BACK   = 3
DB_PATH      = "history.db"
BINANCE_BASE = "https://fapi.binance.com"

# Binance max 1500 gyertyát ad vissza egyszerre
LIMIT        = 1500
MAX_RETRIES  = 5  # Maximális újrapróbálkozások száma hibák esetén

# ── Adatbázis ─────────────────────────────────────────────────────
def db_init(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol    TEXT    NOT NULL,
            timeframe TEXT    NOT NULL,
            ts        INTEGER NOT NULL,
            open      REAL    NOT NULL,
            high      REAL    NOT NULL,
            low       REAL    NOT NULL,
            close     REAL    NOT NULL,
            volume    REAL    NOT NULL,
            UNIQUE(symbol, timeframe, ts)
        )
    """)
    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_candles_sym_tf_ts
        ON candles (symbol, timeframe, ts)
    """)
    con.commit()
    print("✅ Adatbázis inicializálva:", DB_PATH)

# ── Egy batch lekérése ────────────────────────────────────────────
def fetch_batch(symbol: str, interval: str, start_ms: int) -> list:
    """Legfeljebb LIMIT gyertyát kér le start_ms-től, exponenciális backoff-os újrapróbálkozással."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(
                f"{BINANCE_BASE}/fapi/v1/klines",
                params={
                    "symbol":    symbol,
                    "interval":  interval,
                    "startTime": start_ms,
                    "limit":     LIMIT,
                },
                headers={"X-MBX-APIKEY": BINANCE_API_KEY},
                timeout=30,
            )
            # Rate limit vagy átmeneti Binance hiba → exponenciális backoff
            if r.status_code in (429, 418, 503):
                wait = min(2 ** attempt, 60)  # max 60 mp várakozás
                print(f"  ⚠️  {r.status_code} hiba ({symbol} {interval}), várakozás {wait}s... próbálkozás {attempt}/{MAX_RETRIES}")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            wait = min(2 ** attempt, 60)
            print(f"  ⚠️  Timeout ({symbol} {interval}), várakozás {wait}s... próbálkozás {attempt}/{MAX_RETRIES}")
            time.sleep(wait)
        except Exception as e:
            wait = min(2 ** attempt, 60)
            print(f"  ⚠️  Fetch hiba ({symbol} {interval}): {e}, próbálkozás {attempt}/{MAX_RETRIES}")
            time.sleep(wait)
    
    print(f"  ❌ Sikertelen letöltés {symbol} {interval} {MAX_RETRIES} próbálkozás után")
    return []

# ── Egy szimbólum + timeframe teljes letöltése ────────────────────
def download_symbol_tf(con, symbol: str, tf: str, start_dt: datetime):
    """Letölti az összes gyertyát start_dt-től napjainkig, lapozással."""

    # Megnézzük, mi van már az adatbázisban (folytatható letöltés)
    row = con.execute(
        "SELECT MAX(ts) FROM candles WHERE symbol=? AND timeframe=?",
        (symbol, tf)
    ).fetchone()
    last_ts = row[0] if row and row[0] else None

    if last_ts:
        # Folytatás az utolsó mentett gyertyától
        start_ms = last_ts + 1
        print(f"  🔄 Folytatás {symbol} {tf}: {datetime.fromtimestamp(last_ts/1000)}")
    else:
        start_ms = int(start_dt.timestamp() * 1000)
        print(f"  ⬇️  Letöltés {symbol} {tf}: {start_dt.date()} -tól")

    total_inserted = 0
    now_ms = int(time.time() * 1000)

    while start_ms < now_ms:
        batch = fetch_batch(symbol, tf, start_ms)
        if not batch:
            time.sleep(2)
            break

        rows = []
        for c in batch:
            rows.append((
                symbol, tf,
                int(c[0]),        # nyitó timestamp (ms)
                float(c[1]),      # open
                float(c[2]),      # high
                float(c[3]),      # low
                float(c[4]),      # close
                float(c[5]),      # volume
            ))

        if rows:
            con.executemany(
                """INSERT OR IGNORE INTO candles
                   (symbol, timeframe, ts, open, high, low, close, volume)
                   VALUES (?,?,?,?,?,?,?,?)""",
                rows
            )
            con.commit()
            total_inserted += len(rows)

        # Következő batch kezdete: az utolsó gyertya ts + 1ms
        last_candle_ts = int(batch[-1][0])
        if last_candle_ts <= start_ms:
            break  # nem haladunk előre, kilépünk
        start_ms = last_candle_ts + 1

        # Binance rate limit: max 1200 kérés/perc, óvatosan
        time.sleep(0.25)

    print(f"     ✅ {total_inserted} új gyertya mentve ({symbol} {tf})")
    return total_inserted

# ── Letöltés utáni statisztika ────────────────────────────────────
def print_stats(con):
    print("\n📊 ADATBÁZIS STATISZTIKA:")
    print("-" * 50)
    rows = con.execute("""
        SELECT symbol, timeframe,
               COUNT(*) as db_count,
               datetime(MIN(ts)/1000, 'unixepoch') as from_dt,
               datetime(MAX(ts)/1000, 'unixepoch') as to_dt
        FROM candles
        GROUP BY symbol, timeframe
        ORDER BY symbol, timeframe
    """).fetchall()

    for r in rows:
        print(f"  {r[0]:10} {r[1]:4}  |  {r[2]:5} gyertya  |  {r[3]} → {r[4]}")
    print("-" * 50)
    size_mb = __import__("os").path.getsize(DB_PATH) / 1024 / 1024
    print(f"  💾 Adatbázis méret: {size_mb:.1f} MB")

# ── Gyors ellenőrzés: van-e lyuk az adatokban ─────────────────────
def check_gaps(con):
    print("\n🔍 ADATMINŐSÉG ELLENŐRZÉS:")
    tf_seconds = {"1h": 3600, "4h": 14400, "1d": 86400}
    issues = 0

    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            expected_gap = tf_seconds.get(tf, 3600) * 1000  # ms-ben
            rows = con.execute(
                """SELECT ts FROM candles
                   WHERE symbol=? AND timeframe=?
                   ORDER BY ts""",
                (symbol, tf)
            ).fetchall()

            if not rows:
                continue

            timestamps = [r[0] for r in rows]
            gaps = []
            for i in range(1, len(timestamps)):
                diff = timestamps[i] - timestamps[i-1]
                # Ha a különbség 2x akkora mint a várható, az hiányzó gyertya
                if diff > expected_gap * 2:
                    gap_start = datetime.fromtimestamp(timestamps[i-1] / 1000)
                    gap_end   = datetime.fromtimestamp(timestamps[i] / 1000)
                    gaps.append(f"{gap_start} → {gap_end}")

            if gaps:
                print(f"  ⚠️  {symbol} {tf}: {len(gaps)} hiány")
                for g in gaps[:3]:  # csak az első 3
                    print(f"       {g}")
                issues += len(gaps)
            else:
                print(f"  ✅ {symbol} {tf}: nincs hiány")

    if issues == 0:
        print("\n  🎉 Az összes adat folyamatos, nincs hiány!")
    else:
        print(f"\n  ⚠️  Összesen {issues} hiány — futtasd újra a letöltőt a pótláshoz.")

# ── Fő program ────────────────────────────────────────────────────
def main():
    print("═" * 60)
    print("  PRO-BOT V5 — Historikus adat letöltő")
    print(f"  Coinok: {', '.join(SYMBOLS)}")
    print(f"  Időkeretek: {', '.join(TIMEFRAMES)}")
    print(f"  Visszamenőleg: {YEARS_BACK} év")
    print("═" * 60)

    start_dt = datetime.now() - timedelta(days=365 * YEARS_BACK)
    print(f"\n📅 Letöltés kezdete: {start_dt.date()}\n")

    con = sqlite3.connect(DB_PATH)
    db_init(con)

    total = 0
    combos = [(s, tf) for s in SYMBOLS for tf in TIMEFRAMES]

    for i, (symbol, tf) in enumerate(combos, 1):
        print(f"\n[{i}/{len(combos)}] {symbol} {tf}")
        n = download_symbol_tf(con, symbol, tf, start_dt)
        total += n

    print(f"\n{'═'*60}")
    print(f"  ✅ Letöltés kész! Összesen {total} új gyertya mentve.")

    print_stats(con)
    check_gaps(con)

    con.close()
    print("\n🚀 Az adatbázis kész. Most már futtathatod a backtestet:")
    print("   python3 backtest.py")

if __name__ == "__main__":
    main()
