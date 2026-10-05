#!/usr/bin/env python3
"""
quant_arsenal.py — Tier-1 HFT & Kvant Kiegészítő Modul
======================================================
1. VPIN (Volume-Synchronized Probability of Toxicity)
2. Parabolic Chandelier Trailing Exit
3. Z-Score Cross-Asset Dispersion
"""

import time
import numpy as np
from collections import deque

class VPINCalculator:
    """Valós idejű VPIN számító az @aggTrade WebSocket adatokhoz."""
    def __init__(self, bucket_size_usd=100000.0, num_buckets=30):
        self.bucket_size = bucket_size_usd
        self.num_buckets = num_buckets
        self.current_buy = 0.0
        self.current_sell = 0.0
        self.buckets = deque(maxlen=num_buckets)

    def update_trade(self, price: float, qty: float, is_buyer_maker: bool):
        trade_usd = price * qty
        # is_buyer_maker=True -> a vevő volt Maker, az eladó volt agresszor (Sell)
        if is_buyer_maker:
            self.current_sell += trade_usd
        else:
            self.current_buy += trade_usd

        # Ha megtelt a volumenkosár, lezárjuk
        while (self.current_buy + self.current_sell) >= self.bucket_size:
            total = self.current_buy + self.current_sell
            excess = total - self.bucket_size
            ratio = self.bucket_size / total if total > 0 else 1.0

            b_vol = self.current_buy * ratio
            s_vol = self.current_sell * ratio
            imbalance = abs(b_vol - s_vol)
            self.buckets.append(imbalance)

            # Maradék átvitele a következő kosárba
            self.current_buy = self.current_buy * (excess / total) if total > 0 else 0.0
            self.current_sell = self.current_sell * (excess / total) if total > 0 else 0.0

    def get_vpin(self) -> float:
        if len(self.buckets) < 10:
            return 0.35  # Bázis alapérték, amíg nincs elég kosár
        return float(np.sum(self.buckets) / (len(self.buckets) * self.bucket_size))


def calculate_chandelier_exit(entry_price: float, current_price: float, peak_price: float,
                              side: str, atr: float, open_ts: float, 
                              current_vol: float, baseline_vol: float,
                              base_mult: float = 1.8) -> float:
    """
    Parabolic Chandelier Exit idő-lecsengéssel és volatilitás-sokk védelemmel.
    """
    elapsed_hours = (time.time() - open_ts) / 3600.0
    time_decay = 1.0 / (1.0 + 0.05 * elapsed_hours)
    
    # Volatilitás skálázás (clamped 0.7 - 1.5)
    vol_ratio = np.sqrt(current_vol / baseline_vol) if baseline_vol > 0 else 1.0
    vol_ratio = max(0.7, min(1.5, vol_ratio))
    
    effective_dist = atr * base_mult * time_decay * vol_ratio
    
    if side == "LONG":
        raw_stop = peak_price - effective_dist
        # Profit Lock: ha már +2.5 ATR felett járunk, a stop minimum +1.2 ATR nyereségre rögzül
        if (peak_price - entry_price) >= 2.5 * atr:
            lock_level = entry_price + 1.2 * atr
            return max(raw_stop, lock_level)
        return raw_stop
    else:
        raw_stop = peak_price + effective_dist
        if (entry_price - peak_price) >= 2.0 * atr:
            lock_level = entry_price - 1.0 * atr
            return min(raw_stop, lock_level)
        return raw_stop


def compute_cross_asset_zscore(coin_returns: np.ndarray, btc_returns: np.ndarray) -> float:
    """
    Kiszámítja az adott altcoin relatív elhajlását a BTC-hez képest (Z-score).
    """
    if len(coin_returns) < 20 or len(btc_returns) < 20:
        return 0.0
    # Kovariancia és béta számítás
    cov = np.cov(coin_returns, btc_returns)[0, 1]
    var_btc = np.var(btc_returns)
    beta = cov / var_btc if var_btc > 0 else 1.0
    
    spread = coin_returns - beta * btc_returns
    mu = np.mean(spread)
    sigma = np.std(spread)
    
    if sigma <= 1e-8:
        return 0.0
    return float((spread[-1] - mu) / sigma)

