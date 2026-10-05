import re

with open(r"c:\Users\tamas\Documents\Crypto-Trading-Bot\pro_bot_v5_pi.py", "r", encoding="utf-8") as f:
    content = f.read()

ws_code = """
# ══════════════════════════════════════════════════════════════════
#  🔌 WEBSOCKET MANAGER (Zero Polling)
# ══════════════════════════════════════════════════════════════════
WS_DATA_LOCK = threading.Lock()
LIVE_PRICES = {}       # {"BTCUSDT": 65420.5, ...}
CLOSED_CANDLES_QUEUE = []  # Események: (symbol, interval, close_price)

async def binance_kline_stream(symbols, intervals=["1h", "4h"]):
    streams = [f"{s.lower()}@kline_{iv}" for s in symbols for iv in intervals]
    stream_path = "/".join(streams)
    url = f"wss://fstream.binance.com/stream?streams={stream_path}" if LIVE_TRADING else f"wss://stream.binancefuture.com/stream?streams={stream_path}"
    
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                log.info("🔌 WebSocket kapcsolat kiépítve: Klines")
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if "data" in data and "k" in data["data"]:
                        k = data["data"]["k"]
                        sym = k["s"]
                        is_closed = k["x"]
                        close_price = float(k["c"])
                        interval = k["i"]
                        
                        if is_closed:
                            with WS_DATA_LOCK:
                                CLOSED_CANDLES_QUEUE.append((sym, interval, close_price))
                            log.info("🕯️ Gyertya lezárult: %s %s @ %.4f", sym, interval, close_price)
        except Exception as e:
            log.error("WebSocket Klines hiba: %s. Újracsatlakozás 5s múlva...", e)
            await asyncio.sleep(5)

async def binance_bookticker_stream(symbols):
    streams = [f"{s.lower()}@bookTicker" for s in symbols]
    stream_path = "/".join(streams)
    url = f"wss://fstream.binance.com/stream?streams={stream_path}" if LIVE_TRADING else f"wss://stream.binancefuture.com/stream?streams={stream_path}"
    
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                log.info("🔌 WebSocket kapcsolat kiépítve: BookTicker")
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if "data" in data:
                        sym = data["data"]["s"]
                        bid = float(data["data"]["b"])
                        ask = float(data["data"]["a"])
                        mid = (bid + ask) / 2.0
                        with WS_DATA_LOCK:
                            LIVE_PRICES[sym] = mid
        except Exception as e:
            log.error("WebSocket BookTicker hiba: %s. Újracsatlakozás 5s múlva...", e)
            await asyncio.sleep(5)

def start_websocket_manager(symbols):
    async def run_streams():
        await asyncio.gather(
            binance_kline_stream(symbols),
            binance_bookticker_stream(symbols)
        )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_streams())

# ══════════════════════════════════════════════════════════════════
#  🚀  FŐ KERESKEDÉSI LOGIKA
# ══════════════════════════════════════════════════════════════════
def evaluate_entry(coin, price=None):
    df_4h = fetch_klines(coin, "4h", limit=210)
    df_1d = fetch_klines(coin, "1d", limit=210)
    df_1h = fetch_klines(coin, "1h", limit=60)

    if df_4h.empty or len(df_4h) < 50: return

    price    = price or float(df_4h["close"].iloc[-1])
    ema      = ind_ema(df_4h["close"], EMA_TREND_LEN)
    atr      = ind_atr(df_4h, ATR_LEN)
    adx      = ind_adx(df_4h, ADX_LEN)
    rsi      = ind_rsi(df_4h["close"], RSI_LEN)
    dh, dl   = ind_donchian(df_4h, DONCHIAN_LEN)
    _, bbu, bbl = ind_bollinger(df_4h["close"], BB_LEN, BB_STD)
    em       = ind_eff_mult(df_4h, atr)
    adx_threshold = get_adx_threshold()

    dp, dtrend = price, "UNKNOWN"
    if not df_1d.empty:
        dp     = float(df_1d["close"].iloc[-1])
        dema   = ind_ema(df_1d["close"], EMA_DAILY_LEN)
        dtrend = "BULL" if dp > dema else "BEAR"

    ob  = fetch_ob_imbalance(coin)
    oi  = fetch_oi_change(coin)
    fr  = fetch_funding(coin)
    
    with STATE_LOCK:
        pos = state["active_pos"].get(coin)

    vol_ok = state.get("btc_atr_pct", 50) >= VOL_FILTER_PERCENTILE

    if pos is None:
        if not can_open_new_position():
            dd_pct = (state.get("peak_capital",state["total_capital"])-state["total_capital"]) / max(state.get("peak_capital",1),1) * 100
            reason = "DAILY_LOSS_LIMIT" if daily_loss_breached() else f"DD_CRITICAL({dd_pct:.1f}%)"
            log_dec(coin,price,dp,ema,atr,adx,rsi,dh,dl,ob,oi,dtrend,f"FILTERED_{reason}")
            return

        if is_cooldown():
            log_dec(coin,price,dp,ema,atr,adx,rsi,dh,dl,ob,oi,dtrend,"FILTERED_COOLDOWN")
            return

        is_long  = (price>ema and price>dh and dtrend=="BULL")
        is_short = (price<ema and price<dl  and dtrend=="BEAR")
        strat    = "TREND"
        flt      = None

        if is_long or is_short:
            sc = "LONG" if is_long else "SHORT"
            if   adx < adx_threshold:                          flt = f"ADX_LOW({adx:.1f})"
            elif datetime.now().hour in LOW_LIQUIDITY_HOURS:   flt = "LOW_LIQ_HOUR"
            elif correlated_exposure_count(coin, sc) >= MAX_CORR_POSITIONS: flt = f"CORR_LIMIT({correlated_exposure_count(coin, sc)})"
            elif not vol_ok:                                   flt = "LOW_VOLATILITY"
            elif sc=="LONG"  and ob < 1/OB_IMBALANCE_MIN:     flt = f"OB_BEARISH({ob:.2f})"
            elif sc=="SHORT" and ob > OB_IMBALANCE_MIN:        flt = f"OB_BULLISH({ob:.2f})"
            elif oi < -OI_CHANGE_THR:                          flt = f"OI_DECLINING({oi:.3f})"
            elif sc=="LONG"  and fr > FUNDING_EXTREME_THR:     flt = f"FUNDING_HIGH({fr:.4f})"
            elif sc=="SHORT" and fr < -FUNDING_EXTREME_THR:    flt = f"FUNDING_LOW({fr:.4f})"
            elif get_coin_regime(coin) in ("HIGH_VOL_CHOP","LOW_VOL_CHOP"): flt = f"REGIME_{get_coin_regime(coin)}"
            else:
                div = detect_rsi_divergence(df_4h)
                if sc=="LONG"  and div=="BEARISH_DIV": flt = "RSI_BEARISH_DIV"
                elif sc=="SHORT" and div=="BULLISH_DIV": flt = "RSI_BULLISH_DIV"
                elif not df_1h.empty:
                    e1h = ind_ema(df_1h["close"], EMA_1H_LEN)
                    p1h = float(df_1h["close"].iloc[-1])
                    if sc=="LONG"  and p1h <= e1h: flt = "1H_NOT_CONFIRMED"
                    elif sc=="SHORT" and p1h >= e1h: flt = "1H_NOT_CONFIRMED"
            if flt:
                log_dec(coin,price,dp,ema,atr,adx,rsi,dh,dl,ob,oi,dtrend,f"FILTERED_{flt}")
                is_long = is_short = False

        if not is_long and not is_short and adx < adx_threshold and vol_ok:
            if rsi<=RSI_OVERSOLD  and price<=bbl and count_dir("LONG")<MAX_CORR_POSITIONS:
                is_long=True;  strat="MEAN_REV"
            elif rsi>=RSI_OVERBOUGHT and price>=bbu and count_dir("SHORT")<MAX_CORR_POSITIONS:
                is_short=True; strat="MEAN_REV"

        if is_long or is_short:
            side  = "LONG" if is_long else "SHORT"
            soff  = atr*STOP_HUNT_OFFSET_ATR if strat=="TREND" else 0
            sdist = atr*em+soff
            cstop = price-sdist if is_long else price+sdist
            lq    = liq_price(price, side)
            if side=="LONG"  and cstop < lq*(1+LIQUIDATION_BUFFER): cstop = lq*(1+LIQUIDATION_BUFFER)
            if side=="SHORT" and cstop > lq*(1-LIQUIDATION_BUFFER): cstop = lq*(1-LIQUIDATION_BUFFER)

            tgt = price+atr*PROFIT_TARGET_ATR if is_long else price-atr*PROFIT_TARGET_ATR

            with STATE_LOCK:
                allowed = state["total_capital"] * state["coin_weights"].get(coin, MIN_WEIGHT)
            er      = kelly_frac(coin) * risk_mult()
            sdf     = abs(price-cstop)
            stake   = min((allowed*er)/(sdf/price), allowed) if sdf>0 else 0
            qty     = get_qty(coin, stake, price)
            if qty <= 0: return

            ok, fill = execute_maker_chase(coin, "BUY" if is_long else "SELL", qty)
            if not ok: return

            slip = (fill-price)/price*100 if fill and price else 0.0
            with STATE_LOCK:
                state["active_pos"][coin]     = side
                state["entry_prices"][coin]   = fill or price
                state["stop_losses"][coin]    = cstop
                state["target_prices"][coin]  = tgt
                state["stakes"][coin]         = stake
                state["strategy_used"][coin]  = strat
                state["break_even"][coin]     = False
                state["partial_taken"][coin]  = False
                fee = apply_fee(stake)
                save_state()
                
            log_dec(coin,price,dp,ema,atr,adx,rsi,dh,dl,ob,oi,dtrend,f"OPEN_{side}_{strat}")
            tg_send(f"🔥 NYITÁS: {side} {coin} [{strat}]\n"
                    f"Ár: {price:.4f} | Stop: {cstop:.4f} | Target: {tgt:.4f}\n"
                    f"Kock.: {er*100:.1f}% | Díj: ${fee:.2f}\n"
                    f"OB: {ob:.2f} | OI Δ: {oi*100:.2f}% | Vol%: {state.get('btc_atr_pct',50):.0f}")

def manage_open_positions_realtime(current_prices):
    with STATE_LOCK:
        active_coins = [c for c in state["active_symbols"] if state["active_pos"].get(c) is not None]
        
    for coin in active_coins:
        price = current_prices.get(coin)
        if not price: continue
        
        with STATE_LOCK:
            pos = state["active_pos"].get(coin)
            if not pos: continue
            
            entry  = state["entry_prices"].get(coin,0)
            stop   = state["stop_losses"].get(coin,0)
            tgt    = state["target_prices"].get(coin,0)
            stake  = state["stakes"].get(coin,0)
            strat  = state["strategy_used"].get(coin,"")
            raw    = (price-entry)/entry if pos=="LONG" else (entry-price)/entry

            # Break-even
            if raw>=0.01 and not state["break_even"].get(coin):
                be = entry*1.001 if pos=="LONG" else entry*0.999
                if (pos=="LONG" and be>stop) or (pos=="SHORT" and be<stop):
                    state["stop_losses"][coin]=be; stop=be
                state["break_even"][coin]=True
                tg_send(f"🛡️ {coin} BREAK-EVEN! Stop={be:.4f}"); save_state()

            # Részleges profit
            if raw>=0.02 and not state["partial_taken"].get(coin):
                half = stake/2.0; hqty = get_qty(coin, half, price)
                if hqty > 0:
                    ok, fill = execute_order(coin,"SELL" if pos=="LONG" else "BUY",hqty,order_type="MARKET",price=price,purpose="EXIT")
                    if ok:
                        profit_h = (half*LEVERAGE)*raw
                        state["total_capital"] += profit_h
                        state["stakes"][coin]   = half; stake=half
                        state["partial_taken"][coin] = True
                        fee = apply_fee(half); save_state()
                        tg_send(f"💰 {coin} RÉSZLEGES: +${profit_h:.2f} | Díj: ${fee:.2f}")

            # Trailing stop + profit target
            cur_stop   = state["stop_losses"].get(coin,0)
            exit_trade = False; exit_price=price; pnl=0.0; exit_type=""

            df_4h = fetch_klines(coin, "4h", limit=30)
            if df_4h.empty: continue
            atr = ind_atr(df_4h, ATR_LEN)
            em  = ind_eff_mult(df_4h, atr)

            if pos=="LONG":
                ns = price-atr*em
                if ns>cur_stop: state["stop_losses"][coin]=ns; cur_stop=ns
                if strat=="MEAN_REV" and tgt>0 and price>=tgt:
                    pnl=(tgt-entry)/entry; exit_price=tgt; exit_trade=True; exit_type="PROFIT_TARGET"
                elif price<=cur_stop:
                    pnl=(cur_stop-entry)/entry; exit_trade=True; exit_type="STOP"
            elif pos=="SHORT":
                ns = price+atr*em
                if ns<cur_stop: state["stop_losses"][coin]=ns; cur_stop=ns
                if strat=="MEAN_REV" and tgt>0 and price<=tgt:
                    pnl=(entry-tgt)/entry; exit_price=tgt; exit_trade=True; exit_type="PROFIT_TARGET"
                elif price>=cur_stop:
                    pnl=(entry-cur_stop)/entry; exit_trade=True; exit_type="STOP"

        if exit_trade:
            use_price = price
            qty = get_qty(coin, stake, price)
            ok, fill = execute_order(coin,"SELL" if pos=="LONG" else "BUY",
                                     qty, order_type="MARKET",
                                     price=use_price, purpose="EXIT")
            if ok:
                actual_pnl = ((fill-entry)/entry if pos=="LONG" else (entry-fill)/entry) if fill else pnl
                with STATE_LOCK:
                    profit = (stake*LEVERAGE)*actual_pnl
                    roi    = actual_pnl*LEVERAGE*100
                    slip   = (fill-price)/price*100 if fill and price else 0.0

                    state["total_capital"] += profit
                    fee = apply_fee(stake)
                    update_score(coin, actual_pnl)
                    register_result(actual_pnl)

                    log_trade_db(coin,pos,strat,exit_type,entry,fill or price,
                                 stake,profit,roi,fee,slip)
                    # We skip log_dec here for simplicity or recreate dp, ema...
                    
                    state["active_pos"][coin]    = None
                    state["break_even"][coin]    = False
                    state["partial_taken"][coin] = False
                    save_state()

                em_j = "✅" if profit>0 else "❌"
                tg_send(f"{em_j} ZÁRÁS: {pos} {coin} [{exit_type}]\n"
                        f"Ár: {fill or price:.4f} | ROI: {roi:.2f}% (${profit:.2f})\n"
                        f"Slip: {slip:.3f}% | Díj: ${fee:.2f}\n"
                        f"Tőke: ${state['total_capital']:.2f}")
"""

# Regex replacement
content = re.sub(
    r"# ══════════════════════════════════════════════════════════════════\n#  🚀  FŐ KERESKEDÉSI LOGIKA.*?def self_test\(\):",
    ws_code + "\n# ══════════════════════════════════════════════════════════════════\n#  🔧  ÖNDIAGNÓZIS\n# ══════════════════════════════════════════════════════════════════\ndef self_test():",
    content,
    flags=re.DOTALL
)


main_code = """
def main():
    log.info("=" * 60)
    log.info("  PRO-BOT V5 — Fejlesztett verzió | Mód: %s", EXECUTION_MODE)
    log.info("=" * 60)

    db_init(); load_state()

    cfg = {"ts":time.time(),"mode":EXECUTION_MODE,"base_symbols":BASE_SYMBOLS,
           "max_weight":MAX_WEIGHT,"profit_target_atr":PROFIT_TARGET_ATR,
           "vol_filter_pct":VOL_FILTER_PERCENTILE,"leverage":LEVERAGE,
           "kelly_fraction":KELLY_FRACTION,"has_bybit":HAS_BYBIT,
           "max_drawdown":MAX_DRAWDOWN,"daily_loss_limit_pct":DAILY_LOSS_LIMIT_PCT,
           "drawdown_review_pct":DRAWDOWN_REVIEW_PCT,
           "correlation_threshold":CORRELATION_THRESHOLD}
    db_exec("INSERT INTO config_snapshots (ts,config_json) VALUES (?,?)",
            (time.time(), json.dumps(cfg)))

    if not self_test():
        log.error("Öndiagnózis sikertelen."); sys.exit(1)

    tg_send(f"🚀 PRO-BOT V5 INDUL!\n"
            f"💰 Tőke: ${state['total_capital']:.2f}\n"
            f"🔄 Mód: {EXECUTION_MODE}\n"
            f"🎯 Profit target: {PROFIT_TARGET_ATR}×ATR\n"
            f"🪙 Coinok: {', '.join(BASE_SYMBOLS)}\n"
            f"🛡️ Napi loss-limit: {DAILY_LOSS_LIMIT_PCT*100:.0f}% | Review: {DRAWDOWN_REVIEW_PCT*100:.0f}% | Max DD: {MAX_DRAWDOWN*100:.0f}%\n"
            f"📈 Korrelációs küszöb: {CORRELATION_THRESHOLD}\n"
            f"🔀 Bybit fallback: {'✅' if HAS_BYBIT else '❌'}")

    schedule.every(REGIME_CLUSTER_HOURS).hours.do(
        lambda: threading.Thread(target=train_regime_models_all, daemon=True).start())
    schedule.every(WALKFORWARD_DAYS).days.do(
        lambda: threading.Thread(target=walkforward_optimize, daemon=True).start())
    schedule.every(7).days.do(
        lambda: threading.Thread(target=update_active_symbols, daemon=True).start())
    schedule.every(4).hours.do(
        lambda: threading.Thread(target=update_vol_filter, daemon=True).start())
    schedule.every(4).hours.do(
        lambda: threading.Thread(target=update_correlation_matrix, daemon=True).start())
    schedule.every().day.at("20:00").do(lambda: tg_send(
        f"🌙 NAPI JELENTÉS\n"
        f"📊 Tőke: ${state['total_capital']:.2f}\n"
        f"📉 Csúcs: ${state['peak_capital']:.2f}\n"
        f"🌊 Rezsim: {state.get('current_regime','?')}\n"
        f"🪙 Aktív coinok: {', '.join(state['active_symbols'])}"
    ))
    schedule.every().day.at("00:00").do(reset_daily_loss_window)

    threading.Thread(target=train_regime_models_all, daemon=True).start()
    threading.Thread(target=update_vol_filter, daemon=True).start()
    threading.Thread(target=update_correlation_matrix, daemon=True).start()

    log.info("🔌 WebSocket szál indítása...")
    ws_thread = threading.Thread(target=start_websocket_manager, args=(state["active_symbols"],), daemon=True)
    ws_thread.start()
    
    log.info("✅ Főciklus indul...")
    last_equity_log = 0

    while True:
        try:
            schedule.run_pending()
            tg_check()

            with STATE_LOCK:
                if state["total_capital"] > state["peak_capital"]:
                    state["peak_capital"] = state["total_capital"]
                if state["total_capital"] <= state["peak_capital"]*(1-MAX_DRAWDOWN):
                    tg_send(f"🚨 VÉSZFÉK! Max drawdown ({MAX_DRAWDOWN*100:.0f}%) elérve. Bot leállt.")
                    log.critical("VÉSZFÉK!")
                    break

                if daily_loss_breached() and not state.get("_daily_limit_alerted", False):
                    tg_send(f"🛑 NAPI LOSS-LIMIT ({DAILY_LOSS_LIMIT_PCT*100:.0f}%) elérve — "
                            f"ma nincs több új nyitás, a nyitott pozíciók stopjai futnak tovább.")
                    state["_daily_limit_alerted"] = True
                    save_state()

                _peak = state.get("peak_capital", state["total_capital"])
                _dd_now = (_peak - state["total_capital"]) / _peak if _peak > 0 else 0.0
                if _dd_now >= DRAWDOWN_REVIEW_PCT and not state.get("_review_pending", False):
                    tg_send(f"⚠️ REVIEW SZÜKSÉGES: a tőke {_dd_now*100:.1f}%-ot esett a csúcstól "
                             f"({DRAWDOWN_REVIEW_PCT*100:.0f}%-os figyelmeztető szint).\n"
                             f"A bot a nyitott pozíciókat tovább kezeli, de ÚJ pozíciót nem nyit, "
                             f"amíg a stratégiát át nem nézed és /confirm-mal meg nem erősíted.")
                    state["_review_pending"] = True
                    save_state()
                elif _dd_now < DRAWDOWN_REVIEW_PCT * 0.5 and state.get("_review_pending", False):
                    state["_review_pending"] = False
                    save_state()
                    tg_send("✅ A drawdown jelentősen csökkent — review-blokk automatikusan feloldva.")

                now = time.time()
                if now - state.get("last_rebalance_ts",0) > REBALANCE_HOURS*3600:
                    rebalance()
                    state["last_rebalance_ts"] = now
                    save_state()
                    
            # 1. Valós idejű pozíciómenedzsment (Tick based)
            current_prices = {}
            with WS_DATA_LOCK:
                current_prices = dict(LIVE_PRICES)
            if current_prices:
                try:
                    manage_open_positions_realtime(current_prices)
                except Exception as e:
                    log.error("Hiba pozíció menedzsment közben: %s", e)

            # 2. Belépési logika (Gyertya lezárásra reagálva)
            candles_to_process = []
            with WS_DATA_LOCK:
                candles_to_process = list(CLOSED_CANDLES_QUEUE)
                CLOSED_CANDLES_QUEUE.clear()
                
            for sym, interval, c_price in candles_to_process:
                if interval == "4h":
                    log.info("🎯 4h Gyertya zárult %s esetén, belépés vizsgálata...", sym)
                    try:
                        evaluate_entry(sym, c_price)
                    except Exception as e:
                        log.error("Hiba belépés vizsgálatakor (%s): %s", sym, e)

            if time.time() - last_equity_log > 3600:
                log_equity()
                last_equity_log = time.time()
                
            time.sleep(1) # Gyors iteráció, mivel WS eseményekre reagál!
            
        except KeyboardInterrupt:
            tg_send("⏹️ Bot leállítva."); break
        except Exception as e:
            log.error("Főciklus hiba: %s", e)
            tg_send(f"⚠️ Hiba: {e}"); time.sleep(10)

    save_state()
    log.info("Bot leállt.")
"""

content = re.sub(
    r"def main\(\):.*",
    main_code + "\n\nif __name__ == '__main__':\n    main()\n",
    content,
    flags=re.DOTALL
)

with open(r"c:\Users\tamas\Documents\Crypto-Trading-Bot\pro_bot_v5_pi.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Patch applied successfully!")
