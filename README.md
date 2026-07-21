# 📈 Crypto Market Trend Analysis & Automated Trading System

A comprehensive Python-based ecosystem designed to extract historical cryptocurrency data, analyze market trends, backtest trading strategies, and execute automated trading logic.

## 🚀 Project Overview

This project bridges the gap between data analysis and automated financial systems. It is built with a modular architecture, separating data acquisition, historical backtesting, data analysis, and the live trading engine. 

*(Note: Sensitive configuration files, local databases, and execution logs are intentionally excluded from this repository for security purposes.)*

## 🛠️ Tech Stack & Features

*   **Language:** Python 3.x
*   **Core Logic:** Algorithmic trading execution & market trend evaluation
*   **Data Handling:** Automated historical data downloading and parsing
*   **Modularity:** 
    *   `download_history.py`: Fetches and formats raw market data from exchange APIs.
    *   `analyze.py`: Processes data and visualizes trends.
    *   `backtest.py`: Simulates trading strategies against historical data to evaluate potential profitability.
    *   `pro_bot_v5_pi.py`: The core automated trading engine designed for deployment (e.g., on a Raspberry Pi or cloud server).

## 💻 System Architecture

The bot is designed to be lightweight and scalable. It utilizes local `.db` (SQLite) instances for state management and trading history logging (ignored in version control) and relies on a secure `config.py` structure to handle API keys and environment variables safely.

## ⚙️ How to Run (Local Setup)

1. Clone the repository:
   ```bash
   git clone [https://github.com/Mtamas05/Crypto-Market-Trend-Analysis.git](https://github.com/Mtamas05/Crypto-Market-Trend-Analysis.git)
   
