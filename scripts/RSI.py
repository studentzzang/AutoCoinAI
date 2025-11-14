from dotenv import load_dotenv, find_dotenv
from pybit.unified_trading import HTTP
import os, sys
from datetime import datetime
import time
import bybit

load_dotenv(find_dotenv(), override=True)
_api_key = os.getenv("API_KEY")
_api_secret = os.getenv("API_KEY_SECRET")
if not _api_key or not _api_secret:
    print("❌ API_KEY 또는 API_KEY_SECRET을 .env에서 못 찾았습니다.")
    sys.exit(1)

session = HTTP(api_key=_api_key, api_secret=_api_secret, recv_window=10000, max_retries=0)

SYMBOLS      = ["PUNPFUNUSDT"]
RSI_PERIODS  = [9]
INTERVALS    = ["1"]

LONG_SWITCH_RSI  = [28] 
SHORT_SWITCH_RSI = [72] 

LEVERAGE      = "5"
PCT           = 40
COOLDOWN_BARS = 0
DOORSTEP      = 3.0  

TP_ROE  = [10]  
SL_ROE  = [15]  
TP_MODE = [1]   

position      = {s: None for s in SYMBOLS}
entry_px      = {s: None for s in SYMBOLS}
init_margin   = {s: None for s in SYMBOLS}
qty           = {s: None for s in SYMBOLS}

last_peak_level    = {s: None for s in SYMBOLS}
last_trough_level  = {s: None for s in SYMBOLS}
armed_short_switch = {s: False for s in SYMBOLS}
armed_long_switch  = {s: False for s in SYMBOLS}

last_closed_price1 = {s: None for s in SYMBOLS}
cooldown_bars      = {s: 0   for s in SYMBOLS}

bybit.PCT = PCT
for s in SYMBOLS:
    bybit.SYMBOLS.append(s)

BASE_CASH = None


def start():
    global BASE_CASH
    BASE_CASH = bybit.get_usdt()
    print(f"🔧 보유금액: {BASE_CASH:.2f} USDT")
    for s in SYMBOLS:
        bybit.set_leverage(symbol=s, leverage=LEVERAGE)


def reset_switch_after_close(symbol, closed_side):
    if closed_side == "long":
        armed_long_switch[symbol] = True
        last_trough_level[symbol] = None
    elif closed_side == "short":
        armed_short_switch[symbol] = True
        last_peak_level[symbol] = None


def update():
    while True:
        for idx, symbol in enumerate(SYMBOLS):
            try:
                tp_roe  = TP_ROE[idx]
                sl_roe  = SL_ROE[idx]
                tp_mode = TP_MODE[idx]

                rsi_period = RSI_PERIODS[idx]
                interval   = INTERVALS[idx]
                long_rsi   = LONG_SWITCH_RSI[idx]
                short_rsi  = SHORT_SWITCH_RSI[idx]

                Pnl = bybit.get_PnL(symbol)
                ROE = bybit.get_ROE(symbol)

                c_prev2, c_prev1, cur_3 = bybit.get_close_price(symbol, interval=interval)
                RSI = bybit.get_RSI(symbol, interval=interval, period=rsi_period)

                new_bar = (last_closed_price1[symbol] is None) or (last_closed_price1[symbol] != c_prev1)
                if new_bar:
                    last_closed_price1[symbol] = c_prev1
                    if cooldown_bars[symbol] > 0:
                        cooldown_bars[symbol] -= 1

                if RSI <= long_rsi:
                    if not armed_long_switch[symbol]:
                        armed_long_switch[symbol] = True
                        last_trough_level[symbol] = RSI
                    else:
                        if last_trough_level[symbol] is None or RSI < last_trough_level[symbol]:
                            last_trough_level[symbol] = RSI

                if RSI >= short_rsi:
                    if not armed_short_switch[symbol]:
                        armed_short_switch[symbol] = True
                        last_peak_level[symbol] = RSI
                    else:
                        if last_peak_level[symbol] is None or RSI > last_peak_level[symbol]:
                            last_peak_level[symbol] = RSI

                if position[symbol] is None and cooldown_bars[symbol] == 0:

                    if armed_short_switch[symbol] and last_peak_level[symbol] is not None:
                        short_trigger = last_peak_level[symbol] - DOORSTEP
                        if RSI <= short_trigger:
                            px, q = bybit.entry_position(symbol, "Sell", LEVERAGE)
                            if q > 0 and px is not None:
                                position[symbol] = "short"
                                entry_px[symbol] = px
                                qty[symbol] = q
                                init_margin[symbol] = (px * q) / float(LEVERAGE)
                                armed_short_switch[symbol] = False
                                last_peak_level[symbol] = None
                                cooldown_bars[symbol] = COOLDOWN_BARS

                    if position[symbol] is None and cooldown_bars[symbol] == 0:
                        if armed_long_switch[symbol] and last_trough_level[symbol] is not None:
                            long_trigger = last_trough_level[symbol] + DOORSTEP
                            if RSI >= long_trigger:
                                px, q = bybit.entry_position(symbol, "Buy", LEVERAGE)
                                if q > 0 and px is not None:
                                    position[symbol] = "long"
                                    entry_px[symbol] = px
                                    qty[symbol] = q
                                    init_margin[symbol] = (px * q) / float(LEVERAGE)
                                    armed_long_switch[symbol] = False
                                    last_trough_level[symbol] = None
                                    cooldown_bars[symbol] = COOLDOWN_BARS

                if position[symbol] == "short":
                    unreal = (entry_px[symbol] - cur_3) * qty[symbol]
                    roe    = (unreal / init_margin[symbol]) * 100
                    closed = False

                    if tp_mode == 1:
                        if roe <= -sl_roe:
                            bybit.close_position(symbol, "Buy")
                            closed = True
                        elif roe >= tp_roe:
                            if RSI <= long_rsi:
                                if (long_rsi - DOORSTEP) <= RSI <= (long_rsi + DOORSTEP):
                                    bybit.close_position(symbol, "Buy")
                                    closed = True
                            else:
                                bybit.close_position(symbol, "Buy")
                                closed = True
                    else:
                        if roe >= tp_roe or roe <= -sl_roe:
                            bybit.close_position(symbol, "Buy")
                            closed = True

                    if closed:
                        position[symbol] = None
                        cooldown_bars[symbol] = COOLDOWN_BARS
                        reset_switch_after_close(symbol, closed_side="short")

                elif position[symbol] == "long":
                    unreal = (cur_3 - entry_px[symbol]) * qty[symbol]
                    roe    = (unreal / init_margin[symbol]) * 100
                    closed = False

                    if tp_mode == 1:
                        if roe <= -sl_roe:
                            bybit.close_position(symbol, "Sell")
                            closed = True
                        elif roe >= tp_roe:
                            if RSI >= short_rsi:
                                if (short_rsi - DOORSTEP) <= RSI <= (short_rsi + DOORSTEP):
                                    bybit.close_position(symbol, "Sell")
                                    closed = True
                            else:
                                bybit.close_position(symbol, "Sell")
                                closed = True
                    else:
                        if roe >= tp_roe or roe <= -sl_roe:
                            bybit.close_position(symbol, "Sell")
                            closed = True

                    if closed:
                        position[symbol] = None
                        cooldown_bars[symbol] = COOLDOWN_BARS
                        reset_switch_after_close(symbol, closed_side="long")

                print(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"🪙 {symbol} 🕧 {interval} | 🚩포지션:{position[symbol]} "
                    f"| RSI:{RSI:.2f} |💸 PnL:{Pnl:.3f} |💎 ROE:{ROE:.2f} "
                )

            except Exception as e:
                print(f"[ERROR] {symbol}: {type(e).__name__} {e}")
                continue

            time.sleep(5)
        time.sleep(10)


start()
update()
