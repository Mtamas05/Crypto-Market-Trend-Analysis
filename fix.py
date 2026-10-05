import re

with open('pro_bot_v5_pi.py', 'r', encoding='utf-8') as f:
    c = f.read()

c = c.replace('[{strat}]\n"', '[{strat}]\\n"')
c = c.replace('Target: {tgt:.4f}\n"', 'Target: {tgt:.4f}\\n"')
c = c.replace('Díj: ${fee:.2f}\n"', 'Díj: ${fee:.2f}\\n"')
c = c.replace('[{exit_type}]\n"', '[{exit_type}]\\n"')
c = c.replace('(${profit:.2f})\n"', '(${profit:.2f})\\n"')
c = c.replace('INDUL!\n"', 'INDUL!\\n"')
c = c.replace("total_capital']:.2f}\n\"", "total_capital']:.2f}\\n\"")
c = c.replace('Mód: {EXECUTION_MODE}\n"', 'Mód: {EXECUTION_MODE}\\n"')
c = c.replace('ATR\n"', 'ATR\\n"')
c = c.replace("BASE_SYMBOLS)}\n\"", "BASE_SYMBOLS)}\\n\"")
c = c.replace('MAX_DRAWDOWN*100:.0f}%\n"', 'MAX_DRAWDOWN*100:.0f}%\\n"')
c = c.replace('CORRELATION_THRESHOLD}\n"', 'CORRELATION_THRESHOLD}\\n"')
c = c.replace('JELENTÉS\n"', 'JELENTÉS\\n"')
c = c.replace("peak_capital']:.2f}\n\"", "peak_capital']:.2f}\\n\"")
c = c.replace("current_regime','?')}\n\"", "current_regime','?')}\\n\"")
c = c.replace('szint).\n"', 'szint).\\n"')
c = c.replace('nyit, \n"', 'nyit, \\n"')
c = c.replace('tovább.\n"', 'tovább.\\n"')
c = c.replace('nyit,\n"', 'nyit,\\n"')
c = c.replace('elérve — \n"', 'elérve — \\n"')
c = c.replace('csúcstól \n"', 'csúcstól \\n"')

with open('pro_bot_v5_pi.py', 'w', encoding='utf-8') as f:
    f.write(c)
