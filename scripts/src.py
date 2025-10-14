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
load_dotenv(find_dotenv(),override=True)
_api_key = os.getenv("API_KEY"); _api_secret = os.getenv("API_KEY_SECRET")
if not _api_key or not _api_secret:
    print("❌ API_KEY 또는 API_KEY_SECRET을 .env에서 못 찾았습니다.")
    print(f"cwd={os.getcwd()}  .env={find_dotenv() or 'NOT FOUND'}"); sys.exit(1)

session = HTTP(api_key=_api_key, api_secret=_api_secret, recv_window=10000, max_retries=0)

# ---- USER PARAMS (리스트 3개) ----
SYMBOLS      = ["SOLUSDT","XRPUSDT","1000PEPEUSDT"]   # 심볼 목록
RSI_PERIODS  = [7,         7,       6]         # 각 심볼별 RSI 기간
INTERVALS    = [30,      "30",   "D"]     # 각 심볼별 인터벌 ("1","3","15","60","240","D"...)

# 길이 검사
if not (len(SYMBOLS)==len(RSI_PERIODS)==len(INTERVALS)):
    print("❌ SYMBOLS/RSI_PERIODS/INTERVALS 길이가 다릅니다."); sys.exit(1)

LEVERAGE = "7"   # 모든 심볼 동일 레버리지(문자열)
PCT      = 50    # 코인별 투자 비중(%)
LONG_SWITCH_RSI  = 28
SHORT_SWITCH_RSI = 72
ENTRY_BAND = 4
COOLDOWN_BARS = 0
BYBIT_BASE = "https://api.bybit.com"

# =========================
# 심볼별 상태(dict)로 완전 분리
# =========================
position  = {s: None for s in SYMBOLS}  # 'long'/'short'/None
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
cooldown_bars      = {s: 0    for s in SYMBOLS}

bybit.PCT = PCT
for i in SYMBOLS:
    bybit.SYMBOLS.append(i)


# ---- MAIN ----
# 전역
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

                # 시세/RSI (심볼별 interval/period)
                c_prev2,c_prev1,cur_3=bybit.get_close_price(symbol, interval=interval)
                RSI=bybit.get_RSI(symbol, interval=interval, period=rsi_period)

                # ✱ 스위치 먼저 찍기(arm) — 포지션과 무관하게 기록 (임계값 변경 없음)
                if RSI <= LONG_SWITCH_RSI:
                    armed_long_switch[symbol] = True
                if RSI >= SHORT_SWITCH_RSI:
                    armed_short_switch[symbol] = True

                # 봉 교체/쿨다운
                new_bar=(last_closed_price1[symbol] is None) or (last_closed_price1[symbol]!=c_prev1)
                if new_bar:
                    last_closed_price1[symbol]=c_prev1
                    if cooldown_bars[symbol]>0: cooldown_bars[symbol]-=1

                # ===== 레벨 갱신 (심볼별 RSI) — 네 원본 그대로 =====
                if RSI>=84: last_peak_level[symbol]=84
                elif RSI>=80:
                    if last_peak_level[symbol] is None or last_peak_level[symbol]<80: last_peak_level[symbol]=80
                elif RSI>=75:
                    if last_peak_level[symbol] is None or last_peak_level[symbol]<75: last_peak_level[symbol]=75
                elif RSI>=72:
                    if last_peak_level[symbol] is None or last_peak_level[symbol]<72: last_peak_level[symbol]=72
                elif RSI>=68:
                    if last_peak_level[symbol] is None or last_peak_level[symbol]<68: last_peak_level[symbol]=68
                
                if RSI<=20: last_trough_level[symbol]=20
                elif RSI<=25:
                    if (last_trough_level[symbol] is None) or (last_trough_level[symbol]>25): last_trough_level[symbol]=25
                elif RSI<=27:
                    if (last_trough_level[symbol] is None) or (last_trough_level[symbol]>27): last_trough_level[symbol]=27
                elif RSI<=30:
                    if (last_trough_level[symbol] is None) or (last_trough_level[symbol]>30): last_trough_level[symbol]=30
                elif RSI<=32:
                    if (last_trough_level[symbol] is None) or (last_trough_level[symbol]>34): last_trough_level[symbol]=34
                
                # ===== 무포지션 → 진입 (스위치 먼저 찍고 ±3 되돌림 구간만 허용) =====
                if position[symbol] is None and cooldown_bars[symbol]==0:
                    # 숏
                    if last_peak_level[symbol] is not None and armed_short_switch[symbol]:
                        short_trigger=last_peak_level[symbol]-3
                        if (RSI <= short_trigger) and (RSI >= short_trigger - ENTRY_BAND):
                            px,qty=bybit.entry_position(symbol=symbol, side="Sell", leverage=leverage)
                            if qty>0 and px is not None:
                                position[symbol]='short'; entry_px[symbol]=px; tp_price[symbol]=None
                                cooldown_bars[symbol]=COOLDOWN_BARS; pending_floor_lvl[symbol]=None
                                last_peak_level[symbol]=None
                                armed_short_switch[symbol]=False          # 사용한 스위치 소모
                                max_rsi_since_ent[symbol]=None
                                armed_long_switch[symbol]=(RSI<=LONG_SWITCH_RSI)
                                min_rsi_since_ent[symbol]=RSI; prev_rsi[symbol]=RSI; continue
                    # 롱
                    if position[symbol] is None and last_trough_level[symbol] is not None and cooldown_bars[symbol]==0 and armed_long_switch[symbol]:
                        long_trigger=last_trough_level[symbol]+3
                        if (RSI >= long_trigger) and (RSI <= long_trigger + ENTRY_BAND):
                            px,qty=bybit.entry_position(symbol=symbol, side="Buy", leverage=leverage)
                            if qty>0 and px is not None:
                                position[symbol]='long'; entry_px[symbol]=px; tp_price[symbol]=None
                                cooldown_bars[symbol]=COOLDOWN_BARS; pending_ceil_lvl[symbol]=None
                                last_trough_level[symbol]=None
                                armed_long_switch[symbol]=False           # 사용한 스위치 소모
                                min_rsi_since_ent[symbol]=None
                                armed_short_switch[symbol]=(RSI>=SHORT_SWITCH_RSI)
                                max_rsi_since_ent[symbol]=RSI; prev_rsi[symbol]=RSI; continue

                # ===== 숏 보유 → 바닥 +3 반등 청산 (+조건부 롱 전환) =====
                elif position[symbol]=='short':
                    if RSI<=30: pending_floor_lvl[symbol]=30 if pending_floor_lvl[symbol] is None else min(pending_floor_lvl[symbol],30)
                    if RSI<=25: pending_floor_lvl[symbol]=25 if pending_floor_lvl[symbol] is None else min(pending_floor_lvl[symbol],25)
                    if RSI<=20: pending_floor_lvl[symbol]=20 if pending_floor_lvl[symbol] is None else min(pending_floor_lvl[symbol],20)
                    if RSI<=15: pending_floor_lvl[symbol]=15 if pending_floor_lvl[symbol] is None else min(pending_floor_lvl[symbol],15)

                    if (min_rsi_since_ent[symbol] is None) or (RSI<min_rsi_since_ent[symbol]): min_rsi_since_ent[symbol]=RSI
                    if RSI<=LONG_SWITCH_RSI: armed_long_switch[symbol]=True

                    if pending_floor_lvl[symbol] is not None:
                        trigger_up=pending_floor_lvl[symbol]+3
                        if RSI>=trigger_up and ROE>0.1:
                            bybit.close_position(symbol=symbol, side="Buy")
                            if armed_long_switch[symbol] and RSI>=LONG_SWITCH_RSI:
                                px,qty=bybit.entry_position(symbol=symbol, side="Buy", leverage=leverage)
                                if qty>0 and px is not None:
                                    position[symbol]='long'; entry_px[symbol]=px; tp_price[symbol]=None
                                    cooldown_bars[symbol]=COOLDOWN_BARS; pending_floor_lvl[symbol]=None
                                    armed_long_switch[symbol]=False; min_rsi_since_ent[symbol]=None
                                    armed_short_switch[symbol]=(RSI>=SHORT_SWITCH_RSI)
                                    max_rsi_since_ent[symbol]=RSI; last_trough_level[symbol]=None; prev_rsi[symbol]=RSI; continue
                            position[symbol]=None; entry_px[symbol]=None; tp_price[symbol]=None
                            cooldown_bars[symbol]=COOLDOWN_BARS; pending_floor_lvl[symbol]=None
                            armed_long_switch[symbol]=False; min_rsi_since_ent[symbol]=None; last_trough_level[symbol]=None

                # ===== 롱 보유 → 천장 -3 하락 청산 (+조건부 숏 전환) =====
                elif position[symbol]=='long':
                    if RSI>=70: pending_ceil_lvl[symbol]=70 if pending_ceil_lvl[symbol] is None else max(pending_ceil_lvl[symbol],70)
                    if RSI>=75: pending_ceil_lvl[symbol]=75 if pending_ceil_lvl[symbol] is None else max(pending_ceil_lvl[symbol],75)
                    if RSI>=80: pending_ceil_lvl[symbol]=80 if pending_ceil_lvl[symbol] is None else max(pending_ceil_lvl[symbol],80)
                    if RSI>=85: pending_ceil_lvl[symbol]=85 if pending_ceil_lvl[symbol] is None else max(pending_ceil_lvl[symbol],85)

                    if (max_rsi_since_ent[symbol] is None) or (RSI>max_rsi_since_ent[symbol]): max_rsi_since_ent[symbol]=RSI
                    if RSI>=SHORT_SWITCH_RSI: armed_short_switch[symbol]=True

                    if pending_ceil_lvl[symbol] is not None:
                        trigger_down=pending_ceil_lvl[symbol]-3
                        if RSI<=trigger_down and ROE>0.1:
                            bybit.close_position(symbol=symbol, side="Sell")
                            if armed_short_switch[symbol] and RSI<SHORT_SWITCH_RSI:
                                px,qty=bybit.entry_position(symbol=symbol, side="Sell", leverage=leverage)
                                if qty>0 and px is not None:
                                    position[symbol]='short'; entry_px[symbol]=px; tp_price[symbol]=None
                                    cooldown_bars[symbol]=COOLDOWN_BARS; pending_ceil_lvl[symbol]=None
                                    armed_short_switch[symbol]=False; max_rsi_since_ent[symbol]=None
                                    armed_long_switch[symbol]=(RSI<=LONG_SWITCH_RSI)
                                    min_rsi_since_ent[symbol]=RSI; last_peak_level[symbol]=None; prev_rsi[symbol]=RSI; continue
                            position[symbol]=None; entry_px[symbol]=None; tp_price[symbol]=None
                            cooldown_bars[symbol]=COOLDOWN_BARS; pending_ceil_lvl[symbol]=None
                            armed_short_switch[symbol]=False; max_rsi_since_ent[symbol]=None; last_peak_level[symbol]=None

                # 출력 (RSI(n) + interval 표시)
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
