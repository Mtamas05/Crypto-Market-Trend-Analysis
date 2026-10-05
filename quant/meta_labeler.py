import pickle
import os

MODEL_PATH = "meta_model.pkl"
_model = None

def load_model():
    global _model
    if os.path.exists(MODEL_PATH):
        try:
            with open(MODEL_PATH, "rb") as f:
                _model = pickle.load(f)
        except Exception as e:
            print("Meta model load error:", e)

def evaluate_trade_meta(features: dict) -> float:
    global _model
    if _model is None:
        load_model()
        
    if _model is not None:
        try:
            import pandas as pd
            # Features are: adx, rsi, atr_pct, don_width_pct, dist_ema_pct, side, cvd_slope
            f_df = pd.DataFrame([{
                "adx": features.get("adx", 20),
                "rsi": features.get("rsi", 50),
                "atr_pct": features.get("atr_pct", 5.0),
                "don_width_pct": features.get("don_width_pct", 10.0),
                "dist_ema_pct": features.get("dist_ema_pct", 2.0),
                "side": features.get("side", 1.0),
                "cvd_slope": features.get("cvd_slope", 0.0)
            }])
            prob = _model.predict_proba(f_df)[0][1]
            return float(prob)
        except Exception as e:
            print("Model prediction error:", e)
            
    # Fallback heuristic
    adx = features.get("adx", 20)
    rsi = features.get("rsi", 50)
    side = features.get("side", 1.0)
    
    prob = 0.60
    if adx < 15:
        prob -= 0.2
    if (side == 1.0 and rsi > 70) or (side == -1.0 and rsi < 30):
        prob -= 0.15
        
    return max(0.0, min(1.0, prob))
