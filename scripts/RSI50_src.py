#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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
_api_key = os.getenv("API_KEY"); _api_secret = os.getenv("API_KEY_SECRET")
if not _api_key or not _api_secret:
    print("❌ API_KEY 또는 API_KEY_SECRET을 .env에서 못 찾았습니다.")
    print(f"cwd={os.getcwd()}  .env={find_dotenv() or 'NOT FOUND'}"); sys.exit(1)

session = HTTP(api_key=_api_key, api_secret=_api_secret, recv_window=10000, max_retries=0)

# ---- USER PARAMS (리스트 3개) ----
SYMBOLS      = ["SOLUSDT","XRPUSDT","1000PEPEUSDT"]
RSI_PERIODS  = [7,         7,       6]
INTERVALS    = [30,      "30",   "D"]

# 길이 검사
if not (len(SYMBOLS)==len(RSI_PERIODS)==len(INTERVALS)):
    print("❌ SYMBOLS/RSI_PERIODS/INTERVALS 길이가 다릅니다."); sys.exit(1)

LEVERAGE = "7"   # 모든 심볼 동일 레버리지(문자열)
PCT      = 50    # 코인별 투자 비중(%)

# ===== RSI 50 전략 파라미터 (실시간) =====
DOORSTEP_ENTRY = 5.0    # 50± 진입 문턱
DOORSTEP_CLOSE = 20.0   # 50± 익절 ARM 문턱
CLOSE_BAND     = 4.0    # ARM 후 피크/트로프에서 되돌림 폭
REENTRY_UNTIL_RSI = 50.0  # 청산 후 50 '통과' 전 재진입 금지
COOLDOWN_BARS = 0

BYBIT_BASE = "https://api.bybit.com"

# =========================
# 심볼별 상태(dict)로 완전 분리
# =========================
position  = {s: None for s in SYMBOLS}  # 'long'/'short'/None
entry_px  = {s: None for s in SYMBOLS}
tp_price  = {s: None for s in SYMBOLS}  # 사용 안 함(형식 유지용)

# ARM/트레일링용
arm_long   = {s: False for s in SYMBOLS}
arm_short  = {s: False for s in SYMBOLS}
peak_rsi   = {s: None  for s in SYMBOLS}
trough_rsi = {s: None  for s in SYMBOLS}

# 재진입 차단
reentry_block = {s: False for s in SYMBOLS}
block_side    = {s: None  for s in SYMBOLS}  # 'above' / 'below' / None

# PnL/표시/쿨다운
max_rsi_since_ent  = {s: None for s in SYMBOLS}  # 포맷 유지용(미사용)
min_rsi_since_ent  = {s: None for s in SYMBOLS}  # 포맷 유지용(미사용)
last_closed_price1 = {s: None for s in SYMBOLS}
cooldown_bars      = {s: 0    for s in SYMBOLS}

bybit.PCT = PCT
for i in SYMBOLS:
    bybit.SYMBOLS.append(i)

# ---- MAIN ----
BASE_CASH = None

def start():
    global BASE_CASH
    BASE_CASH = bybit.get_usdt()
    print(f"🔧 기준가용(스냅샷): {BASE_CASH:.2f} USDT")
    for s in SYMBOLS:
        bybit.set_leverage(symbol=s, leverage=LEVERAGE)

def update():
    prev_rsi={s: None for s in SYMBOLS}
    while True:
        for idx, symbol in enumerate(SYMBOLS):
            try:
                rsi_period = RSI_PERIODS[idx]
                interval   = INTERVALS[idx]
                leverage   = LEVERAGE  # 동일 레버리지

                # PnL/ROE (심볼별)
                Pnl=bybit.get_PnL(symbol); ROE=bybit.get_ROE(symbol)

                # 시세/RSI (심볼별 interval/period) — 실시간
                c_prev2, c_prev1, cur_3 = bybit.get_close_price(symbol, interval=interval)
                RSI = bybit.get_RSI(symbol, interval=interval, period=rsi_period)

                # 봉 교체/쿨다운
                new_bar = (last_closed_price1[symbol] is None) or (last_closed_price1[symbol] != c_prev1)
                if new_bar:
                    last_closed_price1[symbol] = c_prev1
                    if cooldown_bars[symbol] > 0:
                        cooldown_bars[symbol] -= 1

                # ===== 재진입 차단 해제: RSI가 50을 '통과'해야 해제 =====
                if reentry_block[symbol] and RSI is not None:
                    if block_side[symbol] == 'above' and RSI <= REENTRY_UNTIL_RSI:
                        reentry_block[symbol] = False; block_side[symbol] = None
                    elif block_side[symbol] == 'below' and RSI >= REENTRY_UNTIL_RSI:
                        reentry_block[symbol] = False; block_side[symbol] = None
                    elif block_side[symbol] is None and abs(RSI - REENTRY_UNTIL_RSI) < 1e-9:
                        reentry_block[symbol] = False

                # ===== FLAT → 진입 =====
                if (position[symbol] is None) and (cooldown_bars[symbol] == 0) and (not reentry_block[symbol]) and (RSI is not None):
                    long_trigger  = 50.0 + DOORSTEP_ENTRY
                    short_trigger = 50.0 - DOORSTEP_ENTRY

                    # LONG 진입
                    if RSI >= long_trigger:
                        px, qty = bybit.entry_position(symbol=symbol, side="Buy", leverage=leverage)
                        if qty > 0 and px is not None:
                            position[symbol] = 'long'
                            entry_px[symbol]  = px
                            arm_long[symbol]  = False;  peak_rsi[symbol]  = None
                            arm_short[symbol] = False;  trough_rsi[symbol]= None
                            cooldown_bars[symbol] = COOLDOWN_BARS

                    # SHORT 진입
                    elif RSI <= short_trigger:
                        px, qty = bybit.entry_position(symbol=symbol, side="Sell", leverage=leverage)
                        if qty > 0 and px is not None:
                            position[symbol] = 'short'
                            entry_px[symbol]  = px
                            arm_short[symbol] = False;  trough_rsi[symbol]= None
                            arm_long[symbol]  = False;  peak_rsi[symbol]  = None
                            cooldown_bars[symbol] = COOLDOWN_BARS

                # ===== 보유 중 → 익절/손절 =====
                elif position[symbol] is not None and RSI is not None:
                    # LONG 포지션
                    if position[symbol] == 'long':
                        # 익절 ARM: 50 + DOORSTEP_CLOSE 이상이면 ARM
                        tp_arm_level = 50.0 + DOORSTEP_CLOSE
                        if (not arm_long[symbol]) and (RSI >= tp_arm_level):
                            arm_long[symbol] = True
                            peak_rsi[symbol] = RSI

                        # ARM 전 손절: RSI가 50 재진입하면 손절
                        if (not arm_long[symbol]) and (RSI <= 50.0):
                            bybit.close_position(symbol=symbol, side="Sell")
                            # 재진입 차단: 청산 시점의 50 기준 위치 기억
                            reentry_block[symbol] = True
                            block_side[symbol] = 'below' if RSI < 50.0 else None
                            # 포지션 종료
                            position[symbol]=None; entry_px[symbol]=None; tp_price[symbol]=None
                            arm_long[symbol]=False; peak_rsi[symbol]=None
                            cooldown_bars[symbol]=COOLDOWN_BARS
                        else:
                            # ARM 이후 트레일링 익절: peak_rsi - CLOSE_BAND 밑으로 내려오면 청산
                            if arm_long[symbol]:
                                peak_rsi[symbol] = max(peak_rsi[symbol], RSI)
                                trigger_down = peak_rsi[symbol] - CLOSE_BAND
                                if RSI <= trigger_down:
                                    bybit.close_position(symbol=symbol, side="Sell")
                                    reentry_block[symbol] = True
                                    # 청산 시점의 50 기준 위치
                                    block_side[symbol] = 'above' if RSI > 50.0 else ('below' if RSI < 50.0 else None)
                                    position[symbol]=None; entry_px[symbol]=None; tp_price[symbol]=None
                                    arm_long[symbol]=False; peak_rsi[symbol]=None
                                    cooldown_bars[symbol]=COOLDOWN_BARS

                    # SHORT 포지션
                    elif position[symbol] == 'short':
                        # 익절 ARM: 50 - DOORSTEP_CLOSE 이하이면 ARM
                        tp_arm_level = 50.0 - DOORSTEP_CLOSE
                        if (not arm_short[symbol]) and (RSI <= tp_arm_level):
                            arm_short[symbol] = True
                            trough_rsi[symbol] = RSI

                        # ARM 전 손절: RSI가 50 재진입하면 손절
                        if (not arm_short[symbol]) and (RSI >= 50.0):
                            bybit.close_position(symbol=symbol, side="Buy")
                            reentry_block[symbol] = True
                            block_side[symbol] = 'above' if RSI > 50.0 else None
                            position[symbol]=None; entry_px[symbol]=None; tp_price[symbol]=None
                            arm_short[symbol]=False; trough_rsi[symbol]=None
                            cooldown_bars[symbol]=COOLDOWN_BARS
                        else:
                            # ARM 이후 트레일링 익절: trough_rsi + CLOSE_BAND 위로 올라오면 청산
                            if arm_short[symbol]:
                                trough_rsi[symbol] = min(trough_rsi[symbol], RSI)
                                trigger_up = trough_rsi[symbol] + CLOSE_BAND
                                if RSI >= trigger_up:
                                    bybit.close_position(symbol=symbol, side="Buy")
                                    reentry_block[symbol] = True
                                    block_side[symbol] = 'above' if RSI > 50.0 else ('below' if RSI < 50.0 else None)
                                    position[symbol]=None; entry_px[symbol]=None; tp_price[symbol]=None
                                    arm_short[symbol]=False; trough_rsi[symbol]=None
                                    cooldown_bars[symbol]=COOLDOWN_BARS

                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                      f"🪙{symbol} @{interval} 💲현재가: {cur_3:.5f}$ 🚩포지션 {position.get(symbol)} "
                      f"| ❣ RSI({rsi_period})={RSI:.2f} | 💎Pnl: {Pnl:.3f} ⚜️ROE: {ROE:.2f}")
                prev_rsi[symbol]=RSI

            except Exception as e:
                print(f"[ERR] {symbol}: {type(e).__name__} {e}")
                continue

            time.sleep(5)
        time.sleep(10)

# run
start()
update()
