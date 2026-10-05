import os
import sys
import numpy as np
import pandas as pd
import lightgbm as lgb
import pickle
import requests
import time

def fetch_historical_klines(symbol="BTCUSDT", interval="4h", limit=1500):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    data = requests.get(url, timeout=15).json()
    df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume","close_ts","qv","trades","tbb","tbq","ign"])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return df.set_index("ts")

def calculate_indicators(df):
    df = df.copy()
    df["ema_trend"] = df["close"].ewm(span=180, adjust=False).mean()
    df["don_high"] = df["high"].shift(1).rolling(20).max()
    df["don_low"] = df["low"].shift(1).rolling(20).min()
    
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs()
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    
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
        
        if price > row["ema_trend"] and price > row["don_high"]: side = "LONG"
        elif price < row["ema_trend"] and price < row["don_low"]: side = "SHORT"
            
        if not side: continue
            
        atr = row["atr"]
        # Aszimmetrikus teszt
        if side == "LONG":
            stop_price = price - atr * atr_mult
            target_price = price + atr * profit_target_atr
        else:
            stop_price = price + atr * 1.3
            target_price = price - atr * 2.0
        
        future = df.iloc[i+1 : i+1+max_bars]
        success = 0
        
        for _, frow in future.iterrows():
            if side == "LONG":
                if frow["low"] <= stop_price: break
                elif frow["high"] >= target_price:
                    success = 1; break
            else:
                if frow["high"] >= stop_price: break
                elif frow["low"] <= target_price:
                    success = 1; break
                    
        events.append({
            "side": 1.0 if side == "LONG" else -1.0,
            "adx": row["adx"],
            "rsi": row["rsi"],
            "atr_pct": row["atr_pct"],
            "don_width_pct": row["don_width_pct"],
            "dist_ema_pct": row["dist_ema_pct"],
            "cvd_slope": np.random.uniform(-1, 1), # Placeholder for CVD in training, real comes from websocket
            "label": success
        })
    return pd.DataFrame(events)

def run_meta_evaluation():
    print("📥 Historikus adatok letöltése több coinra...")
    all_events = []
    for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT"]:
        print(f"Adatok feldolgozása: {sym}")
        df = fetch_historical_klines(sym, "4h", limit=1500)
        df = calculate_indicators(df)
        events = simulate_events(df)
        all_events.append(events)
        time.sleep(1)
        
    df_events = pd.concat(all_events, ignore_index=True)
    if len(df_events) < 50:
        print("Kevés jel keletkezett.")
        return
        
    features = ["adx", "rsi", "atr_pct", "don_width_pct", "dist_ema_pct", "side", "cvd_slope"]
    
    model = lgb.LGBMClassifier(
        n_estimators=100,
        learning_rate=0.03,
        max_depth=3,
        num_leaves=7,
        random_state=42,
        verbose=-1
    )
    
    print("⚙️ Modell betanítása...")
    model.fit(df_events[features], df_events["label"])
    
    with open("meta_model.pkl", "wb") as f:
        pickle.dump(model, f)
        
    print("✅ Modell sikeresen elmentve: meta_model.pkl")

if __name__ == "__main__":
    run_meta_evaluation()
