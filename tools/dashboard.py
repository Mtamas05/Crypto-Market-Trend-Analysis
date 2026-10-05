import sqlite3
import pandas as pd
import json
import webbrowser
import os
from datetime import datetime
import sys

sys.stdout.reconfigure(encoding='utf-8')

def generate_dashboard(db_path="../data/bot_v5_paper.db", output_html="dashboard.html"):
    if not os.path.exists(db_path):
        print(f"Adatbázis nem található: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    
    # 1. Equity adatok
    try:
        equity_df = pd.read_sql("SELECT ts, capital, peak FROM equity ORDER BY ts ASC", conn)
        equity_df["ts"] = pd.to_datetime(equity_df["ts"], unit="s").dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        print(f"Hiba az equity olvasásakor: {e}")
        equity_df = pd.DataFrame(columns=["ts", "capital", "peak"])
        
    # 2. Legutóbbi Trade-ek
    try:
        trades_df = pd.read_sql("SELECT ts, coin, side, exit_type, profit_usd, roi_pct FROM trades ORDER BY ts DESC LIMIT 50", conn)
        trades_df["ts"] = pd.to_datetime(trades_df["ts"], unit="s").dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        trades_df = pd.DataFrame()

    conn.close()

    equity_labels = equity_df["ts"].tolist() if not equity_df.empty else []
    equity_data = equity_df["capital"].tolist() if not equity_df.empty else []
    peak_data = equity_df["peak"].tolist() if not equity_df.empty else []

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>PRO-BOT V5 Telemetria</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body {{ font-family: Arial, sans-serif; background-color: #121212; color: #ffffff; padding: 20px; }}
            .container {{ max-width: 1200px; margin: auto; }}
            .card {{ background-color: #1e1e1e; padding: 20px; border-radius: 8px; margin-bottom: 20px; }}
            h2 {{ color: #03dac6; border-bottom: 1px solid #333; padding-bottom: 10px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th, td {{ border: 1px solid #333; padding: 10px; text-align: left; }}
            th {{ background-color: #2c2c2c; }}
            .win {{ color: #4caf50; font-weight: bold; }}
            .loss {{ color: #f44336; font-weight: bold; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>📊 PRO-BOT V5 Élő Telemetria</h1>
            <p>Generálva: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            
            <div class="card">
                <h2>📈 Kumulált Tőke (Equity) Görbe</h2>
                <canvas id="equityChart" height="100"></canvas>
            </div>
            
            <div class="card">
                <h2>📝 Legutóbbi Lezárt Pozíciók (Top 50)</h2>
                <table>
                    <tr>
                        <th>Időpont</th>
                        <th>Coin</th>
                        <th>Irány</th>
                        <th>Kilépés Oka</th>
                        <th>Profit (USD)</th>
                        <th>ROI (%)</th>
                    </tr>
                    {"".join([
                        f"<tr><td>{row['ts']}</td><td>{row['coin']}</td><td>{row['side']}</td><td>{row['exit_type']}</td><td class={'win' if row['profit_usd'] > 0 else 'loss'}>${row['profit_usd']:.2f}</td><td class={'win' if row['roi_pct'] > 0 else 'loss'}>{row['roi_pct']:.2f}%</td></tr>" 
                        for _, row in trades_df.iterrows()
                    ]) if not trades_df.empty else "<tr><td colspan='6'>Nincs még lezárt trade.</td></tr>"}
                </table>
            </div>
        </div>

        <script>
            const ctx = document.getElementById('equityChart').getContext('2d');
            new Chart(ctx, {{
                type: 'line',
                data: {{
                    labels: {json.dumps(equity_labels)},
                    datasets: [
                        {{
                            label: 'Tőke ($)',
                            data: {json.dumps(equity_data)},
                            borderColor: '#03dac6',
                            backgroundColor: 'rgba(3, 218, 198, 0.1)',
                            fill: true,
                            tension: 0.1,
                            pointRadius: 0
                        }},
                        {{
                            label: 'Vízválasztó Csúcs ($)',
                            data: {json.dumps(peak_data)},
                            borderColor: '#bb86fc',
                            borderDash: [5, 5],
                            fill: false,
                            pointRadius: 0
                        }}
                    ]
                }},
                options: {{
                    responsive: true,
                    scales: {{
                        x: {{ display: false }},
                        y: {{ beginAtZero: false }}
                    }}
                }}
            }});
        </script>
    </body>
    </html>
    """

    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html_content)
    
    print(f"✅ Dashboard sikeresen legenerálva: {os.path.abspath(output_html)}")
    webbrowser.open(f"file://{os.path.abspath(output_html)}")

if __name__ == "__main__":
    generate_dashboard()
