# PRO-BOT V5 — Tier-1 Quantitative Trading Engine

> An institutional-grade algorithmic cryptocurrency trading engine designed with High-Frequency Trading (HFT) and Quantitative hedge fund principles.

## 🚀 Architecture Overview

The system has been upgraded to a modern, professional quantitative architecture:

`	ext
Crypto-Trading-Bot/
├── pro_bot_v5_pi.py     # Main Engine Entrypoint
├── config.py            # Global API and strategy configurations
├── quant/               # Math, ML, & Microstructure Models
│   ├── deep_quant.py    # Kalman Filter, Hurst Exponent, OU Half-Life
│   ├── quant_arsenal.py # VPIN Toxic Flow, Chandelier Exit, Z-Score
│   └── meta_labeler.py  # LightGBM Trade Classification
├── tools/               # Subsystems & UI
│   ├── dashboard.py     # Flask Web Dashboard
│   ├── reporter.py      # Telegram and logging dispatcher
│   ├── status_cli.py    # Command-line status checker
│   └── watchdog.py      # Daemon and recovery system
├── research/            # Backtesting & Data Science
│   ├── backtest.py      # Historical vector backtester
│   └── train_meta_model.py # Meta-Labeler ML trainer
├── data/                # SQLite Databases & JSON State
└── logs/                # Execution Logs
`

## 🧠 Deep Quantitative Features

The bot now utilizes the same mathematical frameworks used by top-tier prop desks:

1. **VPIN (Volume-Synchronized Probability of Toxicity)**: Real-time order flow toxicity measurement via @aggTrade WebSockets to defend Maker limit orders from being swept by institutional players.
2. **Dynamic Kalman Filter (β)**: Continuously updates the cross-asset correlation beta between altcoins and Bitcoin to detect extreme mean-reverting statistical arbitrage spreads.
3. **Fractal Regime Detection (Hurst Exponent)**: Calculates the *H* exponent to categorically filter out random walk noise (H ~ 0.50), rejecting trades unless the market is structurally mean-reverting (H < 0.48) or persistently trending (H > 0.52).
4. **Ornstein-Uhlenbeck (OU) Mean-Reversion Half-Life**: Solves the OU stochastic differential equation to calculate the half-life of a spread. Only enters reversal trades if the predicted snap-back time is under 12 hours.
5. **Alpha Decay Chandelier Exit**: A trailing stop mechanism that tightens dynamically based on the age of the position (Time Decay) and localized volatility shocks.
6. **Bayesian Online Change-Point Detection (BOCPD)**: A probability matrix that detects sudden market regime shifts in real-time, executing panic-stops (0.5 ATR) instantly when a macro event occurs.

## 🔧 Installation & Usage

1. **Virtual Environment Setup**:
   `ash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # Linux/Mac:
   source .venv/bin/activate
   
   pip install -r requirements.txt
   `

2. **Run the Trading Engine**:
   `ash
   python pro_bot_v5_pi.py
   `

3. **Start the Dashboard**:
   `ash
   cd tools
   python dashboard.py
   `

## ⚠️ Disclaimer
This software is provided for educational and research purposes. Quantitative finance involves significant risk. Never trade with capital you cannot afford to lose.
