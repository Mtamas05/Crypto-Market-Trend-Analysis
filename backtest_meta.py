#!/usr/bin/env python3
"""
backtest_meta.py — Kvant Stratégia & Meta-Labeling Validáció
=============================================================
Összehasonlítja az alap Donchian+EMA stratégiát a LightGBM szűréssel ellátott változattal.
"""

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import classification_report, roc_auc_score
import requests
import sys
sys.stdout.reconfigure(encoding='utf-8')

def fetch_historical_klines(symbol="BTCUSDT", interval="4h", limit=1000):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    data = requests.get(url, timeout=15).json()
    df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume","close_ts","qv","trades","tbb","tbq","ign"])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return df.set_index("ts")

def calculate_indicators(df):
    df = df.copy()
    # Trend és csatornák
    df["ema_trend"] = df["close"].ewm(span=180, adjust=False).mean()
    df["don_high"] = df["high"].shift(1).rolling(20).max()
    df["don_low"] = df["low"].shift(1).rolling(20).min()
    
    # ATR
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs()
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    
    # ADX & RSI
    up = df["high"].diff()
    down = -df["low"].diff()
    pdm = np.where((up > down) & (up > 0), up, 0.0)
    mdm = np.where((down > up) & (down > 0), down, 0.0)
    atr_s = tr.ewm(span=14, adjust=False).mean()
    pdi = 100 * pd.Series(pdm, index=df.index).ewm(span=14, adjust=False).mean() / atr_s
    mdi = 100 * pd.Series(mdm, index=df.index).ewm(span=14, adjust=False).mean() / atr_s
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    df["adx"] = dx.ewm(span=14, adjust=False).mean()
    
    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(span=14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(span=14, adjust=False).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, 1e-9)))
    
    # Normalizált jellemzők az ML-nek
    df["atr_pct"] = (df["atr"] / df["close"]) * 100
    df["don_width_pct"] = ((df["don_high"] - df["don_low"]) / df["close"]) * 100
    df["dist_ema_pct"] = ((df["close"] - df["ema_trend"]) / df["ema_trend"]) * 100
    return df.dropna()

def simulate_events(df, atr_mult=1.8, profit_target_atr=3.0, max_bars=30):
    events = []
    
    for i in range(len(df) - max_bars):
        row = df.iloc[i]
        price = row["close"]
        side = None
        
        if price > row["ema_trend"] and price > row["don_high"]:
            side = "LONG"
        elif price < row["ema_trend"] and price < row["don_low"]:
            side = "SHORT"
            
        if not side:
            continue
            
        atr = row["atr"]
        stop_price = price - atr * atr_mult if side == "LONG" else price + atr * atr_mult
        target_price = price + atr * profit_target_atr if side == "LONG" else price - atr * profit_target_atr
        
        # Jövőbeli árfolyam-ablak figyelése (Triple Barrier)
        future = df.iloc[i+1 : i+1+max_bars]
        ret = 0.0
        success = 0
        
        for _, frow in future.iterrows():
            if side == "LONG":
                if frow["low"] <= stop_price:
                    ret = -atr_mult * (atr / price)
                    success = 0
                    break
                elif frow["high"] >= target_price:
                    ret = profit_target_atr * (atr / price)
                    success = 1
                    break
            else:
                if frow["high"] >= stop_price:
                    ret = -atr_mult * (atr / price)
                    success = 0
                    break
                elif frow["low"] <= target_price:
                    ret = profit_target_atr * (atr / price)
                    success = 1
                    break
                    
        events.append({
            "idx": i,
            "side": 1.0 if side == "LONG" else -1.0,
            "adx": row["adx"],
            "rsi": row["rsi"],
            "atr_pct": row["atr_pct"],
            "don_width_pct": row["don_width_pct"],
            "dist_ema_pct": row["dist_ema_pct"],
            "raw_return": ret,
            "label": success
        })
        
    return pd.DataFrame(events)

def run_meta_evaluation():
    print("📥 Historikus adatok letöltése (BTCUSDT)...")
    df = fetch_historical_klines("BTCUSDT", "4h", limit=1500)
    df = calculate_indicators(df)
    
    print("⚙️ Kereskedési jelek generálása...")
    events = simulate_events(df)
    
    if len(events) < 50:
        print("Kevés jel keletkezett a tesztablakban.")
        return
        
    # Időbeli szétválasztás (Out-of-Sample)
    split_idx = int(len(events) * 0.70)
    train_df = events.iloc[:split_idx]
    test_df = events.iloc[split_idx:].copy()
    
    features = ["adx", "rsi", "atr_pct", "don_width_pct", "dist_ema_pct", "side"]
    
    # Modell illesztése
    model = lgb.LGBMClassifier(
        n_estimators=100,
        learning_rate=0.03,
        max_depth=3,
        num_leaves=7,
        random_state=42,
        verbose=-1
    )
    model.fit(train_df[features], train_df["label"])
    
    test_df["prob"] = model.predict_proba(test_df[features])[:, 1]
    
    # Eredmények összehasonlítása
    fee_per_trade = 0.0004 * 2  # Belépő és kilépő Maker/Taker átlag
    
    # 1. Alap stratégia (minden jelre belép)
    base_trades = test_df["raw_return"] - fee_per_trade
    base_pnl = base_trades.sum()
    base_wr = (test_df["label"] == 1).mean()
    
    # 2. Meta-Labeler szűrt stratégia
    threshold = test_df["prob"].quantile(0.75) if len(test_df) > 0 else 0.50
    filtered = test_df[test_df["prob"] >= threshold]
    filt_trades = filtered["raw_return"] - fee_per_trade
    filt_pnl = filt_trades.sum()
    filt_wr = (filtered["label"] == 1).mean() if len(filtered) > 0 else 0
    
    print("\n" + "="*50)
    print("📈 OUT-OF-SAMPLE TESZT EREDMÉNYEK")
    print("="*50)
    print(f"Összes jel (Test halmaz): {len(test_df)}")
    print(f"Alap Stratégia Win Rate: {base_wr*100:.1f}% | Összhozam: {base_pnl*100:.2f}%")
    print(f"Szűrt jelek száma (Meta): {len(filtered)} ({len(filtered)/len(test_df)*100:.1f}% átengedve)")
    print(f"Meta-Labeler Win Rate : {filt_wr*100:.1f}% | Összhozam: {filt_pnl*100:.2f}%")
    
    # Feature fontosság
    fi = dict(zip(features, model.feature_importances_))
    print("\n🔍 Döntési faktorok fontossága (Feature Importance):")
    for feat, imp in sorted(fi.items(), key=lambda x: x[1], reverse=True):
        print(f" - {feat:15s}: {imp}")

if __name__ == "__main__":
    run_meta_evaluation()
