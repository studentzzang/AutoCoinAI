from dotenv import load_dotenv, find_dotenv
from pybit.unified_trading import HTTP
import os, sys
import pandas as pd
from datetime import datetime
import time
from math import floor, isclose
import hmac, hashlib, requests, json
from decimal import Decimal
import bybit

# ------ GET API KEY -----------------
load_dotenv(find_dotenv(), override=True)
_api_key = os.getenv("API_KEY")
_api_secret = os.getenv("API_KEY_SECRET")
if not _api_key or not _api_secret:
    print("❌ API_KEY 또는 API_KEY_SECRET을 .env에서 못 찾았습니다.")
    print(f"cwd={os.getcwd()}  .env={find_dotenv() or 'NOT FOUND'}")
    sys.exit(1)

session = HTTP(api_key=_api_key, api_secret=_api_secret, recv_window=10000, max_retries=0)

# ---- USER PARAMS ----
SYMBOLS      = ["FARTCOINUSDT","PUNPFUNUSDT"]
RSI_PERIODS  = [7,7]
INTERVALS    = ["30","30"]

#  코인별 진입조건 배열로 지정
LONG_SWITCH_RSI  = [28,28]  # 각 코인별 롱 진입 RSI
SHORT_SWITCH_RSI = [72,72]  # 각 코인별 숏 진입 RSI

LEVERAGE = "5"
PCT      = 40
ENTRY_BAND = 4
COOLDOWN_BARS = 0
BYBIT_BASE = "https://api.bybit.com"

# =========================
# 심볼별 상태
# =========================
position  = {s: None for s in SYMBOLS}
entry_px  = {s: None for s in SYMBOLS}
tp_price  = {s: None for s in SYMBOLS}
last_peak_level    = {s: None for s in SYMBOLS}
last_trough_level  = {s: None for s in SYMBOLS}
pending_floor_lvl  = {s: None for s in SYMBOLS}
pending_ceil_lvl   = {s: None for s in SYMBOLS}
armed_short_switch = {s: False for s in SYMBOLS}
armed_long_switch  = {s: False for s in SYMBOLS}
max_rsi_since_ent  = {s: None for s in SYMBOLS}
min_rsi_since_ent  = {s: None for s in SYMBOLS}
last_closed_price1 = {s: None for s in SYMBOLS}
cooldown_bars      = {s: 0 for s in SYMBOLS}

bybit.PCT = PCT
for i in SYMBOLS:
    bybit.SYMBOLS.append(i)

# ---- MAIN ----
BASE_CASH = None

def start():
    global BASE_CASH
    BASE_CASH = bybit.get_usdt()
    print(f"🔧 보유($): {BASE_CASH:.2f} USDT")
    for s in SYMBOLS:
        bybit.set_leverage(symbol=s, leverage=LEVERAGE)

def update():
    prev_rsi = {s: None for s in SYMBOLS}
    while True:
        for idx, symbol in enumerate(SYMBOLS):
            try:
                rsi_period = RSI_PERIODS[idx]
                interval   = INTERVALS[idx]
                long_rsi   = LONG_SWITCH_RSI[idx]
                short_rsi  = SHORT_SWITCH_RSI[idx]
                leverage   = LEVERAGE

                # PnL / ROE
                Pnl = bybit.get_PnL(symbol)
                ROE = bybit.get_ROE(symbol)

                # 시세 / RSI
                c_prev2, c_prev1, cur_3 = bybit.get_close_price(symbol, interval=interval)
                RSI = bybit.get_RSI(symbol, interval=interval, period=rsi_period)

                # ===== 스위치 감지 =====
                if RSI <= long_rsi:
                    armed_long_switch[symbol] = True
                if RSI >= short_rsi:
                    armed_short_switch[symbol] = True

                # ===== 봉 교체 / 쿨다운 =====
                new_bar = (last_closed_price1[symbol] is None) or (last_closed_price1[symbol] != c_prev1)
                if new_bar:
                    last_closed_price1[symbol] = c_prev1
                    if cooldown_bars[symbol] > 0:
                        cooldown_bars[symbol] -= 1

                # ===== 레벨 갱신 =====
                if RSI >= 84: last_peak_level[symbol] = 84
                elif RSI >= 80:
                    if last_peak_level[symbol] is None or last_peak_level[symbol] < 80: last_peak_level[symbol] = 80
                elif RSI >= 75:
                    if last_peak_level[symbol] is None or last_peak_level[symbol] < 75: last_peak_level[symbol] = 75
                elif RSI >= 70:
                    if last_peak_level[symbol] is None or last_peak_level[symbol] < 70: last_peak_level[symbol] = 70

                if RSI <= 20: last_trough_level[symbol] = 20
                elif RSI <= 25:
                    if last_trough_level[symbol] is None or last_trough_level[symbol] > 25: last_trough_level[symbol] = 25
                elif RSI <= 30:
                    if last_trough_level[symbol] is None or last_trough_level[symbol] > 30: last_trough_level[symbol] = 30
                elif RSI <= 35:
                    if last_trough_level[symbol] is None or last_trough_level[symbol] > 35: last_trough_level[symbol] = 35

                # ===== 무포지션 → 진입 =====
                if position[symbol] is None and cooldown_bars[symbol] == 0:
                    # 숏 진입
                    if last_peak_level[symbol] is not None and armed_short_switch[symbol]:
                        short_trigger = last_peak_level[symbol] - 3
                        if (RSI <= short_trigger) and (RSI >= short_trigger - ENTRY_BAND):
                            px, qty = bybit.entry_position(symbol=symbol, side="Sell", leverage=leverage)
                            if qty > 0 and px is not None:
                                position[symbol] = 'short'
                                entry_px[symbol] = px
                                cooldown_bars[symbol] = COOLDOWN_BARS
                                last_peak_level[symbol] = None
                                armed_short_switch[symbol] = False
                                max_rsi_since_ent[symbol] = None
                                armed_long_switch[symbol] = (RSI <= long_rsi)
                                min_rsi_since_ent[symbol] = RSI
                                continue

                    # 롱 진입
                    if position[symbol] is None and last_trough_level[symbol] is not None and cooldown_bars[symbol] == 0 and armed_long_switch[symbol]:
                        long_trigger = last_trough_level[symbol] + 3
                        if (RSI >= long_trigger) and (RSI <= long_trigger + ENTRY_BAND):
                            px, qty = bybit.entry_position(symbol=symbol, side="Buy", leverage=leverage)
                            if qty > 0 and px is not None:
                                position[symbol] = 'long'
                                entry_px[symbol] = px
                                cooldown_bars[symbol] = COOLDOWN_BARS
                                last_trough_level[symbol] = None
                                armed_long_switch[symbol] = False
                                min_rsi_since_ent[symbol] = None
                                armed_short_switch[symbol] = (RSI >= short_rsi)
                                max_rsi_since_ent[symbol] = RSI
                                continue

                # ===== 숏 보유 → 바닥 반등 청산 =====
                elif position[symbol] == 'short':
                    if RSI <= long_rsi + 2:
                        bybit.close_position(symbol=symbol, side="Buy")
                        position[symbol] = None
                        armed_long_switch[symbol] = False
                        last_trough_level[symbol] = None
                        cooldown_bars[symbol] = COOLDOWN_BARS

                # ===== 롱 보유 → 천장 하락 청산 =====
                elif position[symbol] == 'long':
                    if RSI >= short_rsi - 2:
                        bybit.close_position(symbol=symbol, side="Sell")
                        position[symbol] = None
                        armed_short_switch[symbol] = False
                        last_peak_level[symbol] = None
                        cooldown_bars[symbol] = COOLDOWN_BARS

                # ===== 출력 =====
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                      f"🪙{symbol} @{interval} 💲현재가: {cur_3:.5f}$ 🚩포지션 {position.get(symbol)} "
                      f"| ❣ RSI({rsi_period})={RSI:.2f} | 💎Pnl: {Pnl:.3f} ⚜️ROE: {ROE:.2f}")

            except Exception as e:
                print(f"[ERR] {symbol}: {type(e).__name__} {e}")
                continue

            time.sleep(5)
        time.sleep(10)

# run
start()
update()
