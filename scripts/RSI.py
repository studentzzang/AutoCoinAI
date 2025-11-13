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

# =====================================
#   USER SETTINGS
# =====================================
SYMBOLS      = ["PUNPFUNUSDT"]
RSI_PERIODS  = [9]
INTERVALS    = ["1"]

LONG_SWITCH_RSI  = [28]   # 롱 스위치 RSI (과매도 경계)
SHORT_SWITCH_RSI = [72]   # 숏 스위치 RSI (과매수 경계)

LEVERAGE      = "5"
PCT           = 40
COOLDOWN_BARS = 0
DOORSTEP      = 3.0   # DOORSTEP

# ===== TP/SL & MODE (심볼별) =====
TP_ROE  = [10]   # 심볼별 TP ROE(%)
SL_ROE  = [15]   # 심볼별 SL ROE(%)
TP_MODE = [1]     # 1: DOORSTEP TP, 2: ROE TP/SL만
# =================================

position      = {s: None for s in SYMBOLS}
entry_px      = {s: None for s in SYMBOLS}
init_margin   = {s: None for s in SYMBOLS}
qty           = {s: None for s in SYMBOLS}

# 스위치 이후 extremum 기록용
last_peak_level    = {s: None for s in SYMBOLS}   # 숏 후보 extremum (최고 RSI)
last_trough_level  = {s: None for s in SYMBOLS}   # 롱 후보 extremum (최저 RSI)
armed_short_switch = {s: False for s in SYMBOLS}  # SHORT 스위치 ON/OFF
armed_long_switch  = {s: False for s in SYMBOLS}  # LONG 스위치 ON/OFF

last_closed_price1 = {s: None for s in SYMBOLS}
cooldown_bars      = {s: 0   for s in SYMBOLS}

bybit.PCT = PCT
for s in SYMBOLS:
    bybit.SYMBOLS.append(s)

BASE_CASH = None


# =====================================
def start():
    global BASE_CASH
    BASE_CASH = bybit.get_usdt()
    print(f"🔧 보유금액: {BASE_CASH:.2f} USDT")
    for s in SYMBOLS:
        bybit.set_leverage(symbol=s, leverage=LEVERAGE)


# =====================================
def update():
    while True:
        for idx, symbol in enumerate(SYMBOLS):
            try:
                # ---- 심볼별 설정값 ----
                tp_roe  = TP_ROE[idx]
                sl_roe  = SL_ROE[idx]
                tp_mode = TP_MODE[idx]

                rsi_period = RSI_PERIODS[idx]
                interval   = INTERVALS[idx]
                long_rsi   = LONG_SWITCH_RSI[idx]
                short_rsi  = SHORT_SWITCH_RSI[idx]

                # ---- PnL / ROE (표시용) ----
                Pnl = bybit.get_PnL(symbol)
                ROE = bybit.get_ROE(symbol)

                # ---- 시세 / RSI ----
                c_prev2, c_prev1, cur_3 = bybit.get_close_price(symbol, interval=interval)
                RSI = bybit.get_RSI(symbol, interval=interval, period=rsi_period)

                # ===== 봉 교체 처리 =====
                new_bar = (last_closed_price1[symbol] is None) or (last_closed_price1[symbol] != c_prev1)
                if new_bar:
                    last_closed_price1[symbol] = c_prev1
                    if cooldown_bars[symbol] > 0:
                        cooldown_bars[symbol] -= 1

                # =====================================
                #   스위치 ON 조건 (과매수/과매도 돌파)
                # =====================================
                # 롱 스위치: RSI가 long_rsi 이하로 내려가면
                if RSI <= long_rsi:
                    if not armed_long_switch[symbol]:
                        armed_long_switch[symbol] = True
                        last_trough_level[symbol] = RSI  # 새 스위치 시작점에서 초기화
                    else:
                        # 스위치 ON 상태에서는 최저값 갱신
                        if last_trough_level[symbol] is None or RSI < last_trough_level[symbol]:
                            last_trough_level[symbol] = RSI

                # 숏 스위치: RSI가 short_rsi 이상으로 올라가면
                if RSI >= short_rsi:
                    if not armed_short_switch[symbol]:
                        armed_short_switch[symbol] = True
                        last_peak_level[symbol] = RSI   # 새 스위치 시작점에서 초기화
                    else:
                        # 스위치 ON 상태에서는 최고값 갱신
                        if last_peak_level[symbol] is None or RSI > last_peak_level[symbol]:
                            last_peak_level[symbol] = RSI

                # =====================================
                #   진입 로직 (DOORSTEP 기반)
                # =====================================
                if position[symbol] is None and cooldown_bars[symbol] == 0:

                    # ----- 숏 진입 (과매수 → peak → DOORSTEP 복구 지점) -----
                    if armed_short_switch[symbol] and last_peak_level[symbol] is not None:
                        short_trigger = last_peak_level[symbol] - DOORSTEP  # peak - DOORSTEP
                        if RSI <= short_trigger:
                            px, q = bybit.entry_position(symbol=symbol, side="Sell", leverage=LEVERAGE)
                            if q > 0 and px is not None:
                                position[symbol]    = "short"
                                entry_px[symbol]    = px
                                qty[symbol]         = q
                                init_margin[symbol] = (px * q) / float(LEVERAGE)
                                cooldown_bars[symbol] = COOLDOWN_BARS
                                # 숏 스위치 리셋
                                armed_short_switch[symbol] = False
                                last_peak_level[symbol]    = None

                    # ----- 롱 진입 (과매도 → trough → DOORSTEP 복구 지점) -----
                    if position[symbol] is None and cooldown_bars[symbol] == 0:
                        if armed_long_switch[symbol] and last_trough_level[symbol] is not None:
                            long_trigger = last_trough_level[symbol] + DOORSTEP  # trough + DOORSTEP
                            if RSI >= long_trigger:
                                px, q = bybit.entry_position(symbol=symbol, side="Buy", leverage=LEVERAGE)
                                if q > 0 and px is not None:
                                    position[symbol]    = "long"
                                    entry_px[symbol]    = px
                                    qty[symbol]         = q
                                    init_margin[symbol] = (px * q) / float(LEVERAGE)
                                    cooldown_bars[symbol] = COOLDOWN_BARS
                                    # 롱 스위치 리셋
                                    armed_long_switch[symbol] = False
                                    last_trough_level[symbol] = None

                # =====================================
                #   청산 로직 (TP_MODE 적용)
                # =====================================
                if position[symbol] == "short":
                    # 숏: 진입가 - 현재가
                    unreal = (entry_px[symbol] - cur_3) * qty[symbol]
                    roe    = (unreal / init_margin[symbol]) * 100

                    if tp_mode == 1:
                        # SL : ROE 기준 항상
                        if roe <= -sl_roe:
                            bybit.close_position(symbol=symbol, side="Buy")
                            position[symbol] = None
                            cooldown_bars[symbol] = COOLDOWN_BARS
                        # TP 조건: ROE가 TP 이상이고, 반대 과상태일 때만 DOORSTEP 사용
                        elif roe >= tp_roe:
                            if RSI <= long_rsi:
                                # 반대 과상태(과매도)일 때 DOORSTEP 밴드 안에서만 청산
                                if (long_rsi - DOORSTEP) <= RSI <= (long_rsi + DOORSTEP):
                                    bybit.close_position(symbol=symbol, side="Buy")
                                    position[symbol] = None
                                    cooldown_bars[symbol] = COOLDOWN_BARS
                            else:
                                # 반대 과상태가 아니면 MODE2처럼 TP 즉시 청산
                                bybit.close_position(symbol=symbol, side="Buy")
                                position[symbol] = None
                                cooldown_bars[symbol] = COOLDOWN_BARS

                    elif tp_mode == 2:
                        # ROE TP/SL만 의존
                        if roe >= tp_roe or roe <= -sl_roe:
                            bybit.close_position(symbol=symbol, side="Buy")
                            position[symbol] = None
                            cooldown_bars[symbol] = COOLDOWN_BARS

                elif position[symbol] == "long":
                    # 롱: 현재가 - 진입가
                    unreal = (cur_3 - entry_px[symbol]) * qty[symbol]
                    roe    = (unreal / init_margin[symbol]) * 100

                    if tp_mode == 1:
                        # SL : ROE 기준 항상
                        if roe <= -sl_roe:
                            bybit.close_position(symbol=symbol, side="Sell")
                            position[symbol] = None
                            cooldown_bars[symbol] = COOLDOWN_BARS
                        # TP 조건: ROE가 TP 이상이고, 반대 과상태일 때만 DOORSTEP 사용
                        elif roe >= tp_roe:
                            if RSI >= short_rsi:
                                # 반대 과상태(과매수)일 때 DOORSTEP 밴드 안에서만 청산
                                if (short_rsi - DOORSTEP) <= RSI <= (short_rsi + DOORSTEP):
                                    bybit.close_position(symbol=symbol, side="Sell")
                                    position[symbol] = None
                                    cooldown_bars[symbol] = COOLDOWN_BARS
                            else:
                                # 반대 과상태가 아니면 MODE2처럼 TP 즉시 청산
                                bybit.close_position(symbol=symbol, side="Sell")
                                position[symbol] = None
                                cooldown_bars[symbol] = COOLDOWN_BARS

                    elif tp_mode == 2:
                        # ROE TP/SL만 의존
                        if roe >= tp_roe or roe <= -sl_roe:
                            bybit.close_position(symbol=symbol, side="Sell")
                            position[symbol] = None
                            cooldown_bars[symbol] = COOLDOWN_BARS

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
