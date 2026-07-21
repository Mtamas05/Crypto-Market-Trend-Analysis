#!/usr/bin/env python3
"""
pro_bot_v5_pi.py — Pro Bot V5 Raspberry Pi (Végleges)
=======================================================
Új fejlesztések ebben a verzióban:
  - Konfig szinkronizálva a backtest optimalizált értékeivel
  - Coinok: BTCUSDT, SOLUSDT, FETUSDT, AVAXUSDT (ETH+BNB kicserélve)
  - ATR_MULT 1.8 (tágabb stop, kevesebb korai kirázás)
  - ADX küszöb 20.0 (több valós jelzés)
  - Portfolió-szintű drawdown monitor (15% → felezett méret, 25% → stop)
  - RSI divergencia szűrő (bearish/bullish divergencia detektálás)
  - MAX_WEIGHT 40%-ra emelve
  - Profit target csak MEAN_REV stratégiánál
  - Vol szűrő lazítva 10%-ra

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
MAX_DRAWDOWN    = 0.20

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
DB_PATH        = "bot_v5.db"

PRECISIONS       = {"BTCUSDT":3,"ETHUSDT":3,"SOLUSDT":2,"BNBUSDT":2,"FETUSDT":1}
PRICE_PRECISIONS = {"BTCUSDT":1,"ETHUSDT":2,"SOLUSDT":3,"BNBUSDT":2,"FETUSDT":4}
MIN_QTY          = {"BTCUSDT":0.001,"ETHUSDT":0.001,"SOLUSDT":0.01,"BNBUSDT":0.01,"FETUSDT":1}

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
STATE_FILE = "bot_state.json"
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
    "btc_atr_pct": 50.0,  # BTC ATR percentilis a vol szűrőhöz
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
                tg_send(f"📊 Tőke: ${state['total_capital']:.2f}\n"
                        f"📈 Nyitott: {active or 'Nincs'}\n"
                        f"🌊 Rezsim: {state['current_regime']}\n"
                        f"📉 Csúcs: ${peak:.2f} | DD: {dd:.1f}% {dd_status}\n"
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
            elif txt == "/closeall":
                for c in state["active_symbols"]:
                    if state["active_pos"].get(c) == "LONG":   state["stop_losses"][c] = 1e9
                    elif state["active_pos"].get(c) == "SHORT": state["stop_losses"][c] = -1.0
                tg_send("🚨 VÉSZKAPCSOLÓ aktiválva!"); save_state()
            elif txt == "/help":
                tg_send("🤖 PARANCSOK:\n/status /coins /closeall /help")
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

        # Top N kiválasztása (BASE_SYMBOLS mindig bent marad)
        top = sorted(scores, key=scores.get, reverse=True)[:TOP_N_COINS*2]
        # Biztosítjuk hogy BTC és ETH mindig bent van
        selected = list({s for s in top[:TOP_N_COINS]} |
                       {"BTCUSDT", "ETHUSDT"})[:TOP_N_COINS+2]
        selected = selected[:TOP_N_COINS]

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
            tg_send(f"🔄 Coin rotáció!\nRégi: {', '.join(old)}\nÚj: {', '.join(selected)}")
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

def ind_eff_mult(df, atr, base=ATR_MULT, lb=30):
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

def train_regime_model():
    global _regime_model, _regime_scaler
    log.info("🌊 Rezsim-klaszterezés indul...")
    try:
        df = fetch_klines("BTCUSDT", "4h", limit=360)
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
        with _regime_lock:
            X = _regime_scaler.fit_transform(features)
            _regime_model = KMeans(n_clusters=4, n_init=10, random_state=42)
            _regime_model.fit(X)
        cur = features.iloc[-1:]
        with _regime_lock:
            cl = int(_regime_model.predict(_regime_scaler.transform(cur))[0])
            centroids = _regime_model.cluster_centers_
        labels = {}
        for i, c in enumerate(centroids):
            if c[0] > 0.5 and c[3] > 0.3:   labels[i] = "BULL_TREND"
            elif c[0] > 0.5 and c[3] < -0.3: labels[i] = "BEAR_TREND"
            elif c[1] > 0.5:                  labels[i] = "HIGH_VOL_CHOP"
            else:                             labels[i] = "LOW_VOL_CHOP"
        regime = labels.get(cl, f"CLUSTER_{cl}")
        state["current_regime"] = regime
        save_state()
        log.info("🌊 Rezsim: %s", regime)
        tg_send(f"🌊 Piaci rezsim: {regime}")
    except Exception as e: log.error("Rezsim hiba: %s", e)

# ══════════════════════════════════════════════════════════════════
#  🔬  WALK-FORWARD OPTIMALIZÁLÁS
# ══════════════════════════════════════════════════════════════════
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
        trades = db_fetch("SELECT profit_usd FROM trades WHERE ts>?", (cutoff,))
        wr = sum(1 for (p,) in trades if p>0)/len(trades) if trades else 0
        params = {"ts":time.time(),"feature_importances":imp,"win_rate":wr,"n":len(rows)}
        db_exec("INSERT INTO optimized_params (ts, params_json) VALUES (?,?)",
                (time.time(), json.dumps(params)))
        state["dynamic_params"] = params; save_state()
        tg_send(f"🔬 Walk-forward kész\n{len(rows)} trade | WR: {wr*100:.1f}%\n"
                f"Legfontosabb: {max(imp, key=imp.get)}")
    except Exception as e: log.error("Walk-forward hiba: %s", e)

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
    Portfolió-szintű drawdown monitor — ez a legfontosabb tőkevédelmi réteg.
    Ha a tőke visszaesik a csúcstól:
      - 15% felett: felezi az összes új pozíció méretét
      - 25% felett: teljesen leállítja az új nyitásokat (0.0 visszatér)
    Ez védi a stratégiát a rossz periódusokban, és megakadályozza hogy
    egyetlen rossz hónap eltüntesse a korábban felépített profitot.
    """
    peak = state.get("peak_capital", state["total_capital"])
    if peak <= 0:
        return 1.0
    dd = (peak - state["total_capital"]) / peak

    if dd >= 0.25:
        # 25%+ drawdown: nincs új nyitás
        return 0.0
    elif dd >= 0.15:
        # 15-25% drawdown: felezett pozícióméret
        return 0.5
    elif dd >= 0.10:
        # 10-15% drawdown: 75%-os pozícióméret (óvatosság jelzése)
        return 0.75
    return 1.0

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
    True ha szabad új pozíciót nyitni (portfolió drawdown nem kritikus).
    25%+ drawdown esetén teljesen letiltja az új nyitásokat.
    """
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
    st = state["coin_stats"].setdefault(coin,{"trades":0,"wins":0,"avg_R":0.0,"score":1.0})
    st["trades"]+=1
    if pnl>0: st["wins"]+=1
    st["avg_R"] = st["avg_R"]*(1-ALPHA_RECENCY)+pnl*ALPHA_RECENCY
    wr = st["wins"]/st["trades"]
    st["score"] = max(0.05, wr*(1+st["avg_R"]*10))

def rebalance():
    syms  = state["active_symbols"]
    total = sum(state["coin_stats"].get(s,{}).get("score",1.0) for s in syms)
    for s in syms:
        sc = state["coin_stats"].get(s,{}).get("score",1.0)
        state["coin_weights"][s] = max(MIN_WEIGHT, min(MAX_WEIGHT, sc/total))
    wt = sum(state["coin_weights"].get(s,0) for s in syms)
    for s in syms: state["coin_weights"][s] /= wt

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
             state.get("current_regime","UNKNOWN"),dec))

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
#  📤  ORDER VÉGREHAJTÁS
# ══════════════════════════════════════════════════════════════════
def execute_order(sym, side, qty, order_type="LIMIT", price=None):
    if not LIVE_TRADING:
        log.info("📄 PAPER: %s %s %s @ %.4f", sym, side, qty, price or 0)
        return True, float(price or 0)
    params = {"symbol":sym,"side":side,"quantity":qty}
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
        time.sleep(2**attempt)
    return False, 0.0

# ══════════════════════════════════════════════════════════════════
#  🚀  FŐ KERESKEDÉSI LOGIKA
# ══════════════════════════════════════════════════════════════════
def process_coin(coin):
    df_4h = fetch_klines(coin, "4h", limit=210)
    df_1d = fetch_klines(coin, "1d", limit=210)
    df_1h = fetch_klines(coin, "1h", limit=60)

    if df_4h.empty or len(df_4h) < 50: return

    price    = float(df_4h["close"].iloc[-1])
    ema      = ind_ema(df_4h["close"], EMA_TREND_LEN)
    atr      = ind_atr(df_4h, ATR_LEN)
    adx      = ind_adx(df_4h, ADX_LEN)
    rsi      = ind_rsi(df_4h["close"], RSI_LEN)
    dh, dl   = ind_donchian(df_4h, DONCHIAN_LEN)
    _, bbu, bbl = ind_bollinger(df_4h["close"], BB_LEN, BB_STD)
    em       = ind_eff_mult(df_4h, atr)

    dp, dtrend = price, "UNKNOWN"
    if not df_1d.empty:
        dp     = float(df_1d["close"].iloc[-1])
        dema   = ind_ema(df_1d["close"], EMA_DAILY_LEN)
        dtrend = "BULL" if dp > dema else "BEAR"

    ob  = fetch_ob_imbalance(coin)
    oi  = fetch_oi_change(coin)
    fr  = fetch_funding(coin)
    pos = state["active_pos"].get(coin)

    # ── ÚJ: volatilitás szűrő ────────────────────────────────────
    vol_ok = state.get("btc_atr_pct", 50) >= VOL_FILTER_PERCENTILE

    # ════════════════════════════════════════════════════════════
    #  NYITÁS
    # ════════════════════════════════════════════════════════════
    if pos is None:
        # Portfolió drawdown monitor — ha kritikus szinten van a DD, nem nyitunk
        if not can_open_new_position():
            dd_pct = (state.get("peak_capital",state["total_capital"])-state["total_capital"]) / max(state.get("peak_capital",1),1) * 100
            log_dec(coin,price,dp,ema,atr,adx,rsi,dh,dl,ob,oi,dtrend,f"FILTERED_DD_CRITICAL({dd_pct:.1f}%)")
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
            if   adx < ADX_THRESHOLD:                          flt = f"ADX_LOW({adx:.1f})"
            elif datetime.now().hour in LOW_LIQUIDITY_HOURS:   flt = "LOW_LIQ_HOUR"
            elif count_dir(sc) >= MAX_CORR_POSITIONS:          flt = "CORR_LIMIT"
            elif not vol_ok:                                   flt = "LOW_VOLATILITY"
            elif sc=="LONG"  and ob < 1/OB_IMBALANCE_MIN:     flt = f"OB_BEARISH({ob:.2f})"
            elif sc=="SHORT" and ob > OB_IMBALANCE_MIN:        flt = f"OB_BULLISH({ob:.2f})"
            elif oi < -OI_CHANGE_THR:                          flt = f"OI_DECLINING({oi:.3f})"
            elif sc=="LONG"  and fr > FUNDING_EXTREME_THR:     flt = f"FUNDING_HIGH({fr:.4f})"
            elif sc=="SHORT" and fr < -FUNDING_EXTREME_THR:    flt = f"FUNDING_LOW({fr:.4f})"
            elif state.get("current_regime") in ("HIGH_VOL_CHOP","LOW_VOL_CHOP"): flt = f"REGIME_{state['current_regime']}"
            else:
                # RSI divergencia szűrő — trend irányával ellentétes divergencia esetén
                # nem nyitunk, mert a momentum már gyengül
                div = detect_rsi_divergence(df_4h)
                if sc=="LONG"  and div=="BEARISH_DIV": flt = "RSI_BEARISH_DIV"
                elif sc=="SHORT" and div=="BULLISH_DIV": flt = "RSI_BULLISH_DIV"
                # 1h megerősítő gyertya — mindig a szűrőlánc legvégén, külön fetch kell
                elif not df_1h.empty:
                    e1h = ind_ema(df_1h["close"], EMA_1H_LEN)
                    p1h = float(df_1h["close"].iloc[-1])
                    if sc=="LONG"  and p1h <= e1h: flt = "1H_NOT_CONFIRMED"
                    elif sc=="SHORT" and p1h >= e1h: flt = "1H_NOT_CONFIRMED"
            if flt:
                log_dec(coin,price,dp,ema,atr,adx,rsi,dh,dl,ob,oi,dtrend,f"FILTERED_{flt}")
                is_long = is_short = False

        # Mean-reversion (drawdown esetén is szabad, de csökkentett mérettel)
        if not is_long and not is_short and adx < ADX_THRESHOLD and vol_ok:
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

            # ÚJ: profit target
            tgt = price+atr*PROFIT_TARGET_ATR if is_long else price-atr*PROFIT_TARGET_ATR

            allowed = state["total_capital"] * state["coin_weights"].get(coin, MIN_WEIGHT)
            er      = kelly_frac(coin) * risk_mult()
            sdf     = abs(price-cstop)
            stake   = min((allowed*er)/(sdf/price), allowed) if sdf>0 else 0
            qty     = get_qty(coin, stake, price)
            if qty <= 0: return

            ok, fill = execute_order(coin, "BUY" if is_long else "SELL", qty, price=price)
            if not ok: return

            slip = (fill-price)/price*100 if fill and price else 0.0
            state["active_pos"][coin]     = side
            state["entry_prices"][coin]   = fill or price
            state["stop_losses"][coin]    = cstop
            state["target_prices"][coin]  = tgt
            state["stakes"][coin]         = stake
            state["strategy_used"][coin]  = strat
            state["break_even"][coin]     = False
            state["partial_taken"][coin]  = False

            fee = apply_fee(stake); save_state()
            log_dec(coin,price,dp,ema,atr,adx,rsi,dh,dl,ob,oi,dtrend,f"OPEN_{side}_{strat}")
            tg_send(f"🔥 NYITÁS: {side} {coin} [{strat}]\n"
                    f"Ár: {price:.4f} | Stop: {cstop:.4f} | Target: {tgt:.4f}\n"
                    f"Kock.: {er*100:.1f}% | Díj: ${fee:.2f}\n"
                    f"OB: {ob:.2f} | OI Δ: {oi*100:.2f}% | Vol%: {state.get('btc_atr_pct',50):.0f}")

    # ════════════════════════════════════════════════════════════
    #  MENEDZSMENT
    # ════════════════════════════════════════════════════════════
    else:
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
                ok, fill = execute_order(coin,"SELL" if pos=="LONG" else "BUY",hqty,price=price)
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

        if pos=="LONG":
            ns = price-atr*em
            if ns>cur_stop: state["stop_losses"][coin]=ns; cur_stop=ns
            # Profit target csak MEAN_REV-nél, TREND-nél a trailing stop fut
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
            use_price = None if exit_type=="STOP" and abs(cur_stop-price)/price>STOP_BUFFER_PCT else exit_price
            qty = get_qty(coin, stake, price)
            ok, fill = execute_order(coin,"SELL" if pos=="LONG" else "BUY",
                                     qty, order_type="MARKET" if use_price is None else "LIMIT",
                                     price=use_price)
            if ok:
                actual_pnl = ((fill-entry)/entry if pos=="LONG" else (entry-fill)/entry) if fill else pnl
                profit = (stake*LEVERAGE)*actual_pnl
                roi    = actual_pnl*LEVERAGE*100
                slip   = (fill-price)/price*100 if fill and price else 0.0

                state["total_capital"] += profit
                fee = apply_fee(stake)
                update_score(coin, actual_pnl)
                register_result(actual_pnl)

                log_trade_db(coin,pos,strat,exit_type,entry,fill or price,
                             stake,profit,roi,fee,slip)
                log_dec(coin,price,dp,ema,atr,adx,rsi,dh,dl,ob,oi,dtrend,f"CLOSE_{pos}_{exit_type}")

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
def main():
    log.info("=" * 60)
    log.info("  PRO-BOT V5 — Fejlesztett verzió | Mód: %s", EXECUTION_MODE)
    log.info("=" * 60)

    db_init(); load_state()

    # Config snapshot
    cfg = {"ts":time.time(),"mode":EXECUTION_MODE,"base_symbols":BASE_SYMBOLS,
           "max_weight":MAX_WEIGHT,"profit_target_atr":PROFIT_TARGET_ATR,
           "vol_filter_pct":VOL_FILTER_PERCENTILE,"leverage":LEVERAGE,
           "kelly_fraction":KELLY_FRACTION,"has_bybit":HAS_BYBIT}
    db_exec("INSERT INTO config_snapshots (ts,config_json) VALUES (?,?)",
            (time.time(), json.dumps(cfg)))

    if not self_test():
        log.error("Öndiagnózis sikertelen."); sys.exit(1)

    tg_send(f"🚀 PRO-BOT V5 INDUL!\n"
            f"💰 Tőke: ${state['total_capital']:.2f}\n"
            f"🔄 Mód: {EXECUTION_MODE}\n"
            f"🎯 Profit target: {PROFIT_TARGET_ATR}×ATR\n"
            f"🪙 Coinok: {', '.join(BASE_SYMBOLS)}\n"
            f"🔀 Bybit fallback: {'✅' if HAS_BYBIT else '❌'}")

    # Háttérfeladatok
    schedule.every(REGIME_CLUSTER_HOURS).hours.do(
        lambda: threading.Thread(target=train_regime_model, daemon=True).start())
    schedule.every(WALKFORWARD_DAYS).days.do(
        lambda: threading.Thread(target=walkforward_optimize, daemon=True).start())
    schedule.every(7).days.do(
        lambda: threading.Thread(target=update_active_symbols, daemon=True).start())
    schedule.every(4).hours.do(
        lambda: threading.Thread(target=update_vol_filter, daemon=True).start())
    schedule.every().day.at("20:00").do(lambda: tg_send(
        f"🌙 NAPI JELENTÉS\n"
        f"📊 Tőke: ${state['total_capital']:.2f}\n"
        f"📉 Csúcs: ${state['peak_capital']:.2f}\n"
        f"🌊 Rezsim: {state.get('current_regime','?')}\n"
        f"🪙 Aktív coinok: {', '.join(state['active_symbols'])}"
    ))

    # Azonnal futó első körök
    threading.Thread(target=train_regime_model, daemon=True).start()
    threading.Thread(target=update_vol_filter, daemon=True).start()

    log.info("✅ Főciklus indul...")
    wdt_interval = 60  # WDT szimulálás (Pi-n valódi signal küld majd)

    while True:
        try:
            schedule.run_pending()
            tg_check()

            # Drawdown killswitch
            if state["total_capital"] > state["peak_capital"]:
                state["peak_capital"] = state["total_capital"]
            if state["total_capital"] <= state["peak_capital"]*(1-MAX_DRAWDOWN):
                tg_send("🚨 VÉSZFÉK! Max drawdown elérve. Bot leállt.")
                log.critical("VÉSZFÉK!")
                break

            # Tőkesúly frissítés
            now = time.time()
            if now - state.get("last_rebalance_ts",0) > REBALANCE_HOURS*3600:
                rebalance()
                state["last_rebalance_ts"] = now
                save_state()

            # Coinok feldolgozása
            for coin in state["active_symbols"]:
                try: process_coin(coin)
                except Exception as e: log.error("process_coin hiba (%s): %s", coin, e)
                time.sleep(2)

            log_equity(); save_state()

            has_pos = any(state["active_pos"].get(c) for c in state["active_symbols"])
            wait = 60 if has_pos else 300
            log.info("💤 Következő ciklus: %ds | Tőke: $%.2f | Rezsim: %s",
                     wait, state["total_capital"], state.get("current_regime","?"))
            time.sleep(wait)

        except KeyboardInterrupt:
            tg_send("⏹️ Bot leállítva."); break
        except Exception as e:
            log.error("Főciklus hiba: %s", e)
            tg_send(f"⚠️ Hiba: {e}"); time.sleep(30)

    save_state()
    log.info("Bot leállt.")

if __name__ == "__main__":
    main()