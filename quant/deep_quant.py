#!/usr/bin/env python3
"""
deep_quant.py — Mély Kvantitatív Matematikai Motor
===================================================
1. Kalmán-szűrő dinamikus fedezeti rátához (Time-varying Beta)
2. Hurst Exponens számító (Fraktális rezsim detektor)
3. Ornstein-Uhlenbeck Mean-Reversion Half-Life kalkulátor
"""

import numpy as np

class KalmanMovingBeta:
    """Kalmán-szűrő valós idejű, adaptív béta számításhoz."""
    def __init__(self, delta=1e-4, R=1e-3):
        self.delta = delta
        self.R = R
        self.theta = np.zeros(2)  # [alfa, béta]
        self.P = np.eye(2)

    def update(self, price_alt: float, price_btc: float) -> tuple[float, float, float]:
        """
        Frissíti az állapotot az új árak alapján.
        Visszatér: (alfa, béta, hiba / spread)
        """
        x = np.array([1.0, price_btc])
        y = price_alt

        # Állapot kovariancia extrapoláció
        self.P += self.delta * np.eye(2)

        # Hiba becslés (Innovation)
        y_hat = np.dot(x, self.theta)
        error = y - y_hat

        # Kalmán erősítés (Gain)
        S = np.dot(x, np.dot(self.P, x)) + self.R
        K = np.dot(self.P, x) / S

        # Állapotvektor és kovariancia frissítés
        self.theta += K * error
        self.P -= np.outer(K, x).dot(self.P)

        alpha, beta = self.theta[0], self.theta[1]
        return alpha, beta, error


def calculate_hurst_exponent(price_series: np.ndarray, max_lags: int = 20) -> float:
    """
    Kiszámítja a Hurst-exponenst egy log-áras idősorra.
    H > 0.55: Trendkövető (Perzisztens)
    H < 0.45: Mean-Reverting (Anti-perzisztens)
    H ~ 0.50: Véletlen zaj (Random Walk)
    """
    if len(price_series) < 60:
        return 0.5

    lags = range(2, max_lags)
    tau = [np.sqrt(np.std(np.subtract(price_series[lag:], price_series[:-lag]))) for lag in lags]
    
    # Ha konstans az ár, elkerüljük a nullával osztást
    if any(t <= 1e-9 for t in tau):
        return 0.5

    poly = np.polyfit(np.log(lags), np.log(tau), 1)
    hurst = poly[0] * 2.0
    return float(np.clip(hurst, 0.0, 1.0))


def calculate_ou_halflife(spread_series: np.ndarray) -> float:
    """
    Ornstein-Uhlenbeck folyamat illesztése lineáris regresszióval:
    Delta(S_t) = theta * (mu - S_{t-1}) * dt + epsilon
    Visszatér a felezési idővel (órákban, ha 1 órás az adat).
    """
    if len(spread_series) < 30:
        return 999.0

    y = np.diff(spread_series)
    x = spread_series[:-1]

    # OLS illesztés a drift taghoz
    x_mean = np.mean(x)
    y_mean = np.mean(y)
    numerator = np.sum((x - x_mean) * (y - y_mean))
    denominator = np.sum((x - x_mean) ** 2)

    if denominator <= 1e-9:
        return 999.0

    theta = -numerator / denominator

    if theta <= 0:
        return 999.0  # Nincs átlaghoz való visszatérés (trendelő/divergáló)

    half_life = np.log(2) / theta
    return float(half_life)
