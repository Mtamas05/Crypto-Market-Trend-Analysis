#!/usr/bin/env python3
"""
backtest.py — Pro Bot V5 Backtest Engine (Fejlesztett verzió)
=============================================================
Új funkciók:
  - FETUSDT eltávolítva
  - MAX_WEIGHT 35%-ra csökkentve
  - ATR-alapú profit target (3×ATR)
  - Volatilitás szűrő (alacsony vol = nincs belépés)
  - Dinamikus coin rotáció szimulálva (momentum alapon)
"""

import sqlite3
import numpy as np
import pandas as pd
from datetime import datetime

# ══════════════════════════════════════════════════════════════════
#  ⚙️  KONFIGURÁCIÓ
# ══════════════════════════════════════════════════════════════════
SYMBOLS         = ["BTCUSDT", "SOLUSDT", "FETUSDT", "AVAXUSDT"]
INITIAL_CAPITAL = 100.0
LEVERAGE        = 5
BASE_RISK_PCT   = 0.14
MAX_WEIGHT      = 0.40
MIN_WEIGHT      = 0.05
TAKER_FEE_PCT   = 0.0004

DONCHIAN_LEN    = 20
EMA_TREND_LEN   = 180
EMA_DAILY_LEN   = 200
ATR_LEN         = 14
ATR_MULT        = 1.8
PROFIT_TARGET_ATR = 3.0
ADX_LEN         = 14
RSI_LEN         = 14
BB_LEN          = 20
BB_STD          = 2.0

ADX_THRESHOLD   = 20.0
MAX_CORR_POS    = 2
STOP_HUNT_ATR   = 0.3
LIQ_BUFFER      = 0.10
STOP_BUFFER_PCT = 0.005

RSI_OVERSOLD    = 30
RSI_OVERBOUGHT  = 70

KELLY_FRACTION  = 0.5
KELLY_MIN_TRADES= 8
ALPHA_RECENCY   = 0.20
LOSS_STREAK_LIM = 3
LOSS_STREAK_RED = 0.5
PROFIT_LOCK_PCT = 0.05
PROFIT_LOCK_RED = 0.5
WEEKEND_RED     = 0.5

VOL_FILTER_PERCENTILE = 10
VOL_FILTER_LOOKBACK   = 90 * 6

MOMENTUM_LOOKBACK = 30 * 6
MOMENTUM_REBALANCE_BARS = 30 * 6  # havonta rebalance

DB_PATH = "history.db"

# ══════════════════════════════════════════════════════════════════
#  📂  ADATBETÖLTÉS
# ══════════════════════════════════════════════════════════════════
def load_candles(symbol, timeframe):
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT ts,open,high,low,close,volume FROM candles "
        "WHERE symbol=? AND timeframe=? ORDER BY ts",
        con, params=(symbol, timeframe)
    )
    con.close()
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    return df

# ══════════════════════════════════════════════════════════════════
#  📐  INDIKÁTOROK
# ══════════════════════════════════════════════════════════════════
def add_indicators(df):
    df = df.copy()
    df["ema_trend"] = df["close"].ewm(span=EMA_TREND_LEN, adjust=False).mean()

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(ATR_LEN).mean()

    up   = df["high"].diff()
    down = -df["low"].diff()
    pdm  = np.where((up > down) & (up > 0), up, 0.0)
    mdm  = np.where((down > up) & (down > 0), down, 0.0)
    atr_s = tr.ewm(span=ADX_LEN, adjust=False).mean()
    pdi   = 100 * pd.Series(pdm, index=df.index).ewm(span=ADX_LEN, adjust=False).mean() / atr_s
    mdi   = 100 * pd.Series(mdm, index=df.index).ewm(span=ADX_LEN, adjust=False).mean() / atr_s
    dx    = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    df["adx"] = dx.ewm(span=ADX_LEN, adjust=False).mean()

    df["don_high"] = df["high"].shift(1).rolling(DONCHIAN_LEN).max()
    df["don_low"]  = df["low"].shift(1).rolling(DONCHIAN_LEN).min()

    delta = df["close"].diff()
    gain  = delta.clip(lower=0).ewm(span=RSI_LEN, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=RSI_LEN, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    df["bb_mid"]   = df["close"].rolling(BB_LEN).mean()
    bb_std         = df["close"].rolling(BB_LEN).std()
    df["bb_upper"] = df["bb_mid"] + BB_STD * bb_std
    df["bb_lower"] = df["bb_mid"] - BB_STD * bb_std

    atr_avg = df["atr"].rolling(30).mean()
    ratio   = df["atr"] / atr_avg.replace(0, np.nan)
    df["eff_mult"] = ATR_MULT
    df.loc[ratio > 1.3, "eff_mult"] = max(1.0, ATR_MULT * 0.8)
    df.loc[ratio < 0.7, "eff_mult"] = min(2.5, ATR_MULT * 1.3)

    # ÚJ: ATR percentilis (volatilitás szűrőhöz)
    df["atr_pct"] = df["atr"].rolling(VOL_FILTER_LOOKBACK).rank(pct=True) * 100

    return df

def add_daily_trend(df_4h, df_1d):
    df_1d = df_1d.copy()
    df_1d["ema_daily"] = df_1d["close"].ewm(span=EMA_DAILY_LEN, adjust=False).mean()
    df_1d["daily_trend"] = np.where(df_1d["close"] > df_1d["ema_daily"], "BULL", "BEAR")
    daily_rs = df_1d["daily_trend"].resample("4h").ffill()
    df_4h = df_4h.copy()
    df_4h["daily_trend"] = daily_rs.reindex(df_4h.index, method="ffill").fillna("UNKNOWN")
    return df_4h

# ══════════════════════════════════════════════════════════════════
#  🎯  BACKTEST MOTOR
# ══════════════════════════════════════════════════════════════════
def run_backtest():
    print("=" * 65)
    print("  PRO-BOT V5 — Fejlesztett Backtest")
    print(f"  Coinok: {', '.join(SYMBOLS)}")
    print(f"  Max pozíció súly: {MAX_WEIGHT*100:.0f}% | Profit target: {PROFIT_TARGET_ATR}×ATR")
    print(f"  Vol szűrő: alsó {VOL_FILTER_PERCENTILE}%-ban nincs belépés")
    print("=" * 65)

    print("\n📂 Adatok betöltése...")
    data = {}
    for sym in SYMBOLS:
        df4 = load_candles(sym, "4h")
        df1 = load_candles(sym, "1d")
        if df4.empty or len(df4) < 200:
            print(f"  ⚠️  {sym}: nincs elég adat, kihagyva.")
            continue
        df4 = add_indicators(df4)
        if not df1.empty:
            df4 = add_daily_trend(df4, df1)
        else:
            df4["daily_trend"] = "UNKNOWN"
        data[sym] = df4.dropna(subset=["ema_trend","atr","adx","don_high","don_low"])
        print(f"  ✅ {sym}: {len(data[sym])} gyertya "
              f"({data[sym].index[0].date()} → {data[sym].index[-1].date()})")

    if not data:
        print("❌ Nincs adat!")
        return

    # BTC ATR percentilis a volatilitás szűrőhöz
    btc_atr_pct = data.get("BTCUSDT", pd.DataFrame()).get("atr_pct", pd.Series())

    common_idx = None
    for df in data.values():
        common_idx = df.index if common_idx is None else common_idx.intersection(df.index)
    common_idx = common_idx.sort_values()

    print(f"\n📅 {common_idx[0].date()} → {common_idx[-1].date()} "
          f"({len(common_idx)} gyertya)\n")

    # ── Állapot ──────────────────────────────────────────────────
    capital       = INITIAL_CAPITAL
    peak          = INITIAL_CAPITAL
    day_start_cap = INITIAL_CAPITAL
    weights       = {s: 1.0/len(data) for s in data}
    coin_stats    = {s: {"trades":0,"wins":0,"avg_R":0.0} for s in data}
    active        = {s: None  for s in data}
    entry_px      = {s: 0.0   for s in data}
    stop_px       = {s: 0.0   for s in data}
    target_px     = {s: 0.0   for s in data}  # ÚJ: profit target ára
    stakes        = {s: 0.0   for s in data}
    strat_log     = {s: ""    for s in data}
    be_done       = {s: False for s in data}
    partial_done  = {s: False for s in data}
    loss_streak   = 0
    cooldown_bars = 0
    trades_log    = []
    equity_log    = []
    filter_counts = {}
    prev_day      = None
    bars_since_rebalance = 0

    def fee(stake):
        nonlocal capital
        f = stake * LEVERAGE * TAKER_FEE_PCT
        capital -= f
        return f

    def kelly(sym):
        st = coin_stats[sym]
        if st["trades"] < KELLY_MIN_TRADES:
            return BASE_RISK_PCT
        wr = st["wins"] / st["trades"]
        ar = st["avg_R"]
        if ar <= 0:
            return BASE_RISK_PCT * 0.5
        k = max(0, wr-(1-wr)/max(ar, 0.01)) * KELLY_FRACTION
        return max(BASE_RISK_PCT*0.3, min(k, BASE_RISK_PCT*2.0))

    def rmult(ts, dp):
        m = 1.0
        if loss_streak > 0:      m *= LOSS_STREAK_RED
        if dp >= PROFIT_LOCK_PCT: m *= PROFIT_LOCK_RED
        if ts.weekday() >= 5:    m *= WEEKEND_RED
        return m

    def count_dir(side):
        return sum(1 for s in data if active[s] == side)

    def liq_px(entry, side):
        return entry*(1-1/LEVERAGE+0.005) if side=="LONG" \
               else entry*(1+1/LEVERAGE-0.005)

    def logf(r):
        filter_counts[r] = filter_counts.get(r, 0) + 1

    # ÚJ: Momentum-alapú súlyok frissítése
    def update_momentum_weights(ts):
        nonlocal weights
        scores = {}
        for sym in data:
            idx_pos = data[sym].index.get_loc(ts) if ts in data[sym].index else -1
            if idx_pos < MOMENTUM_LOOKBACK:
                scores[sym] = 1.0
                continue
            past  = float(data[sym]["close"].iloc[idx_pos - MOMENTUM_LOOKBACK])
            now   = float(data[sym]["close"].iloc[idx_pos])
            scores[sym] = max(0.01, (now - past) / past) if past > 0 else 0.01
        total = sum(scores.values())
        for sym in data:
            raw = scores[sym] / total
            weights[sym] = max(MIN_WEIGHT, min(MAX_WEIGHT, raw))
        # Normalizálás
        w_total = sum(weights.values())
        for sym in data:
            weights[sym] /= w_total

    total_bars = len(common_idx)
    for bar_n, ts in enumerate(common_idx):
        if bar_n % (total_bars // 10 or 1) == 0:
            print(f"  ⏳ {bar_n/total_bars*100:.0f}% | Tőke: ${capital:.2f}", end="\r")

        if prev_day != ts.date():
            day_start_cap = capital
            prev_day = ts.date()
        dp = (capital - day_start_cap) / day_start_cap if day_start_cap > 0 else 0

        if cooldown_bars > 0:
            cooldown_bars -= 1

        # Havi momentum-rebalance
        bars_since_rebalance += 1
        if bars_since_rebalance >= MOMENTUM_REBALANCE_BARS:
            update_momentum_weights(ts)
            bars_since_rebalance = 0

        # ÚJ: BTC volatilitás szűrő
        vol_ok = True
        if ts in btc_atr_pct.index:
            atr_p = float(btc_atr_pct.loc[ts])
            if not np.isnan(atr_p) and atr_p < VOL_FILTER_PERCENTILE:
                vol_ok = False

        for sym in data:
            if ts not in data[sym].index:
                continue
            row = data[sym].loc[ts]

            price    = float(row["close"])
            ema      = float(row["ema_trend"])
            atr      = float(row["atr"])
            adx      = float(row["adx"])
            don_high = float(row["don_high"])
            don_low  = float(row["don_low"])
            rsi      = float(row["rsi"])
            bb_upper = float(row["bb_upper"])
            bb_lower = float(row["bb_lower"])
            em       = float(row["eff_mult"])
            dtrend   = str(row.get("daily_trend","UNKNOWN"))
            pos      = active[sym]

            # ── NYITÁS ───────────────────────────────────────────
            if pos is None:
                if cooldown_bars > 0:
                    logf("COOLDOWN"); continue

                is_long  = (price > ema and price > don_high and dtrend == "BULL")
                is_short = (price < ema and price < don_low  and dtrend == "BEAR")
                strat    = "TREND"
                flt      = None

                if is_long or is_short:
                    sc = "LONG" if is_long else "SHORT"
                    if   adx < ADX_THRESHOLD:            flt = "ADX_LOW"
                    elif ts.hour in (1,2,3,4,5):         flt = "LOW_LIQ_HOUR"
                    elif count_dir(sc) >= MAX_CORR_POS:  flt = "CORR_LIMIT"
                    elif not vol_ok:                     flt = "LOW_VOLATILITY"
                    if flt:
                        logf(flt); is_long = is_short = False

                # Mean-reversion (csak ha van elég volatilitás)
                if not is_long and not is_short and adx < ADX_THRESHOLD and vol_ok:
                    if rsi <= RSI_OVERSOLD and price <= bb_lower:
                        if count_dir("LONG") < MAX_CORR_POS:
                            is_long = True; strat = "MEAN_REV"
                    elif rsi >= RSI_OVERBOUGHT and price >= bb_upper:
                        if count_dir("SHORT") < MAX_CORR_POS:
                            is_short = True; strat = "MEAN_REV"

                if not is_long and not is_short:
                    continue

                side  = "LONG" if is_long else "SHORT"
                soff  = atr * STOP_HUNT_ATR if strat == "TREND" else 0
                sdist = atr * em + soff
                cstop = price - sdist if is_long else price + sdist

                # Likvidáció védelem
                lq = liq_px(price, side)
                if side=="LONG"  and cstop < lq*(1+LIQ_BUFFER): cstop = lq*(1+LIQ_BUFFER)
                if side=="SHORT" and cstop > lq*(1-LIQ_BUFFER): cstop = lq*(1-LIQ_BUFFER)

                # ÚJ: Profit target kiszámítása belépéskor
                pt = price + atr*PROFIT_TARGET_ATR if is_long \
                     else price - atr*PROFIT_TARGET_ATR

                allowed = capital * weights[sym]
                er      = kelly(sym) * rmult(ts, dp)
                sdf     = abs(price - cstop)
                stake   = min((allowed*er)/(sdf/price), allowed) if sdf > 0 else 0
                if stake <= 0: continue

                fee(stake)
                active[sym]      = side
                entry_px[sym]    = price
                stop_px[sym]     = cstop
                target_px[sym]   = pt       # ÚJ
                stakes[sym]      = stake
                strat_log[sym]   = strat
                be_done[sym]     = False
                partial_done[sym]= False

            # ── MENEDZSMENT ──────────────────────────────────────
            else:
                entry = entry_px[sym]
                stake = stakes[sym]
                stop  = stop_px[sym]
                tgt   = target_px[sym]
                strat = strat_log[sym]
                raw   = (price-entry)/entry if pos=="LONG" else (entry-price)/entry

                # Break-even
                if raw >= 0.01 and not be_done[sym]:
                    be = entry*1.001 if pos=="LONG" else entry*0.999
                    if (pos=="LONG" and be>stop) or (pos=="SHORT" and be<stop):
                        stop_px[sym] = be; stop = be
                    be_done[sym] = True

                # Részleges profit (2%-nál)
                if raw >= 0.02 and not partial_done[sym]:
                    half = stake / 2.0
                    capital += (half*LEVERAGE)*raw
                    fee(half)
                    stakes[sym] = half; stake = half
                    partial_done[sym] = True

                # Trailing stop frissítés
                cur_stop   = stop_px[sym]
                exit_trade = False
                exit_price = price
                pnl        = 0.0
                exit_type  = ""

                if pos == "LONG":
                    ns = price - atr*em
                    if ns > cur_stop: stop_px[sym] = ns; cur_stop = ns

                    # Profit target CSAK mean-reversion esetén — trend stratégiánál
                    # hagyjuk futni a pozíciót, ott a trailing stop kezeli a kilépést
                    if strat == "MEAN_REV" and tgt > 0 and price >= tgt:
                        pnl = (tgt - entry) / entry
                        exit_price = tgt
                        exit_trade = True; exit_type = "PROFIT_TARGET"
                    elif price <= cur_stop:
                        pnl = (cur_stop - entry) / entry
                        exit_trade = True; exit_type = "STOP"

                elif pos == "SHORT":
                    ns = price + atr*em
                    if ns < cur_stop: stop_px[sym] = ns; cur_stop = ns

                    if strat == "MEAN_REV" and tgt > 0 and price <= tgt:
                        pnl = (entry - tgt) / entry
                        exit_price = tgt
                        exit_trade = True; exit_type = "PROFIT_TARGET"
                    elif price >= cur_stop:
                        pnl = (entry - cur_stop) / entry
                        exit_trade = True; exit_type = "STOP"

                if exit_trade:
                    profit = (stake*LEVERAGE)*pnl
                    capital += profit
                    fee(stake)

                    st = coin_stats[sym]
                    st["trades"] += 1
                    if pnl > 0: st["wins"] += 1
                    st["avg_R"] = st["avg_R"]*(1-ALPHA_RECENCY) + pnl*ALPHA_RECENCY

                    if pnl > 0:
                        loss_streak = 0
                    else:
                        loss_streak += 1
                        if loss_streak >= LOSS_STREAK_LIM:
                            cooldown_bars = 6; loss_streak = 0

                    trades_log.append({
                        "ts": ts, "symbol": sym, "side": pos,
                        "strategy": strat, "exit_type": exit_type,
                        "entry": entry, "exit": exit_price,
                        "stake": stake, "profit": profit,
                        "roi_pct": pnl*LEVERAGE*100, "capital": capital,
                    })
                    active[sym] = None
                    be_done[sym] = False; partial_done[sym] = False

        if capital > peak: peak = capital
        equity_log.append({"ts": ts, "capital": capital, "peak": peak})

    print(f"\n\n  ✅ Kész! {len(trades_log)} trade.")

    # ══════════════════════════════════════════════════════════════
    #  📈  EREDMÉNYEK
    # ══════════════════════════════════════════════════════════════
    tdf = pd.DataFrame(trades_log)
    edf = pd.DataFrame(equity_log).set_index("ts")

    print("\n" + "="*65)
    print("  EREDMÉNYEK (2026 nélkül)")
    print("="*65)

    tdf_clean = tdf[pd.to_datetime(tdf["ts"]).dt.year < 2026] if not tdf.empty else tdf

    if tdf_clean.empty:
        print("  Nincs elég adat."); return

    total  = len(tdf_clean)
    wins   = (tdf_clean["profit"] > 0).sum()
    losses = total - wins
    wr     = wins/total*100
    net    = tdf_clean["profit"].sum()
    ret    = net/INITIAL_CAPITAL*100
    gw     = tdf_clean.loc[tdf_clean["profit"]>0,  "profit"].sum()
    gl     = abs(tdf_clean.loc[tdf_clean["profit"]<=0, "profit"].sum())
    pf     = gw/gl if gl > 0 else 999

    edf_clean = edf[edf.index.year < 2026]
    edf_clean = edf_clean.copy()
    edf_clean["ret"] = edf_clean["capital"].pct_change()
    sharpe = (edf_clean["ret"].mean()/edf_clean["ret"].std()*np.sqrt(6*252)
              ) if edf_clean["ret"].std() > 0 else 0
    edf_clean["dd"] = (edf_clean["peak"] - edf_clean["capital"]) / edf_clean["peak"]
    max_dd = edf_clean["dd"].max()*100

    print(f"\n  Tőke: ${INITIAL_CAPITAL:.2f} → ${INITIAL_CAPITAL + net:.2f}")
    print(f"  Nettó profit:    ${net:.2f}  ({ret:.1f}%)")
    print(f"  Max Drawdown:    {max_dd:.1f}%")
    print(f"  Trades:          {total} ({wins} nyerő | {losses} vesztes)")
    print(f"  Win rate:        {wr:.1f}%")
    print(f"  Profit Factor:   {pf:.2f}")
    print(f"  Sharpe:          {sharpe:.2f}")

    # Exit típus bontás
    if "exit_type" in tdf_clean.columns:
        print(f"\n  EXIT TÍPUSOK:")
        for et in tdf_clean["exit_type"].unique():
            etdf = tdf_clean[tdf_clean["exit_type"]==et]
            etwr = (etdf["profit"]>0).sum()/len(etdf)*100
            print(f"     {et:18}: {len(etdf):4} trade | {etwr:.0f}% WR | ${etdf['profit'].sum():.2f}")

    print(f"\n  COIN BONTÁS:")
    for sym in SYMBOLS:
        st = coin_stats.get(sym)
        if not st or st["trades"]==0: continue
        sw = st["wins"]/st["trades"]*100
        sp = tdf_clean[tdf_clean["symbol"]==sym]["profit"].sum()
        print(f"     {sym:10}: {st['trades']:3} trade | {sw:.0f}% WR | ${sp:.2f}")

    print(f"\n  STRATÉGIA BONTÁS:")
    for s in tdf_clean["strategy"].unique():
        sdf = tdf_clean[tdf_clean["strategy"]==s]
        sw  = (sdf["profit"]>0).sum()/len(sdf)*100
        print(f"     {s:14}: {len(sdf):3} trade | {sw:.0f}% WR | ${sdf['profit'].sum():.2f}")

    tdf_clean = tdf_clean.copy()
    tdf_clean["month"] = pd.to_datetime(tdf_clean["ts"]).dt.to_period("M")
    monthly = tdf_clean.groupby("month")["profit"].sum()
    pos_m   = (monthly > 0).sum()
    print(f"\n  Nyereséges hónapok: {pos_m}/{len(monthly)} ({pos_m/len(monthly)*100:.0f}%)")
    print(f"  Legjobb hónap:  ${monthly.max():.2f}")
    print(f"  Legrosszabb:    ${monthly.min():.2f}")

    # Értékelés
    print(f"\n{'='*65}")
    print("  ÉRTÉKELÉS:")
    goods, issues = [], []
    if ret > 100:    goods.append(f"✅ Kiváló hozam: {ret:.1f}%")
    elif ret > 20:   goods.append(f"✅ Pozitív hozam: {ret:.1f}%")
    else:            issues.append(f"❌ Gyenge hozam: {ret:.1f}%")
    if sharpe > 1.5: goods.append(f"✅ Kiváló Sharpe: {sharpe:.2f}")
    elif sharpe>0.5: goods.append(f"⚠️  Elfogadható Sharpe: {sharpe:.2f}")
    else:            issues.append(f"❌ Gyenge Sharpe: {sharpe:.2f}")
    if max_dd < 20:  goods.append(f"✅ Alacsony drawdown: {max_dd:.1f}%")
    elif max_dd<35:  issues.append(f"⚠️  Közepes drawdown: {max_dd:.1f}%")
    else:            issues.append(f"❌ Magas drawdown: {max_dd:.1f}%")
    if pf > 1.5:     goods.append(f"✅ Erős profit factor: {pf:.2f}")
    elif pf > 1.0:   issues.append(f"⚠️  Közepes profit factor: {pf:.2f}")
    else:            issues.append(f"❌ Profit factor < 1.0!")
    for g in goods:  print(f"  {g}")
    for i in issues: print(f"  {i}")
    if not issues:
        print("\n  🚀 KÉSZ A PAPER MODE TESZTELÉSRE!")
    elif len(issues) <= 1:
        print("\n  ⚠️  PAPER módban futtatható.")
    else:
        print("\n  🛑 Még javítás szükséges.")
    print("="*65)

    tdf_clean.to_csv("backtest_trades.csv", index=False)
    edf_clean.to_csv("backtest_equity.csv")
    print("\n  Mentve: backtest_trades.csv | backtest_equity.csv")

if __name__ == "__main__":
    run_backtest()