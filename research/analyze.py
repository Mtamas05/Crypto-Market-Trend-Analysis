import pandas as pd

df = pd.read_csv('backtest_trades.csv')
df['ts'] = pd.to_datetime(df['ts'])
df = df[df['ts'].dt.year < 2026]

total  = len(df)
wins   = (df['profit'] > 0).sum()
losses = total - wins
net    = df['profit'].sum()
gross_win  = df.loc[df['profit'] > 0,  'profit'].sum()
gross_loss = abs(df.loc[df['profit'] <= 0, 'profit'].sum())
pf = gross_win / gross_loss if gross_loss > 0 else 999

print("=" * 50)
print("  BACKTEST VALÓS EREDMÉNY (2023-2025)")
print("=" * 50)
print(f"  Trades:          {total}")
print(f"  Nyero:           {wins}  ({wins/total*100:.1f}%)")
print(f"  Vesztes:         {losses}  ({losses/total*100:.1f}%)")
print(f"  Netto profit:    ${net:.2f}")
print(f"  Profit factor:   {pf:.2f}")
print()
print("  Eves bontás:")
yearly = df.groupby(df['ts'].dt.year)['profit'].agg(['sum','count','mean']).round(2)
yearly.columns = ['profit_usd', 'trade_db', 'atlag_usd']
print(yearly.to_string())

print()
print("  Legjobb 5 trade:")
top = df.nlargest(5, 'roi_pct')[['ts','symbol','side','roi_pct','profit']]
print(top.to_string(index=False))

print()
print("  Legrosszabb 5 trade:")
bot = df.nsmallest(5, 'roi_pct')[['ts','symbol','side','roi_pct','profit']]
print(bot.to_string(index=False))

print()
# Havi nyereséges hónapok aránya
df['month'] = df['ts'].dt.to_period('M')
monthly = df.groupby('month')['profit'].sum()
pos_months = (monthly > 0).sum()
print(f"  Nyeresegese honapok: {pos_months}/{len(monthly)} ({pos_months/len(monthly)*100:.0f}%)")
print(f"  Legjobb honap:  ${monthly.max():.2f}")
print(f"  Legrosszabb:    ${monthly.min():.2f}")