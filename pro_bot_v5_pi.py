#!/usr/bin/env python3
"""
pro_bot_v5_pi.py — Pro Bot V5 Raspberry Pi (Végleges)
=======================================================
Új fejlesztések ebben a verzióban:
  - Konfig szinkronizálva a backtest optimalizált értékeivel
  - Coinok: BTCUSDT, SOLUSDT, FETUSDT, AVAXUSDT (ETH+BNB kicserélve)
  - ATR_MULT 1.8 (tágabb stop, kevesebb korai kirázás)
  - ADX küszöb 20.0 (több valós jelzés)
  - Portfolió-szintű drawdown monitor (10-20% → fokozatosan csökkentett méret,
    20% → nincs új nyitás, 25% → hard stop; lásd MAX_DRAWDOWN)
  - RSI divergencia szűrő (bearish/bullish divergencia detektálás)
  - MAX_WEIGHT 40%-ra emelve
  - Profit target csak MEAN_REV stratégiánál
  - Vol szűrő lazítva 10%-ra

Javítások ebben a review-körben (2026-09):
  - AVAXUSDT pótolva a PRECISIONS/PRICE_PRECISIONS/MIN_QTY táblákban
    (korábban hiányzott, LIVE módban rossz order-mennyiséghez vezethetett)
  - update_active_symbols(): a BASE_SYMBOLS (backtest-validált 4 coin) mostantól
    mindig bent marad, a heti rotáció csak az extra helyekre válogat momentum
    alapján — korábban csendben kicserélhette SOL/FET/AVAX-ot ETH-ra
  - Drawdown-fékek összehangolva: MAX_DRAWDOWN 20%→25% (hard stop), a
    portfolio_drawdown_mult() "nincs új nyitás" szintje 25%→20% (soft stop) —
    így a két szint nem esik egybe, mindkettő ténylegesen elérhető/aktív
  - Új réteg: napi loss-limit (DAILY_LOSS_LIMIT_PCT, -4%) — gyorsabban
    reagál egy rossz napra, mint a csúcstól számított portfolió-DD
  - walkforward_optimize() mostantól ténylegesen visszaír a stratégiába:
    az ADX küszöböt és az ATR szorzót a mért win rate / stop-kizárási arány
    alapján finomhangolja (korlátok közé szorítva), és szép Telegram-
    összefoglalót küld a változásról

Telepítés:
    pip install requests pandas numpy scikit-learn schedule

Indítás:
    python pro_bot_v5_pi.py
"""

import os, sys, time, json, hmac, hashlib, logging
import sqlite3, threading, schedule, requests
import numpy as np, pandas as pd
from datetime import datetime, timedelta
from sklearn.cluster import KMeans
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
import asyncio
import websockets

from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, BINANCE_API_KEY, BINANCE_SECRET
try:
    from config import BYBIT_API_KEY, BYBIT_SECRET
    HAS_BYBIT = True
except ImportError:
    BYBIT_API_KEY = BYBIT_SECRET = ""
    HAS_BYBIT = False

# ══════════════════════════════════════════════════════════════════
#  ⚙️  KONFIGURÁCIÓ — backtest által bizonyított értékek
# ══════════════════════════════════════════════════════════════════
BASE_SYMBOLS    = ["BTCUSDT", "SOLUSDT", "FETUSDT", "AVAXUSDT"]
INITIAL_CAPITAL = 100.0
LEVERAGE        = 5
BASE_RISK_PCT   = 0.14
MAX_WEIGHT      = 0.40   # backtest optimalizált
MIN_WEIGHT      = 0.05
REBALANCE_HOURS = 600
TAKER_FEE_PCT   = 0.0004

# ── Drawdown-fékek, összehangolva ───────────────────────────────
# Szintek (profi ajánlás alapján): napi limit a leggyorsabb reakció,
# utána a csúcstól számított portfolió-DD lépcsői (lásd
# portfolio_drawdown_mult), végül a hard-stop teljes leállás.
DAILY_LOSS_LIMIT_PCT = 0.04   # -4% egy nap alatt → ma nincs több új nyitás
DRAWDOWN_REVIEW_PCT  = 0.12   # -12% csúcstól → csak riasztás + manuális /confirm kell,
                               # a bot NEM áll le, de új pozíciót sem nyit, amíg nincs
                               # megerősítve, hogy a stratégiát átnézték
MAX_DRAWDOWN          = 0.25  # csúcstól -25% → hard stop, bot teljesen leáll
                               # (portfolio_drawdown_mult 20%-nál már letiltja
                               #  az új nyitásokat, tehát ez tudatosan a
                               #  végső, ritkán elért vészfék)

DONCHIAN_LEN      = 20
EMA_TREND_LEN     = 180
EMA_DAILY_LEN     = 200
EMA_1H_LEN        = 20
ATR_LEN           = 14
ATR_MULT          = 1.8   # 1.5 → 1.8 (tágabb stop, kevesebb korai kirázás)
PROFIT_TARGET_ATR = 3.0   # csak MEAN_REV stratégiánál aktív
ADX_LEN           = 14
RSI_LEN           = 14
BB_LEN            = 20
BB_STD            = 2.0

ADX_THRESHOLD         = 20.0   # 22 → 20
MAX_CORR_POSITIONS    = 2
FUNDING_EXTREME_THR   = 0.0008
LOW_LIQUIDITY_HOURS   = (1, 2, 3, 4, 5)
STOP_HUNT_OFFSET_ATR  = 0.3
LIQUIDATION_BUFFER    = 0.10
STOP_BUFFER_PCT       = 0.005
OB_IMBALANCE_MIN      = 1.5
OI_CHANGE_THR         = 0.03
VOL_FILTER_PERCENTILE = 10   # 20 → 10 (kevésbé agresszív)

RSI_OVERSOLD   = 30
RSI_OVERBOUGHT = 70

KELLY_FRACTION    = 0.5
KELLY_MIN_TRADES  = 8
ALPHA_RECENCY     = 0.20
LOSS_STREAK_LIMIT = 3
LOSS_STREAK_CD_H  = 6
LOSS_STREAK_RED   = 0.5
PROFIT_LOCK_PCT   = 0.05
PROFIT_LOCK_RED   = 0.5
WEEKEND_RED       = 0.5

MOMENTUM_LOOKBACK_DAYS = 30   # ÚJ: coin rotáció
TOP_N_COINS            = 4    # ÚJ: top N coin kereskedés

REGIME_CLUSTER_HOURS = 24
WALKFORWARD_DAYS     = 7
WALKFORWARD_WINDOW   = 90

EXECUTION_MODE = "PAPER"
LIVE_TRADING   = (EXECUTION_MODE == "LIVE")
# PAPER és LIVE mostantól külön fájlokat használ — így egy módváltás nem
# keveri össze a papír-egyenleget az éles tőkével.
DB_PATH        = f"bot_v5_{EXECUTION_MODE.lower()}.db"
STATE_FILE_SUFFIX = EXECUTION_MODE.lower()

# FIGYELEM: minden BASE_SYMBOLS-beli coinnak legyen itt bejegyzése — a .get()
# fallback (2 tizedes / min_qty=0) LIVE módban rossz mennyiségű order leadásához
# vezethet. AVAXUSDT korábban hiányzott, pedig a BASE_SYMBOLS tartalmazza.
PRECISIONS       = {"BTCUSDT":3,"ETHUSDT":3,"SOLUSDT":2,"BNBUSDT":2,"FETUSDT":1,"AVAXUSDT":1}
PRICE_PRECISIONS = {"BTCUSDT":1,"ETHUSDT":2,"SOLUSDT":3,"BNBUSDT":2,"FETUSDT":4,"AVAXUSDT":3}
MIN_QTY          = {"BTCUSDT":0.001,"ETHUSDT":0.001,"SOLUSDT":0.01,"BNBUSDT":0.01,"FETUSDT":1,"AVAXUSDT":0.1}

# ── Logging ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot_v5.log"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("probot")

# ══════════════════════════════════════════════════════════════════
#  🗄️  ADATBÁZIS
# ══════════════════════════════════════════════════════════════════
DB_LOCK = threading.Lock()

def db_connect():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def db_init():
    con = db_connect()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, coin TEXT, side TEXT, strategy TEXT, exit_type TEXT,
        entry REAL, exit_price REAL, stake REAL,
        profit_usd REAL, roi_pct REAL, fee_usd REAL, slippage_pct REAL
    );
    CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, coin TEXT, price REAL, daily_price REAL,
        ema_4h REAL, atr REAL, adx REAL, rsi REAL,
        don_high REAL, don_low REAL, ob_ratio REAL, oi_change REAL,
        daily_trend TEXT, regime TEXT, decision TEXT
    );
    CREATE TABLE IF NOT EXISTS equity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, capital REAL, peak REAL, open_pos INTEGER
    );
    CREATE TABLE IF NOT EXISTS config_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, config_json TEXT
    );
    CREATE TABLE IF NOT EXISTS optimized_params (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, params_json TEXT
    );
    """)
    con.commit(); con.close()
    log.info("✅ DB kész: %s", DB_PATH)

def db_exec(sql, params=()):
    with DB_LOCK:
        con = db_connect()
        try:
            con.execute(sql, params); con.commit()
        except Exception as e:
            log.error("DB hiba: %s", e)
        finally:
            con.close()

def db_fetch(sql, params=()):
    with DB_LOCK:
        con = db_connect()
        try:
            return con.execute(sql, params).fetchall()
        except Exception as e:
            log.error("DB fetch hiba: %s", e); return []
        finally:
            con.close()

# ══════════════════════════════════════════════════════════════════
#  🧠  ÁLLAPOT
# ══════════════════════════════════════════════════════════════════
STATE_FILE = f"bot_state_{STATE_FILE_SUFFIX}.json"
state = {
    "total_capital": INITIAL_CAPITAL, "peak_capital": INITIAL_CAPITAL,
    "start_of_day_capital": INITIAL_CAPITAL, "last_report_day": 0,
    "last_rebalance_ts": 0, "loss_streak": 0, "cooldown_until": 0,
    "active_symbols": BASE_SYMBOLS,
    "coin_stats": {c:{"trades":0,"wins":0,"avg_R":0.0,"score":1.0} for c in BASE_SYMBOLS},
    "coin_weights": {c:1.0/len(BASE_SYMBOLS) for c in BASE_SYMBOLS},
    "active_pos": {c:None for c in BASE_SYMBOLS},
    "entry_prices": {c:0.0 for c in BASE_SYMBOLS},
    "stop_losses": {c:0.0 for c in BASE_SYMBOLS},
    "target_prices": {c:0.0 for c in BASE_SYMBOLS},
    "stakes": {c:0.0 for c in BASE_SYMBOLS},
    "strategy_used": {c:"" for c in BASE_SYMBOLS},
    "break_even": {c:False for c in BASE_SYMBOLS},
    "partial_taken": {c:False for c in BASE_SYMBOLS},
    "current_regime": "UNKNOWN", "dynamic_params": {},
    "regimes": {},  # coin-specifikus rezsim (lásd train_regime_model(symbol))
    "corr_matrix": {},
    "btc_atr_pct": 50.0,  # BTC ATR percentilis a vol szűrőhöz
    "_daily_limit_alerted": False,  # napi loss-limit riasztás egyszeri jelzésre
    "_review_pending": False,  # 12%-os DD review-riasztás — /confirm-ig blokkol új nyitást
}

def save_state():
    try:
        with open(STATE_FILE+".tmp","w") as f: json.dump(state, f, indent=2)
        os.replace(STATE_FILE+".tmp", STATE_FILE)
    except Exception as e: log.error("State mentési hiba: %s", e)

def load_state():
    global state
    if not os.path.exists(STATE_FILE):
        save_state(); return
    try:
        with open(STATE_FILE) as f: saved = json.load(f)
        for k, v in saved.items():
            if k in state: state[k] = v
        log.info("💾 Állapot betöltve. Tőke: $%.2f", state["total_capital"])
    except Exception as e: log.error("State betöltési hiba: %s", e)

# ══════════════════════════════════════════════════════════════════
#  📡  TELEGRAM
# ══════════════════════════════════════════════════════════════════
_last_update_id = 0

def tg_send(msg):
    try:
        requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            params={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10
        )
    except Exception as e: log.warning("Telegram hiba: %s", e)

def tg_check():
    global _last_update_id
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": _last_update_id+1, "timeout":1}, timeout=10
        ).json()
        for upd in r.get("result", []):
            _last_update_id = upd["update_id"]
            txt = upd.get("message",{}).get("text","").lower().strip()
            if txt == "/status":
                syms = state["active_symbols"]
                active = [c for c in syms if state["active_pos"].get(c)]
                peak = state.get("peak_capital", state["total_capital"])
                dd   = (peak - state["total_capital"]) / peak * 100 if peak > 0 else 0
                dd_mult = portfolio_drawdown_mult()
                dd_status = "✅ OK" if dd_mult==1.0 else ("⚠️ Csökkentett méret" if dd_mult>0 else "🛑 Nincs nyitás!")
                review = "⚠️ FÜGGŐ — küldj /confirm-ot" if state.get("_review_pending", False) else "✅ nincs"
                tg_send(f"📊 Tőke: ${state['total_capital']:.2f}\n"
                        f"📈 Nyitott: {active or 'Nincs'}\n"
                        f"🌊 Rezsim (BTC): {state['current_regime']}\n"
                        f"📉 Csúcs: ${peak:.2f} | DD: {dd:.1f}% {dd_status}\n"
                        f"🔍 Review-blokk: {review}\n"
                        f"🔄 Mód: {EXECUTION_MODE}\n"
                        f"🪙 Aktív coinok: {', '.join(syms)}")
            elif txt == "/coins":
                lines = ["📊 COIN STATISZTIKÁK:"]
                for c in state["active_symbols"]:
                    st = state["coin_stats"].get(c, {})
                    wr = st["wins"]/st["trades"]*100 if st.get("trades",0) > 0 else 0
                    w  = state["coin_weights"].get(c, 0)
                    lines.append(f"{c}: {st.get('trades',0)} trade | {wr:.0f}% WR | súly: {w*100:.0f}%")
                tg_send("\n".join(lines))
            elif txt == "/panic" or txt == "/closeall":
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
                tg_send("✅ Minden pozíció likvidálva.")
            elif txt == "/confirm":
                if state.get("_review_pending", False):
                    state["_review_pending"] = False
                    save_state()
                    tg_send("✅ Review megerősítve — a bot újra nyithat pozíciót.")
                else:
                    tg_send("Nincs függő review-riasztás.")
            elif txt == "/help":
                tg_send("🤖 PARANCSOK:\n/status /coins /panic /confirm /help")
    except Exception as e: log.debug("tg_check hiba: %s", e)

# ══════════════════════════════════════════════════════════════════
#  🔗  BINANCE API
# ══════════════════════════════════════════════════════════════════
BINANCE_BASE = "https://fapi.binance.com"
BINANCE_TEST = "https://testnet.binancefuture.com"
_host = BINANCE_TEST if not LIVE_TRADING else BINANCE_BASE

def _sign(params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    params["signature"] = hmac.new(BINANCE_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    return params

def _bget(path, params=None, signed=False, fb=True):
    try:
        p = params.copy() if params else {}
        h = {"X-MBX-APIKEY": BINANCE_API_KEY}
        if signed: p["timestamp"] = int(time.time()*1000); p = _sign(p)
        r = requests.get(f"{_host}{path}", params=p, headers=h, timeout=10)
        r.raise_for_status(); return r.json()
    except Exception as e:
        log.warning("Binance GET hiba (%s): %s", path, e)
        if fb and HAS_BYBIT: return _bybit_fallback(path, params)
        return None

def _bpost(path, params):
    try:
        p = params.copy()
        p["timestamp"] = int(time.time()*1000); p = _sign(p)
        h = {"X-MBX-APIKEY": BINANCE_API_KEY}
        r = requests.post(f"{_host}{path}", params=p, headers=h, timeout=10)
        return r.json()
    except Exception as e: log.error("Binance POST hiba: %s", e); return None

def _bdelete(path, params):
    try:
        p = params.copy()
        p["timestamp"] = int(time.time()*1000); p = _sign(p)
        h = {"X-MBX-APIKEY": BINANCE_API_KEY}
        r = requests.delete(f"{_host}{path}", params=p, headers=h, timeout=10)
        resp = r.json()
        if r.status_code != 200:
            log.warning("Binance DELETE API hiba (%s): %s", r.status_code, resp)
        return resp
    except Exception as e: log.error("Binance DELETE hiba: %s", e); return None

def _bybit_fallback(binance_path, params):
    if "/klines" not in binance_path: return None
    try:
        symbol = params.get("symbol","BTCUSDT")
        im = {"1h":"60","4h":"240","1d":"D"}
        iv = im.get(params.get("interval","4h"),"240")
        r  = requests.get("https://api.bybit.com/v5/market/kline",
                          params={"category":"linear","symbol":symbol,
                                  "interval":iv,"limit":params.get("limit",210)},
                          timeout=10).json()
        candles = []
        for row in reversed(r.get("result",{}).get("list",[])):
            candles.append([int(row[0]),row[1],row[2],row[3],row[4],row[5],
                            int(row[0])+1,"0","0","0","0","0"])
        log.info("✅ Bybit fallback: %s", symbol)
        return candles
    except Exception as e: log.error("Bybit fallback hiba: %s", e); return None

# ══════════════════════════════════════════════════════════════════
#  📊  PIACI ADATOK
# ══════════════════════════════════════════════════════════════════
def fetch_klines(symbol, interval, limit=210):
    data = _bget("/fapi/v1/klines", {"symbol":symbol,"interval":interval,"limit":limit})
    if not data: return pd.DataFrame()
    try:
        df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume",
                                         "close_ts","qv","trades","tbb","tbq","ign"])
        for c in ["open","high","low","close","volume"]: df[c] = df[c].astype(float)
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df.set_index("ts")
    except Exception as e: log.error("Kline parse hiba: %s", e); return pd.DataFrame()

def fetch_ob_imbalance(symbol):
    data = _bget("/fapi/v1/depth", {"symbol":symbol,"limit":10}, fb=False)
    if not data: return 1.0
    try:
        bv = sum(float(b[1]) for b in data.get("bids",[]))
        av = sum(float(a[1]) for a in data.get("asks",[]))
        return round(bv/av, 4) if av > 0 else 1.0
    except: return 1.0

def fetch_oi_change(symbol):
    data = _bget("/futures/data/openInterestHist",
                 {"symbol":symbol,"period":"4h","limit":2}, fb=False)
    if not data or len(data) < 2: return 0.0
    try:
        n = float(data[-1]["sumOpenInterest"]); p = float(data[-2]["sumOpenInterest"])
        return (n-p)/p if p > 0 else 0.0
    except: return 0.0

def fetch_funding(symbol):
    data = _bget("/fapi/v1/fundingRate", {"symbol":symbol,"limit":1}, fb=False)
    if not data: return 0.0
    try: return float(data[-1]["fundingRate"])
    except: return 0.0

# ÚJ: Dinamikus coin lista frissítése (top N momentum alapján)
def update_active_symbols():
    log.info("🔄 Coin rotáció frissítése...")
    try:
        # Összes USDT linear futures szimbólum
        info = _bget("/fapi/v1/exchangeInfo", fb=False)
        if not info: return
        all_syms = [s["symbol"] for s in info.get("symbols",[])
                    if s.get("quoteAsset")=="USDT" and s.get("status")=="TRADING"
                    and s.get("contractType")=="PERPETUAL"]

        # 30 napos momentum
        scores = {}
        for sym in all_syms[:50]:  # top 50 forgalmú, hogy ne vegyen el örökké
            df = fetch_klines(sym, "1d", limit=MOMENTUM_LOOKBACK_DAYS+1)
            if df.empty or len(df) < 10: continue
            ret = (df["close"].iloc[-1] - df["close"].iloc[0]) / df["close"].iloc[0]
            vol = df["volume"].iloc[-MOMENTUM_LOOKBACK_DAYS:].mean()
            # Momentum × volume kombinált score
            scores[sym] = float(ret) * float(vol)
            time.sleep(0.1)

        # A BASE_SYMBOLS (backtest-validált 4 fő coin) MINDIG bent marad —
        # a rotáció csak a TOP_N_COINS-on felüli extra helyekre válogat
        # momentum alapján, hogy ne írja felül a bizonyított alapkonfigot.
        core = list(BASE_SYMBOLS)
        extra_slots = max(0, TOP_N_COINS - len(core))
        candidates = [s for s in sorted(scores, key=scores.get, reverse=True) if s not in core]
        extra = candidates[:extra_slots]
        selected = core + extra

        old = state["active_symbols"]
        if set(selected) != set(old):
            state["active_symbols"] = selected
            # Új coinok state initiálása
            for c in selected:
                if c not in state["coin_stats"]:
                    state["coin_stats"][c] = {"trades":0,"wins":0,"avg_R":0.0,"score":1.0}
                if c not in state["active_pos"]:
                    state["active_pos"][c] = None
                    state["entry_prices"][c] = 0.0
                    state["stop_losses"][c]  = 0.0
                    state["target_prices"][c]= 0.0
                    state["stakes"][c]       = 0.0
                    state["strategy_used"][c]= ""
                    state["break_even"][c]   = False
                    state["partial_taken"][c]= False
            save_state()
            tg_send(f"🔄 Coin rotáció!\nRégi: {', '.join(old)}\nÚj: {', '.join(selected)}\n"
                    f"(alap 4: {', '.join(core)} mindig bent, csak az extra helyek rotálnak)")
            log.info("Coin rotáció: %s → %s", old, selected)
        else:
            log.info("Coin rotáció: nincs változás (%s)", selected)

    except Exception as e: log.error("Coin rotáció hiba: %s", e)

# ÚJ: BTC volatilitás percentilis frissítése
def update_vol_filter():
    df = fetch_klines("BTCUSDT", "4h", limit=VOL_FILTER_LOOKBACK+5)
    if df.empty: return
    try:
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"]  - df["close"].shift()).abs(),
        ], axis=1).max(axis=1)
        atr  = tr.rolling(14).mean()
        cur  = float(atr.iloc[-1])
        hist = atr.dropna().values
        pct  = float(np.mean(hist <= cur)) * 100
        state["btc_atr_pct"] = pct
        log.info("Vol szűrő: BTC ATR percentilis = %.1f%%", pct)
    except Exception as e: log.error("Vol szűrő hiba: %s", e)

# ÚJ: gördülő árkorreláció-mátrix az aktív coinok között
CORRELATION_LOOKBACK_DAYS = 30
CORRELATION_THRESHOLD     = 0.7   # e fölött "ugyanaz a kockázat" — lásd correlated_exposure_count

def update_correlation_matrix():
    """
    30 napos gördülő korreláció az aktív coinok napi hozamai között.
    Ez váltja ki az egyszerű LONG/SHORT-számlálást: két coin csak akkor
    számít "ugyanannak a kockázatnak", ha ténylegesen együtt mozog —
    BTC/SOL/AVAX trendben tipikusan igen, de ez adatból derül ki, nem
    feltételezésből.
    """
    try:
        syms = list(state["active_symbols"])
        closes = {}
        for s in syms:
            df = fetch_klines(s, "1d", limit=CORRELATION_LOOKBACK_DAYS+2)
            if not df.empty and len(df) > 5:
                closes[s] = df["close"].pct_change().dropna()
        if len(closes) < 2:
            return
        mat = {}
        for a in closes:
            mat[a] = {}
            for b in closes:
                if a == b:
                    mat[a][b] = 1.0; continue
                joined = pd.concat([closes[a], closes[b]], axis=1).dropna()
                if len(joined) < 5:
                    mat[a][b] = 1.0  # kevés adat → óvatosan korreláltnak vesszük
                    continue
                c = float(joined.iloc[:,0].corr(joined.iloc[:,1]))
                mat[a][b] = 0.0 if np.isnan(c) else c
        state["corr_matrix"] = mat
        save_state()
        log.info("📈 Korrelációs mátrix frissítve (%d coin)", len(mat))
    except Exception as e:
        log.error("Korreláció frissítési hiba: %s", e)

def correlated_exposure_count(coin, side):
    """
    Hány, ugyanolyan irányú (side) nyitott pozíció van olyan coinban, amely
    a korrelációs mátrix szerint >= CORRELATION_THRESHOLD mértékben együtt
    mozog `coin`-nal. Ha nincs még mátrix-adat egy párra, konzervatívan
    korreláltnak vesszük (biztonsági alapállás).
    """
    mat = state.get("corr_matrix", {})
    row = mat.get(coin)
    n = 0
    for c in state["active_symbols"]:
        if c == coin or state["active_pos"].get(c) != side:
            continue
        corr = row.get(c) if row else None
        if corr is None or abs(corr) >= CORRELATION_THRESHOLD:
            n += 1
    return n

VOL_FILTER_LOOKBACK = 90 * 6  # 90 nap × 6 gyertya/nap

# ══════════════════════════════════════════════════════════════════
#  📐  INDIKÁTOROK
# ══════════════════════════════════════════════════════════════════
def ind_ema(series, period):
    return float(series.ewm(span=period, adjust=False).mean().iloc[-1])

def ind_atr(df, length=14):
    tr = pd.concat([
        df["high"]-df["low"],
        (df["high"]-df["close"].shift()).abs(),
        (df["low"] -df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return float(tr.rolling(length).mean().iloc[-1])

def ind_adx(df, length=14):
    up   = df["high"].diff(); down = -df["low"].diff()
    pdm  = np.where((up>down)&(up>0), up, 0.0)
    mdm  = np.where((down>up)&(down>0), down, 0.0)
    tr   = pd.concat([df["high"]-df["low"],
                       (df["high"]-df["close"].shift()).abs(),
                       (df["low"] -df["close"].shift()).abs()], axis=1).max(axis=1)
    atr_s= tr.ewm(span=length, adjust=False).mean()
    pdi  = 100*pd.Series(pdm,index=df.index).ewm(span=length,adjust=False).mean()/atr_s
    mdi  = 100*pd.Series(mdm,index=df.index).ewm(span=length,adjust=False).mean()/atr_s
    dx   = 100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)
    return float(dx.ewm(span=length,adjust=False).mean().iloc[-1])

def ind_rsi(series, length=14):
    d = series.diff()
    g = d.clip(lower=0).ewm(span=length,adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(span=length,adjust=False).mean()
    return float((100-100/(1+g/l.replace(0,1e-9))).iloc[-1])

def ind_bollinger(series, length=20, ns=2.0):
    mid = float(series.rolling(length).mean().iloc[-1])
    std = float(series.rolling(length).std().iloc[-1])
    return mid, mid+ns*std, mid-ns*std

def ind_donchian(df, length=20):
    return float(df["high"].iloc[-length-1:-1].max()), float(df["low"].iloc[-length-1:-1].min())

def ind_eff_mult(df, atr, base=None, lb=30):
    if base is None:
        base = state.get("dynamic_params", {}).get("values", {}).get("atr_mult", ATR_MULT)
    avg = float(df["close"].rolling(lb).apply(
        lambda x: pd.Series(x).pct_change().std(), raw=False
    ).iloc[-1]) if len(df) > lb else 0
    ratio = atr/float(df["close"].iloc[-1]) / avg if avg > 0 else 1.0
    if ratio > 1.3: return max(1.0, base*0.8)
    if ratio < 0.7: return min(2.5, base*1.3)
    return base

# ══════════════════════════════════════════════════════════════════
#  🌊  REZSIM-KLASZTEREZÉS
# ══════════════════════════════════════════════════════════════════
_regime_model  = None
_regime_scaler = StandardScaler()
_regime_lock   = threading.Lock()

def train_regime_model(symbol="BTCUSDT"):
    """
    Rezsim-klaszterezés egy adott coinra. Korábban ez csak BTC-n futott és
    minden coinra ugyanazt a rezsimet húzta rá — az altcoinok (SOL, AVAX)
    viszont gyakran más fázisban vannak, mint BTC, ezért ezt most
    coin-onként külön futtatjuk (lásd main() ütemezés: BASE_SYMBOLS-re).
    """
    global _regime_model, _regime_scaler
    log.info("🌊 Rezsim-klaszterezés indul (%s)...", symbol)
    try:
        df = fetch_klines(symbol, "4h", limit=360)
        if df.empty or len(df) < 50: return
        adx_s, atr_s, rsi_s, tr_s = [], [], [], []
        for i in range(20, len(df)):
            sub = df.iloc[:i+1]
            adx_s.append(ind_adx(sub))
            atr_s.append(ind_atr(sub) / float(sub["close"].iloc[-1]))
            rsi_s.append(ind_rsi(sub["close"]))
            tr_s.append((float(sub["close"].iloc[-1])-float(sub["close"].iloc[-20]))/float(sub["close"].iloc[-20]))
        features = pd.DataFrame({"adx":adx_s,"atr":atr_s,"rsi":rsi_s,"trend":tr_s}).dropna()
        if len(features) < 10: return
        # Minden coinnak saját, csak rá illesztett modellje van (nem osztott
        # globális _regime_model), így a rezsim-küszöbök is coin-specifikusak.
        scaler = StandardScaler()
        X = scaler.fit_transform(features)
        model = KMeans(n_clusters=4, n_init=10, random_state=42)
        model.fit(X)
        cur = features.iloc[-1:]
        cl = int(model.predict(scaler.transform(cur))[0])
        centroids = model.cluster_centers_
        labels = {}
        for i, c in enumerate(centroids):
            if c[0] > 0.5 and c[3] > 0.3:   labels[i] = "BULL_TREND"
            elif c[0] > 0.5 and c[3] < -0.3: labels[i] = "BEAR_TREND"
            elif c[1] > 0.5:                  labels[i] = "HIGH_VOL_CHOP"
            else:                             labels[i] = "LOW_VOL_CHOP"
        regime = labels.get(cl, f"CLUSTER_{cl}")
        state.setdefault("regimes", {})[symbol] = regime
        if symbol == "BTCUSDT":
            state["current_regime"] = regime  # visszafele kompatibilis: napi jelentés, /status
            with _regime_lock:
                _regime_model, _regime_scaler = model, scaler
        save_state()
        log.info("🌊 Rezsim (%s): %s", symbol, regime)
        tg_send(f"🌊 Rezsim frissítve — {symbol}: {regime}")
    except Exception as e: log.error("Rezsim hiba (%s): %s", symbol, e)

def get_coin_regime(coin):
    return state.get("regimes", {}).get(coin, state.get("current_regime", "UNKNOWN"))

def train_regime_models_all():
    """Coin-specifikus rezsimfelismerés minden aktív coinra (BTC + a többi BASE_SYMBOLS)."""
    for sym in state.get("active_symbols", BASE_SYMBOLS):
        try:
            train_regime_model(sym)
        except Exception as e:
            log.error("Rezsim hiba (%s): %s", sym, e)
        time.sleep(1)

# ══════════════════════════════════════════════════════════════════
#  🔬  WALK-FORWARD ÖNOPTIMALIZÁLÁS (ténylegesen visszaír a stratégiába)
# ══════════════════════════════════════════════════════════════════
DYNAMIC_PARAM_BOUNDS = {
    "adx_threshold": (16.0, 28.0),
    "atr_mult":      (1.3, 2.5),
}

def walkforward_optimize():
    log.info("🔬 Walk-forward indul...")
    try:
        cutoff = time.time() - WALKFORWARD_WINDOW*86400
        rows = db_fetch(
            "SELECT adx, rsi, ob_ratio, oi_change, decision FROM decisions WHERE ts>? AND decision LIKE 'CLOSE_%'",
            (cutoff,)
        )
        if len(rows) < 20:
            log.info("🔬 Nem elég adat (%d trade)", len(rows)); return
        df = pd.DataFrame(rows, columns=["adx","rsi","ob_ratio","oi_change","decision"])
        df["profit"] = df["decision"].apply(lambda d: 1 if "LONG" in d or "SHORT" in d else 0)
        X = df[["adx","rsi","ob_ratio","oi_change"]].fillna(0).values
        y = df["profit"].values
        sc = StandardScaler(); Xs = sc.fit_transform(X)
        m  = LogisticRegression(max_iter=500, random_state=42); m.fit(Xs, y)
        imp = dict(zip(["adx","rsi","ob_ratio","oi_change"], np.abs(m.coef_[0])))

        trades = db_fetch("SELECT profit_usd, exit_type FROM trades WHERE ts>?", (cutoff,))
        n_trades = len(trades)
        wr = sum(1 for (p,_) in trades if p>0)/n_trades if n_trades else 0
        stopouts = sum(1 for (_,et) in trades if et=="STOP")
        stopout_rate = stopouts/n_trades if n_trades else 0

        # ── Tényleges visszacsatolás a stratégiába ──────────────────
        # Jelenlegi (előző körből átvett vagy alap) dinamikus paraméterek
        prev = state.get("dynamic_params", {}).get("values", {})
        adx_thr = prev.get("adx_threshold", ADX_THRESHOLD)
        atr_m   = prev.get("atr_mult", ATR_MULT)

        # ADX küszöb: rossz win rate → szigorúbb (magasabb) küszöb kell,
        # jó win rate → lehet lazábban is szűrni, több jelzést engedve be.
        if n_trades >= 20:
            if wr < 0.45:
                adx_thr += 1.0
            elif wr > 0.60:
                adx_thr -= 1.0

        # ATR szorzó: ha a lezárások többsége STOP (nem profit target/trailing
        # nyereséggel zárt), az arra utal, hogy a stopok túl szorosak —
        # tágítjuk. Fordítva: ha ritkán stopol ki, szűkíthető a kockázat.
        if n_trades >= 20:
            if stopout_rate > 0.65:
                atr_m += 0.1
            elif stopout_rate < 0.35:
                atr_m -= 0.1

        lo, hi = DYNAMIC_PARAM_BOUNDS["adx_threshold"]; adx_thr = max(lo, min(hi, adx_thr))
        lo, hi = DYNAMIC_PARAM_BOUNDS["atr_mult"];       atr_m   = max(lo, min(hi, atr_m))

        old_adx, old_atr = prev.get("adx_threshold", ADX_THRESHOLD), prev.get("atr_mult", ATR_MULT)

        params = {
            "ts": time.time(),
            "feature_importances": imp,
            "win_rate": wr,
            "n": len(rows),
            "n_trades": n_trades,
            "stopout_rate": stopout_rate,
            "values": {"adx_threshold": adx_thr, "atr_mult": atr_m},
        }
        db_exec("INSERT INTO optimized_params (ts, params_json) VALUES (?,?)",
                (time.time(), json.dumps(params)))
        state["dynamic_params"] = params; save_state()

        top_feat = max(imp, key=imp.get)
        adx_arrow = "↑" if adx_thr>old_adx else ("↓" if adx_thr<old_adx else "→")
        atr_arrow = "↑" if atr_m>old_atr else ("↓" if atr_m<old_atr else "→")
        tg_send(
            "🔬 WALK-FORWARD OPTIMALIZÁLÁS\n"
            f"Időablak: utolsó {WALKFORWARD_WINDOW} nap\n"
            f"Trade-ek: {n_trades} | Win rate: {wr*100:.1f}%\n"
            f"Stop-kizárási arány: {stopout_rate*100:.1f}%\n"
            f"Legfontosabb szignál: {top_feat}\n"
            "── Frissített paraméterek ──\n"
            f"ADX küszöb: {old_adx:.1f} → {adx_thr:.1f} {adx_arrow}\n"
            f"ATR szorzó: {old_atr:.2f} → {atr_m:.2f} {atr_arrow}"
        )
        log.info("🔬 Walk-forward kész: ADX %.1f→%.1f | ATR_MULT %.2f→%.2f",
                  old_adx, adx_thr, old_atr, atr_m)
    except Exception as e: log.error("Walk-forward hiba: %s", e)

def get_adx_threshold():
    return state.get("dynamic_params", {}).get("values", {}).get("adx_threshold", ADX_THRESHOLD)

# ══════════════════════════════════════════════════════════════════
#  ⚖️  KOCKÁZATKEZELÉS
# ══════════════════════════════════════════════════════════════════
def kelly_frac(coin):
    st = state["coin_stats"].get(coin, {"trades":0,"wins":0,"avg_R":0.0})
    if st["trades"] < KELLY_MIN_TRADES: return BASE_RISK_PCT
    wr = st["wins"]/st["trades"]; ar = st["avg_R"]
    if ar <= 0: return BASE_RISK_PCT*0.5
    k = max(0, wr-(1-wr)/max(ar,0.01)) * KELLY_FRACTION
    return max(BASE_RISK_PCT*0.3, min(k, BASE_RISK_PCT*2.0))

def portfolio_drawdown_mult() -> float:
    """
    Portfolió-szintű drawdown monitor — többlépcsős fék, összhangban a
    main-loop-beli végső vészfékkel (MAX_DRAWDOWN, lásd lent):
      - 10-15%: 75%-os pozícióméret (óvatosság)
      - 15-20%: felezett pozícióméret
      - 20%+:   nincs új nyitás, de a nyitott pozíciók stop/trailing
                logikája tovább fut (nem kényszerzárás)
      - MAX_DRAWDOWN (25%): a main loop teljesen leállítja a botot.
    A két szint (itt 20%, lent 25%) tudatosan van szétválasztva: előbb
    csak új kockázatvállalást tiltunk (soft stop), utána jön a hard stop.
    """
    peak = state.get("peak_capital", state["total_capital"])
    if peak <= 0:
        return 1.0
    dd = (peak - state["total_capital"]) / peak

    if dd >= 0.20:
        return 0.0
    elif dd >= 0.15:
        return 0.5
    elif dd >= 0.10:
        return 0.75
    return 1.0

def daily_loss_breached() -> bool:
    """
    Napi veszteség-limit — külön réteg a csúcstól-számított portfolió
    drawdown mellett. Ez a start_of_day_capital-hoz képesti napi
    visszaesést nézi, és sokkal gyorsabban reagál egy rossz napra
    (API hiba, flash crash, egy kifejezetten rossz szignálnap), mint
    a csúcstól számított, lassabban mozgó drawdown-monitor.
    """
    soc = state.get("start_of_day_capital", state["total_capital"])
    if soc <= 0: return False
    daily_pnl_pct = (state["total_capital"] - soc) / soc
    return daily_pnl_pct <= -DAILY_LOSS_LIMIT_PCT

def reset_daily_loss_window():
    """Minden nap 00:00-kor fut: új napi bázis-tőke, napi loss-limit riasztás újra-fegyverzése."""
    state["start_of_day_capital"] = state["total_capital"]
    state["_daily_limit_alerted"] = False
    save_state()
    log.info("🔄 Napi loss-limit ablak nullázva. Bázis-tőke: $%.2f", state["total_capital"])

def risk_mult():
    m = 1.0
    if state["loss_streak"] > 0: m *= LOSS_STREAK_RED
    soc = state.get("start_of_day_capital", state["total_capital"])
    if soc > 0 and (state["total_capital"]-soc)/soc >= PROFIT_LOCK_PCT: m *= PROFIT_LOCK_RED
    if datetime.now().weekday() >= 5: m *= WEEKEND_RED
    # Portfolió drawdown szorzó is beépül
    m *= portfolio_drawdown_mult()
    return m

def can_open_new_position() -> bool:
    """
    True ha szabad új pozíciót nyitni. Letiltja, ha a portfolió-drawdown
    kritikus szinten van, a napi loss-limit sérült, VAGY ha egy 12%-os
    review-riasztás vár manuális /confirm-ra (lásd DRAWDOWN_REVIEW_PCT).
    """
    if state.get("_review_pending", False):
        return False
    if daily_loss_breached():
        return False
    return portfolio_drawdown_mult() > 0.0

def is_cooldown():
    return time.time() < state.get("cooldown_until", 0)

def register_result(pnl):
    if pnl > 0: state["loss_streak"] = 0
    else:
        state["loss_streak"] = state.get("loss_streak",0)+1
        if state["loss_streak"] >= LOSS_STREAK_LIMIT:
            state["cooldown_until"] = time.time()+LOSS_STREAK_CD_H*3600
            state["loss_streak"] = 0
            tg_send(f"🧊 {LOSS_STREAK_LIMIT} veszteség egymás után! {LOSS_STREAK_CD_H}h hűtés.")

def update_score(coin, pnl):
    st = state["coin_stats"].setdefault(coin,{"trades":0,"wins":0,"avg_win":0.0,"avg_loss":0.0,"score":1.0})
    st["trades"]+=1
    if pnl > 0:
        st["wins"]+=1
        st["avg_win"] = st.get("avg_win",0.0)*(1-ALPHA_RECENCY) + pnl*ALPHA_RECENCY
    else:
        st["avg_loss"] = st.get("avg_loss",0.0)*(1-ALPHA_RECENCY) + abs(pnl)*ALPHA_RECENCY
    wr = st["wins"]/st["trades"]
    aw = st.get("avg_win", 0.0); al = st.get("avg_loss", 0.0)
    ar = (aw / al) if al > 0 else 1.0
    st["score"] = max(0.05, wr * (1 + ar))

def rebalance():
    with STATE_LOCK:
        syms  = state["active_symbols"]
        
        # Calculate inverse volatility (ATR Parity) for each coin
        inv_vols = {}
        for s in syms:
            try:
                df = fetch_klines(s, "1d", limit=20)
                if len(df) >= 14:
                    atr = ind_atr(df, 14)
                    price = float(df["close"].iloc[-1])
                    atr_pct = atr / price if price > 0 else 0.1
                    inv_vols[s] = 1.0 / atr_pct
                else:
                    inv_vols[s] = 1.0
            except:
                inv_vols[s] = 1.0
                
        # Combine performance score with inverse volatility
        combined_scores = {}
        for s in syms:
            sc = state["coin_stats"].get(s, {}).get("score", 1.0)
            combined_scores[s] = sc * inv_vols.get(s, 1.0)
            
        total = sum(combined_scores.values()) if sum(combined_scores.values()) > 0 else 1.0
        
        for s in syms:
            raw_w = combined_scores[s] / total
            state["coin_weights"][s] = max(MIN_WEIGHT, min(MAX_WEIGHT, raw_w))
            
        wt = sum(state["coin_weights"].get(s,0) for s in syms)
        for s in syms: state["coin_weights"][s] /= (wt if wt > 0 else 1.0)

def apply_fee(stake):
    f = stake*LEVERAGE*TAKER_FEE_PCT
    state["total_capital"] -= f
    return f

def liq_price(entry, side, lev=LEVERAGE, mmr=0.005):
    return entry*(1-1/lev+mmr) if side=="LONG" else entry*(1+1/lev-mmr)

def count_dir(side):
    return sum(1 for c in state["active_symbols"] if state["active_pos"].get(c)==side)

def get_qty(coin, stake, price):
    qty = round((stake*LEVERAGE)/price, PRECISIONS.get(coin,2))
    return qty if qty >= MIN_QTY.get(coin,0) else 0.0

def detect_rsi_divergence(df_4h: pd.DataFrame) -> str:
    """
    RSI divergencia detektálás — extra szűrő a fals trend-jelekhez.

    Bearish divergencia: az ár új csúcsot csinál, de az RSI nem (gyengülő momentum)
      → LONG belépést blokkol

    Bullish divergencia: az ár új mélypontot csinál, de az RSI nem (gyengülő eladói nyomás)
      → SHORT belépést blokkol

    Visszatér: "BEARISH_DIV", "BULLISH_DIV", vagy "NONE"
    """
    if len(df_4h) < 30:
        return "NONE"
    try:
        close = df_4h["close"].values
        # RSI kiszámítása az utolsó 30 gyertyára
        delta = pd.Series(close).diff()
        gain  = delta.clip(lower=0).ewm(span=RSI_LEN, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(span=RSI_LEN, adjust=False).mean()
        rsi   = (100 - 100/(1 + gain/loss.replace(0, 1e-9))).values

        # Az utolsó 20 gyertya csúcsait/mélypontjait nézzük
        lookback = 20
        prices = close[-lookback:]
        rsis   = rsi[-lookback:]

        # Utolsó két árkülönbség meghatározása
        last_high_idx  = np.argmax(prices[:-5])
        last_high2_idx = np.argmax(prices[-5:]) + (lookback - 5)

        last_low_idx   = np.argmin(prices[:-5])
        last_low2_idx  = np.argmin(prices[-5:]) + (lookback - 5)

        # Bearish divergencia: ár magasabb csúcs, RSI alacsonyabb csúcs
        if (prices[last_high2_idx] > prices[last_high_idx] and
                rsis[last_high2_idx] < rsis[last_high_idx] - 3):
            return "BEARISH_DIV"

        # Bullish divergencia: ár alacsonyabb mélypont, RSI magasabb mélypont
        if (prices[last_low2_idx] < prices[last_low_idx] and
                rsis[last_low2_idx] > rsis[last_low_idx] + 3):
            return "BULLISH_DIV"

        return "NONE"
    except Exception:
        return "NONE"

# ══════════════════════════════════════════════════════════════════
#  📝  NAPLÓZÁS
# ══════════════════════════════════════════════════════════════════
def log_dec(coin, price, dp, ema, atr, adx, rsi, dh, dl, ob, oi, dt, dec):
    db_exec("""INSERT INTO decisions
               (ts,coin,price,daily_price,ema_4h,atr,adx,rsi,
                don_high,don_low,ob_ratio,oi_change,daily_trend,regime,decision)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(),coin,price,dp,ema,atr,adx,rsi,dh,dl,ob,oi,dt,
             get_coin_regime(coin),dec))

def log_trade_db(coin, side, strat, exit_type, entry, exit_p, stake, profit, roi, fee, slip=0.0):
    db_exec("""INSERT INTO trades
               (ts,coin,side,strategy,exit_type,entry,exit_price,stake,
                profit_usd,roi_pct,fee_usd,slippage_pct)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(),coin,side,strat,exit_type,entry,exit_p,stake,profit,roi,fee,slip))

def log_equity():
    op = sum(1 for c in state["active_symbols"] if state["active_pos"].get(c))
    db_exec("INSERT INTO equity (ts,capital,peak,open_pos) VALUES(?,?,?,?)",
            (time.time(), state["total_capital"], state["peak_capital"], op))

# ══════════════════════════════════════════════════════════════════
#  📤  ORDER VÉGREHAJTÁS (idempotens clientOrderId + Bybit vészút)
# ══════════════════════════════════════════════════════════════════
def _make_client_order_id(sym, side, purpose, bucket_seconds=30):
    """
    Determinisztikus clientOrderId egy adott szándékra (pl. "AVAXUSDT-SELL-EXIT"),
    egy rövid (bucket_seconds) időablakra kerekítve. Ha egy retry ugyanabban az
    ablakban fut le, ugyanazt az ID-t kapja → az exchange nem enged duplikált
    orderId-t, így egy timeout utáni retry nem tud véletlenül duplán
    nyitni/zárni, ha az előző kísérlet valójában már végrehajtódott.
    """
    bucket = int(time.time() // bucket_seconds)
    raw = f"{sym}-{side}-{purpose}-{bucket}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:20]
    return f"pb5-{h}"

def _query_order_by_client_id(sym, client_order_id):
    """Megnézi, hogy egy korábbi kísérlet clientOrderId-ja alatt már létezik-e order Binance-on."""
    return _bget("/fapi/v1/order",
                 {"symbol": sym, "origClientOrderId": client_order_id},
                 signed=True, fb=False)

def _bybit_signed_post(path, body: dict):
    """Minimál Bybit v5 signed POST — csak vészhelyzeti (fallback) zárásra használjuk."""
    if not (HAS_BYBIT and BYBIT_API_KEY and BYBIT_SECRET):
        return None
    try:
        ts = str(int(time.time()*1000))
        recv_window = "5000"
        body_json = json.dumps(body, separators=(",", ":"))
        sign_payload = ts + BYBIT_API_KEY + recv_window + body_json
        sign = hmac.new(BYBIT_SECRET.encode(), sign_payload.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window, "X-BAPI-SIGN": sign,
            "Content-Type": "application/json",
        }
        r = requests.post(f"https://api.bybit.com{path}", headers=headers, data=body_json, timeout=10)
        return r.json()
    except Exception as e:
        log.error("Bybit signed POST hiba: %s", e); return None

def _bybit_emergency_close(sym, side, qty):
    """
    Másodlagos végrehajtási útvonal: ha a Binance zárás LIVE módban 3x
    sikertelen, megpróbáljuk piaci áron lezárni a pozíciót egy Bybit
    tükörszámlán (ha van konfigurálva). Ez CSAK vészhelyzeti zárásra való,
    nem helyettesíti a fő execution-t — feltételezi, hogy a felhasználó
    Bybiten is tart fedezetet/tükörpozíciót erre az esetre.
    """
    if not (HAS_BYBIT and BYBIT_API_KEY and BYBIT_SECRET):
        return False, 0.0
    body = {"category": "linear", "symbol": sym, "side": side,
            "orderType": "Market", "qty": str(qty), "reduceOnly": True}
    resp = _bybit_signed_post("/v5/order/create", body)
    ok = bool(resp and resp.get("retCode") == 0)
    if ok:
        log.warning("🆘 Bybit vész-zárás sikeres: %s %s %s", sym, side, qty)
        tg_send(f"🆘 Bybit vész-zárás VÉGREHAJTVA: {sym} {side} {qty} "
                f"(Binance execution 3x sikertelen volt!)")
    else:
        log.error("Bybit vész-zárás is sikertelen: %s", resp)
    return ok, 0.0

def execute_order(sym, side, qty, order_type="LIMIT", price=None, purpose="ENTRY"):
    if not LIVE_TRADING:
        log.info("📄 PAPER: %s %s %s @ %.4f", sym, side, qty, price or 0)
        return True, float(price or 0)

    client_id = _make_client_order_id(sym, side, purpose)

    # Idempotencia-ellenőrzés: lehet, hogy egy korábbi (timeoutolt) kísérlet
    # ugyanezzel a client_id-vel már ténylegesen végrehajtódott.
    existing = _query_order_by_client_id(sym, client_id)
    if existing and existing.get("status") in ("FILLED", "PARTIALLY_FILLED"):
        fill = float(existing.get("avgPrice") or price or 0)
        log.info("♻️ Order már létezett (idempotencia-találat): %s %s fill=%.4f", sym, side, fill)
        return True, fill

    params = {"symbol":sym,"side":side,"quantity":qty,"newClientOrderId":client_id}
    if order_type=="LIMIT" and price:
        pp = PRICE_PRECISIONS.get(sym,2)
        params.update({"type":"LIMIT","timeInForce":"GTC","price":f"{price:.{pp}f}"})
    else: params["type"] = "MARKET"

    for attempt in range(3):
        resp = _bpost("/fapi/v1/order", params)
        if resp and "orderId" in resp:
            fill = float(resp.get("avgPrice") or price or 0)
            log.info("✅ %s %s %s fill=%.4f", sym, side, qty, fill)
            return True, fill
        # Timeout/hiba esetén, mielőtt újraküldenénk, ellenőrizzük, hogy
        # esetleg mégis végrehajtódott-e a kérés az exchange oldalán.
        existing = _query_order_by_client_id(sym, client_id)
        if existing and existing.get("status") in ("FILLED", "PARTIALLY_FILLED"):
            fill = float(existing.get("avgPrice") or price or 0)
            log.warning("♻️ Retry közben derült ki, hogy már végrehajtódott: %s fill=%.4f", sym, fill)
            return True, fill
        time.sleep(2**attempt)

    # Binance 3x sikertelen — ha ez egy EXIT (zárás), próbáljuk meg a
    # másodlagos Bybit-útvonalon, hogy ne maradjon fedezetlen pozíció.
    if purpose == "EXIT":
        ok, _ = _bybit_emergency_close(sym, side, qty)
        if ok:
            return True, float(price or 0)
        tg_send(f"🚨 KRITIKUS: {sym} zárása Binance-on ÉS Bybit vész-úton is sikertelen! Manuális beavatkozás szükséges!")

    return False, 0.0

def execute_maker_chase(sym, side, qty, max_slip_pct=0.0005, chase_seconds=6):
    """
    Limit Chasing (Post-Only): próbál MAKER díjjal belépni a legjobb ajánlati szinten.
    Ha nem sikerül chase_seconds alatt, visszavonja és MARKET áron beüti a maradékot.
    """
    if not LIVE_TRADING:
        # Paper módban szimuláljuk az azonnali teljesülést
        return execute_order(sym, side, qty, order_type="MARKET", purpose="ENTRY")
        
    book = _bget("/fapi/v1/ticker/bookTicker", {"symbol": sym}, fb=False)
    if not book:
        return execute_order(sym, side, qty, order_type="MARKET", purpose="ENTRY")
    
    target_p = float(book["bidPrice"]) if side == "BUY" else float(book["askPrice"])
    tick = TICK_SIZES.get(sym, 0)
    if tick > 0: target_p = round(round(target_p / tick) * tick, PRICE_PRECISIONS.get(sym, 2))
    pp = PRICE_PRECISIONS.get(sym, 2)
    
    client_id = _make_client_order_id(sym, side, "CHASE_ENTRY", bucket_seconds=15)
    params = {
        "symbol": sym, "side": side, "quantity": qty,
        "type": "LIMIT", "timeInForce": "GTX",
        "price": f"{target_p:.{pp}f}",
        "newClientOrderId": client_id
    }
    
    resp = _bpost("/fapi/v1/order", params)
    if not resp or "orderId" not in resp:
        log.warning("Maker order elutasítva (%s), fallback MARKET.", resp.get("msg") if resp else "")
        return execute_order(sym, side, qty, order_type="MARKET", purpose="ENTRY")
        
    order_id = resp["orderId"]
    time.sleep(chase_seconds)
    
    chk = _bget("/fapi/v1/order", {"symbol": sym, "origClientOrderId": client_id}, signed=True, fb=False)
    if chk and chk.get("status") == "FILLED":
        fill = float(chk.get("avgPrice") or target_p)
        log.info("✅ MAKER FILL: %s %s %s @ %.4f", sym, side, qty, fill)
        return True, fill
        
    # Törlés
    _bdelete("/fapi/v1/order", {"symbol": sym, "origClientOrderId": client_id})
    time.sleep(0.5)
    
    chk = _bget("/fapi/v1/order", {"symbol": sym, "origClientOrderId": client_id}, signed=True, fb=False)
    filled_qty = float(chk.get("executedQty", 0)) if chk else 0.0
    rem_qty = qty - filled_qty
    
    if rem_qty > 0 and rem_qty >= MIN_QTY.get(sym, 0.0):
        log.info("Maker részleges/nincs teljesülés, maradék (%.3f) MARKET-be tolva.", rem_qty)
        ok, m_fill = execute_order(sym, side, rem_qty, order_type="MARKET", purpose="ENTRY_REMAINDER")
        if filled_qty > 0 and ok:
            avg_p = (filled_qty * target_p + rem_qty * m_fill) / qty
            return True, avg_p
        elif ok:
            return True, m_fill
        elif filled_qty > 0:
            return True, target_p
    elif filled_qty > 0:
        return True, target_p
        
    return False, 0.0


# ══════════════════════════════════════════════════════════════════
#  🔌 WEBSOCKET MANAGER (Zero Polling)
# ══════════════════════════════════════════════════════════════════
WS_DATA_LOCK = threading.Lock()
LIVE_PRICES = {}       # {"BTCUSDT": 65420.5, ...}
CLOSED_CANDLES_QUEUE = []  # Események: (symbol, interval, close_price)

async def binance_kline_stream(symbols, intervals=["1h", "4h"]):
    streams = [f"{s.lower()}@kline_{iv}" for s in symbols for iv in intervals]
    stream_path = "/".join(streams)
    url = f"wss://fstream.binance.com/stream?streams={stream_path}" if LIVE_TRADING else f"wss://stream.binancefuture.com/stream?streams={stream_path}"
    
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                log.info("🔌 WebSocket kapcsolat kiépítve: Klines")
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if "data" in data and "k" in data["data"]:
                        k = data["data"]["k"]
                        sym = k["s"]
                        is_closed = k["x"]
                        close_price = float(k["c"])
                        interval = k["i"]
                        
                        if is_closed:
                            with WS_DATA_LOCK:
                                CLOSED_CANDLES_QUEUE.append((sym, interval, close_price))
                            log.info("🕯️ Gyertya lezárult: %s %s @ %.4f", sym, interval, close_price)
        except Exception as e:
            log.error("WebSocket Klines hiba: %s. Újracsatlakozás 5s múlva...", e)
            await asyncio.sleep(5)

async def binance_bookticker_stream(symbols):
    streams = [f"{s.lower()}@bookTicker" for s in symbols]
    stream_path = "/".join(streams)
    url = f"wss://fstream.binance.com/stream?streams={stream_path}" if LIVE_TRADING else f"wss://stream.binancefuture.com/stream?streams={stream_path}"
    
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                log.info("🔌 WebSocket kapcsolat kiépítve: BookTicker")
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if "data" in data:
                        sym = data["data"]["s"]
                        bid = float(data["data"]["b"])
                        ask = float(data["data"]["a"])
                        mid = (bid + ask) / 2.0
                        with WS_DATA_LOCK:
                            LIVE_PRICES[sym] = mid
        except Exception as e:
            log.error("WebSocket BookTicker hiba: %s. Újracsatlakozás 5s múlva...", e)
            await asyncio.sleep(5)

def start_websocket_manager(symbols):
    async def run_streams():
        await asyncio.gather(
            binance_kline_stream(symbols),
            binance_bookticker_stream(symbols)
        )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_streams())

# ══════════════════════════════════════════════════════════════════
#  🚀  FŐ KERESKEDÉSI LOGIKA
# ══════════════════════════════════════════════════════════════════
def evaluate_entry(coin, price=None):
    df_4h = fetch_klines(coin, "4h", limit=210)
    df_1d = fetch_klines(coin, "1d", limit=210)
    df_1h = fetch_klines(coin, "1h", limit=60)

    if df_4h.empty or len(df_4h) < 50: return

    price    = price or float(df_4h["close"].iloc[-1])
    ema      = ind_ema(df_4h["close"], EMA_TREND_LEN)
    atr      = ind_atr(df_4h, ATR_LEN)
    adx      = ind_adx(df_4h, ADX_LEN)
    rsi      = ind_rsi(df_4h["close"], RSI_LEN)
    dh, dl   = ind_donchian(df_4h, DONCHIAN_LEN)
    _, bbu, bbl = ind_bollinger(df_4h["close"], BB_LEN, BB_STD)
    em       = ind_eff_mult(df_4h, atr)
    adx_threshold = get_adx_threshold()

    dp, dtrend = price, "UNKNOWN"
    if not df_1d.empty:
        dp     = float(df_1d["close"].iloc[-1])
        dema   = ind_ema(df_1d["close"], EMA_DAILY_LEN)
        dtrend = "BULL" if dp > dema else "BEAR"

    ob  = fetch_ob_imbalance(coin)
    oi  = fetch_oi_change(coin)
    fr  = fetch_funding(coin)
    
    with STATE_LOCK:
        pos = state["active_pos"].get(coin)

    vol_ok = state.get("btc_atr_pct", 50) >= VOL_FILTER_PERCENTILE

    if pos is None:
        if not can_open_new_position():
            dd_pct = (state.get("peak_capital",state["total_capital"])-state["total_capital"]) / max(state.get("peak_capital",1),1) * 100
            reason = "DAILY_LOSS_LIMIT" if daily_loss_breached() else f"DD_CRITICAL({dd_pct:.1f}%)"
            log_dec(coin,price,dp,ema,atr,adx,rsi,dh,dl,ob,oi,dtrend,f"FILTERED_{reason}")
            return

        if is_cooldown():
            log_dec(coin,price,dp,ema,atr,adx,rsi,dh,dl,ob,oi,dtrend,"FILTERED_COOLDOWN")
            return

        is_long  = (price>ema and price>dh and dtrend=="BULL")
        is_short = (price<ema and price<dl  and dtrend=="BEAR")
        strat    = "TREND"
        flt      = None

        if is_long or is_short:
            sc = "LONG" if is_long else "SHORT"
            if   adx < adx_threshold:                          flt = f"ADX_LOW({adx:.1f})"
            elif datetime.now().hour in LOW_LIQUIDITY_HOURS:   flt = "LOW_LIQ_HOUR"
            elif correlated_exposure_count(coin, sc) >= MAX_CORR_POSITIONS: flt = f"CORR_LIMIT({correlated_exposure_count(coin, sc)})"
            elif not vol_ok:                                   flt = "LOW_VOLATILITY"
            elif sc=="LONG"  and ob < 1/OB_IMBALANCE_MIN:     flt = f"OB_BEARISH({ob:.2f})"
            elif sc=="SHORT" and ob > OB_IMBALANCE_MIN:        flt = f"OB_BULLISH({ob:.2f})"
            elif oi < -OI_CHANGE_THR:                          flt = f"OI_DECLINING({oi:.3f})"
            elif sc=="LONG"  and fr > FUNDING_EXTREME_THR:     flt = f"FUNDING_HIGH({fr:.4f})"
            elif sc=="SHORT" and fr < -FUNDING_EXTREME_THR:    flt = f"FUNDING_LOW({fr:.4f})"
            elif get_coin_regime(coin) in ("HIGH_VOL_CHOP","LOW_VOL_CHOP"): flt = f"REGIME_{get_coin_regime(coin)}"
            else:
                div = detect_rsi_divergence(df_4h)
                if sc=="LONG"  and div=="BEARISH_DIV": flt = "RSI_BEARISH_DIV"
                elif sc=="SHORT" and div=="BULLISH_DIV": flt = "RSI_BULLISH_DIV"
                elif not df_1h.empty:
                    e1h = ind_ema(df_1h["close"], EMA_1H_LEN)
                    p1h = float(df_1h["close"].iloc[-1])
                    if sc=="LONG"  and p1h <= e1h: flt = "1H_NOT_CONFIRMED"
                    elif sc=="SHORT" and p1h >= e1h: flt = "1H_NOT_CONFIRMED"
            if flt:
                log_dec(coin,price,dp,ema,atr,adx,rsi,dh,dl,ob,oi,dtrend,f"FILTERED_{flt}")
                is_long = is_short = False

        if not is_long and not is_short and adx < adx_threshold and vol_ok:
            if rsi<=RSI_OVERSOLD  and price<=bbl and count_dir("LONG")<MAX_CORR_POSITIONS:
                is_long=True;  strat="MEAN_REV"
            elif rsi>=RSI_OVERBOUGHT and price>=bbu and count_dir("SHORT")<MAX_CORR_POSITIONS:
                is_short=True; strat="MEAN_REV"

        if is_long or is_short:
            side  = "LONG" if is_long else "SHORT"
            soff  = atr*STOP_HUNT_OFFSET_ATR if strat=="TREND" else 0
            sdist = atr*em+soff
            cstop = price-sdist if is_long else price+sdist
            lq    = liq_price(price, side)
            if side=="LONG"  and cstop < lq*(1+LIQUIDATION_BUFFER): cstop = lq*(1+LIQUIDATION_BUFFER)
            if side=="SHORT" and cstop > lq*(1-LIQUIDATION_BUFFER): cstop = lq*(1-LIQUIDATION_BUFFER)

            tgt = price+atr*PROFIT_TARGET_ATR if is_long else price-atr*PROFIT_TARGET_ATR

            with STATE_LOCK:
                allowed = state["total_capital"] * state["coin_weights"].get(coin, MIN_WEIGHT)
            er      = kelly_frac(coin) * risk_mult()
            sdf     = abs(price-cstop)
            stake   = min((allowed*er)/(sdf/price), allowed) if sdf>0 else 0
            qty     = get_qty(coin, stake, price)
            if qty <= 0: return

            ok, fill = execute_maker_chase(coin, "BUY" if is_long else "SELL", qty)
            if not ok: return

            slip = (fill-price)/price*100 if fill and price else 0.0
            with STATE_LOCK:
                state["active_pos"][coin]     = side
                state["entry_prices"][coin]   = fill or price
                state["stop_losses"][coin]    = cstop
                state["target_prices"][coin]  = tgt
                state["stakes"][coin]         = stake
                state["strategy_used"][coin]  = strat
                state["break_even"][coin]     = False
                state["partial_taken"][coin]  = False
                fee = apply_fee(stake)
                save_state()
                
            log_dec(coin,price,dp,ema,atr,adx,rsi,dh,dl,ob,oi,dtrend,f"OPEN_{side}_{strat}")
            tg_send(f"🔥 NYITÁS: {side} {coin} [{strat}]\n"
                    f"Ár: {price:.4f} | Stop: {cstop:.4f} | Target: {tgt:.4f}\n"
                    f"Kock.: {er*100:.1f}% | Díj: ${fee:.2f}\n"
                    f"OB: {ob:.2f} | OI Δ: {oi*100:.2f}% | Vol%: {state.get('btc_atr_pct',50):.0f}")

def manage_open_positions_realtime(current_prices):
    with STATE_LOCK:
        active_coins = [c for c in state["active_symbols"] if state["active_pos"].get(c) is not None]
        
    for coin in active_coins:
        price = current_prices.get(coin)
        if not price: continue
        
        with STATE_LOCK:
            pos = state["active_pos"].get(coin)
            if not pos: continue
            
            entry  = state["entry_prices"].get(coin,0)
            stop   = state["stop_losses"].get(coin,0)
            tgt    = state["target_prices"].get(coin,0)
            stake  = state["stakes"].get(coin,0)
            strat  = state["strategy_used"].get(coin,"")
            raw    = (price-entry)/entry if pos=="LONG" else (entry-price)/entry

            # Break-even
            if raw>=0.01 and not state["break_even"].get(coin):
                be = entry*1.001 if pos=="LONG" else entry*0.999
                if (pos=="LONG" and be>stop) or (pos=="SHORT" and be<stop):
                    state["stop_losses"][coin]=be; stop=be
                state["break_even"][coin]=True
                tg_send(f"🛡️ {coin} BREAK-EVEN! Stop={be:.4f}"); save_state()

            # Részleges profit
            if raw>=0.02 and not state["partial_taken"].get(coin):
                half = stake/2.0; hqty = get_qty(coin, half, price)
                if hqty > 0:
                    ok, fill = execute_order(coin,"SELL" if pos=="LONG" else "BUY",hqty,order_type="MARKET",price=price,purpose="EXIT")
                    if ok:
                        profit_h = (half*LEVERAGE)*raw
                        state["total_capital"] += profit_h
                        state["stakes"][coin]   = half; stake=half
                        state["partial_taken"][coin] = True
                        fee = apply_fee(half); save_state()
                        tg_send(f"💰 {coin} RÉSZLEGES: +${profit_h:.2f} | Díj: ${fee:.2f}")

            # Trailing stop + profit target
            cur_stop   = state["stop_losses"].get(coin,0)
            exit_trade = False; exit_price=price; pnl=0.0; exit_type=""

            now_ts = time.time()
            if "_atr_cache" not in state: state["_atr_cache"] = {}
            if coin not in state["_atr_cache"] or now_ts - state["_atr_cache"][coin].get("ts", 0) > 300:
                df_4h = fetch_klines(coin, "4h", limit=30)
                if df_4h.empty: continue
                c_atr = ind_atr(df_4h, ATR_LEN)
                c_em  = ind_eff_mult(df_4h, c_atr)
                state["_atr_cache"][coin] = {"ts": now_ts, "atr": c_atr, "em": c_em}
            
            atr = state["_atr_cache"][coin]["atr"]
            em  = state["_atr_cache"][coin]["em"]

            if pos=="LONG":
                ns = price-atr*em
                if ns>cur_stop: state["stop_losses"][coin]=ns; cur_stop=ns
                if strat=="MEAN_REV" and tgt>0 and price>=tgt:
                    pnl=(tgt-entry)/entry; exit_price=tgt; exit_trade=True; exit_type="PROFIT_TARGET"
                elif price<=cur_stop:
                    pnl=(cur_stop-entry)/entry; exit_trade=True; exit_type="STOP"
            elif pos=="SHORT":
                ns = price+atr*em
                if ns<cur_stop: state["stop_losses"][coin]=ns; cur_stop=ns
                if strat=="MEAN_REV" and tgt>0 and price<=tgt:
                    pnl=(entry-tgt)/entry; exit_price=tgt; exit_trade=True; exit_type="PROFIT_TARGET"
                elif price>=cur_stop:
                    pnl=(entry-cur_stop)/entry; exit_trade=True; exit_type="STOP"

        if exit_trade:
            use_price = price
            qty = get_qty(coin, stake, price)
            ok, fill = execute_order(coin,"SELL" if pos=="LONG" else "BUY",
                                     qty, order_type="MARKET",
                                     price=use_price, purpose="EXIT")
            if ok:
                actual_pnl = ((fill-entry)/entry if pos=="LONG" else (entry-fill)/entry) if fill else pnl
                with STATE_LOCK:
                    profit = (stake*LEVERAGE)*actual_pnl
                    roi    = actual_pnl*LEVERAGE*100
                    slip   = (fill-price)/price*100 if fill and price else 0.0

                    state["total_capital"] += profit
                    fee = apply_fee(stake)
                    update_score(coin, actual_pnl)
                    register_result(actual_pnl)

                    log_trade_db(coin,pos,strat,exit_type,entry,fill or price,
                                 stake,profit,roi,fee,slip)
                    # We skip log_dec here for simplicity or recreate dp, ema...
                    
                    state["active_pos"][coin]    = None
                    state["break_even"][coin]    = False
                    state["partial_taken"][coin] = False
                    save_state()

                em_j = "✅" if profit>0 else "❌"
                tg_send(f"{em_j} ZÁRÁS: {pos} {coin} [{exit_type}]\n"
                        f"Ár: {fill or price:.4f} | ROI: {roi:.2f}% (${profit:.2f})\n"
                        f"Slip: {slip:.3f}% | Díj: ${fee:.2f}\n"
                        f"Tőke: ${state['total_capital']:.2f}")

# ══════════════════════════════════════════════════════════════════
#  🔧  ÖNDIAGNÓZIS
# ══════════════════════════════════════════════════════════════════
def self_test():
    log.info("🔧 Öndiagnózis...")
    ok = True
    r = _bget("/fapi/v1/ping")
    if r is not None: log.info("✅ Binance API: OK")
    else:
        if HAS_BYBIT: log.warning("⚠️ Binance nem elérhető, Bybit fallback aktív")
        else: log.error("❌ Binance nem elérhető!"); ok=False
    df = fetch_klines("BTCUSDT","4h",limit=10)
    if not df.empty: log.info("✅ Kline fetch: OK (BTC: %.2f)", float(df["close"].iloc[-1]))
    else: log.error("❌ Kline fetch HIBA!"); ok=False
    tg_send("🔧 PRO-BOT V5 öndiagnózis — kapcsolat OK")
    return ok

# ══════════════════════════════════════════════════════════════════
#  🚀  FŐPROGRAM
# ══════════════════════════════════════════════════════════════════


def reconcile_positions_with_exchange():
    """Összehangolja a belső állapotot a Binance valós nyitott pozícióival."""
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

def main():
    log.info("=" * 60)
    log.info("  PRO-BOT V5 — Fejlesztett verzió | Mód: %s", EXECUTION_MODE)
    log.info("=" * 60)

    db_init(); load_state()

    cfg = {"ts":time.time(),"mode":EXECUTION_MODE,"base_symbols":BASE_SYMBOLS,
           "max_weight":MAX_WEIGHT,"profit_target_atr":PROFIT_TARGET_ATR,
           "vol_filter_pct":VOL_FILTER_PERCENTILE,"leverage":LEVERAGE,
           "kelly_fraction":KELLY_FRACTION,"has_bybit":HAS_BYBIT,
           "max_drawdown":MAX_DRAWDOWN,"daily_loss_limit_pct":DAILY_LOSS_LIMIT_PCT,
           "drawdown_review_pct":DRAWDOWN_REVIEW_PCT,
           "correlation_threshold":CORRELATION_THRESHOLD}
    db_exec("INSERT INTO config_snapshots (ts,config_json) VALUES (?,?)",
            (time.time(), json.dumps(cfg)))

    if not self_test():
        log.error("Öndiagnózis sikertelen."); sys.exit(1)

    tg_send(f"🚀 PRO-BOT V5 INDUL!\n"
            f"💰 Tőke: ${state['total_capital']:.2f}\n"
            f"🔄 Mód: {EXECUTION_MODE}\n"
            f"🎯 Profit target: {PROFIT_TARGET_ATR}×ATR\n"
            f"🪙 Coinok: {', '.join(BASE_SYMBOLS)}\n"
            f"🛡️ Napi loss-limit: {DAILY_LOSS_LIMIT_PCT*100:.0f}% | Review: {DRAWDOWN_REVIEW_PCT*100:.0f}% | Max DD: {MAX_DRAWDOWN*100:.0f}%\n"
            f"📈 Korrelációs küszöb: {CORRELATION_THRESHOLD}\n"
            f"🔀 Bybit fallback: {'✅' if HAS_BYBIT else '❌'}")

    schedule.every(REGIME_CLUSTER_HOURS).hours.do(
        lambda: threading.Thread(target=train_regime_models_all, daemon=True).start())
    schedule.every(WALKFORWARD_DAYS).days.do(
        lambda: threading.Thread(target=walkforward_optimize, daemon=True).start())
    schedule.every(7).days.do(
        lambda: threading.Thread(target=update_active_symbols, daemon=True).start())
    schedule.every(3).minutes.do(
        lambda: threading.Thread(target=reconcile_positions_with_exchange, daemon=True).start())
    schedule.every(4).hours.do(
        lambda: threading.Thread(target=update_vol_filter, daemon=True).start())
    schedule.every(4).hours.do(
        lambda: threading.Thread(target=update_correlation_matrix, daemon=True).start())
    schedule.every().day.at("20:00").do(lambda: tg_send(
        f"🌙 NAPI JELENTÉS\n"
        f"📊 Tőke: ${state['total_capital']:.2f}\n"
        f"📉 Csúcs: ${state['peak_capital']:.2f}\n"
        f"🌊 Rezsim: {state.get('current_regime','?')}\n"
        f"🪙 Aktív coinok: {', '.join(state['active_symbols'])}"
    ))
    schedule.every().day.at("00:00").do(reset_daily_loss_window)

    threading.Thread(target=train_regime_models_all, daemon=True).start()
    threading.Thread(target=update_vol_filter, daemon=True).start()
    threading.Thread(target=update_correlation_matrix, daemon=True).start()

    log.info("🔌 WebSocket szál indítása...")
    ws_thread = threading.Thread(target=start_websocket_manager, args=(state["active_symbols"],), daemon=True)
    ws_thread.start()
    
    log.info("✅ Főciklus indul...")
    last_equity_log = 0

    while True:
        try:
            with open("bot_heartbeat.ts", "w") as f:
                f.write(str(time.time()))
            
            schedule.run_pending()
            tg_check()

            with STATE_LOCK:
                if state["total_capital"] > state["peak_capital"]:
                    state["peak_capital"] = state["total_capital"]
                if state["total_capital"] <= state["peak_capital"]*(1-MAX_DRAWDOWN):
                    tg_send(f"🚨 VÉSZFÉK! Max drawdown ({MAX_DRAWDOWN*100:.0f}%) elérve. Bot leállt.")
                    log.critical("VÉSZFÉK!")
                    break

                if daily_loss_breached() and not state.get("_daily_limit_alerted", False):
                    tg_send(f"🛑 NAPI LOSS-LIMIT ({DAILY_LOSS_LIMIT_PCT*100:.0f}%) elérve — "
                            f"ma nincs több új nyitás, a nyitott pozíciók stopjai futnak tovább.")
                    state["_daily_limit_alerted"] = True
                    save_state()

                _peak = state.get("peak_capital", state["total_capital"])
                _dd_now = (_peak - state["total_capital"]) / _peak if _peak > 0 else 0.0
                if _dd_now >= DRAWDOWN_REVIEW_PCT and not state.get("_review_pending", False):
                    tg_send(f"⚠️ REVIEW SZÜKSÉGES: a tőke {_dd_now*100:.1f}%-ot esett a csúcstól "
                             f"({DRAWDOWN_REVIEW_PCT*100:.0f}%-os figyelmeztető szint).\n"
                             f"A bot a nyitott pozíciókat tovább kezeli, de ÚJ pozíciót nem nyit, "
                             f"amíg a stratégiát át nem nézed és /confirm-mal meg nem erősíted.")
                    state["_review_pending"] = True
                    save_state()
                elif _dd_now < DRAWDOWN_REVIEW_PCT * 0.5 and state.get("_review_pending", False):
                    state["_review_pending"] = False
                    save_state()
                    tg_send("✅ A drawdown jelentősen csökkent — review-blokk automatikusan feloldva.")

                now = time.time()
                if now - state.get("last_rebalance_ts",0) > REBALANCE_HOURS*3600:
                    rebalance()
                    state["last_rebalance_ts"] = now
                    save_state()
                    
            # 1. Valós idejű pozíciómenedzsment (Tick based)
            current_prices = {}
            with WS_DATA_LOCK:
                current_prices = dict(LIVE_PRICES)
            if current_prices:
                try:
                    manage_open_positions_realtime(current_prices)
                except Exception as e:
                    log.error("Hiba pozíció menedzsment közben: %s", e)

            # 2. Belépési logika (Gyertya lezárásra reagálva)
            candles_to_process = []
            with WS_DATA_LOCK:
                candles_to_process = list(CLOSED_CANDLES_QUEUE)
                CLOSED_CANDLES_QUEUE.clear()
                
            for sym, interval, c_price in candles_to_process:
                if interval == "4h":
                    log.info("🎯 4h Gyertya zárult %s esetén, belépés vizsgálata...", sym)
                    try:
                        evaluate_entry(sym, c_price)
                    except Exception as e:
                        log.error("Hiba belépés vizsgálatakor (%s): %s", sym, e)

            if time.time() - last_equity_log > 3600:
                log_equity()
                last_equity_log = time.time()
                
            time.sleep(1) # Gyors iteráció, mivel WS eseményekre reagál!
            
        except KeyboardInterrupt:
            tg_send("⏹️ Bot leállítva."); break
        except Exception as e:
            log.error("Főciklus hiba: %s", e)
            tg_send(f"⚠️ Hiba: {e}"); time.sleep(10)

    save_state()
    log.info("Bot leállt.")


if __name__ == '__main__':
    main()
