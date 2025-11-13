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
SYMBOLS      = ["PUMPFUNUSDT"]
RSI_PERIODS  = [9]
INTERVALS    = ["1"]

LONG_SWITCH_RSI  = [28]   # 롱 스위치 기준
SHORT_SWITCH_RSI = [72]   # 숏 스위치 기준

LEVERAGE      = "5"
PCT           = 40
COOLDOWN_BARS = 0
DOORSTEP      = 3

# ===== TP/SL & MODE (심볼별) =====
TP_ROE  = [10]  
SL_ROE  = [15]   
TP_MODE = [1]     
# =================================

position      = {s: None for s in SYMBOLS}
entry_px      = {s: None for s in SYMBOLS}
init_margin   = {s: None for s in SYMBOLS}
qty           = {s: None for s in SYMBOLS}

last_peak_level    = {s: None for s in SYMBOLS}
last_trough_level  = {s: None for s in SYMBOLS}
armed_short_switch = {s: False for s in SYMBOLS}
armed_long_switch  = {s: False for s in SYMBOLS}

max_rsi_since_ent  = {s: None for s in SYMBOLS}
min_rsi_since_ent  = {s: None for s in SYMBOLS}

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

                # ---- PnL / ROE ----
                Pnl = bybit.get_PnL(symbol)
                ROE = bybit.get_ROE(symbol)

                # ---- 시세 / RSI ----
                c_prev2, c_prev1, cur_3 = bybit.get_close_price(symbol, interval=interval)
                RSI = bybit.get_RSI(symbol, interval=interval, period=rsi_period)

                # ===== 스위치 감지 =====
                if RSI <= long_rsi:
                    armed_long_switch[symbol] = True
                if RSI >= short_rsi:
                    armed_short_switch[symbol] = True

                # ===== 봉 교체 처리 =====
                new_bar = (last_closed_price1[symbol] is None) or (last_closed_price1[symbol] != c_prev1)
                if new_bar:
                    last_closed_price1[symbol] = c_prev1
                    if cooldown_bars[symbol] > 0:
                        cooldown_bars[symbol] -= 1

                # ===== peak 갱신 =====
                if RSI >= 84:
                    last_peak_level[symbol] = 84
                elif RSI >= 80:
                    if last_peak_level[symbol] is None or last_peak_level[symbol] < 80:
                        last_peak_level[symbol] = 80
                elif RSI >= 75:
                    if last_peak_level[symbol] is None or last_peak_level[symbol] < 75:
                        last_peak_level[symbol] = 75
                elif RSI >= 70:
                    if last_peak_level[symbol] is None or last_peak_level[symbol] < 70:
                        last_peak_level[symbol] = 70

                # ===== trough 갱신 =====
                if RSI <= 20:
                    last_trough_level[symbol] = 20
                elif RSI <= 25:
                    if last_trough_level[symbol] is None or last_trough_level[symbol] > 25:
                        last_trough_level[symbol] = 25
                elif RSI <= 30:
                    if last_trough_level[symbol] is None or last_trough_level[symbol] > 30:
                        last_trough_level[symbol] = 30
                elif RSI <= 35:
                    if last_trough_level[symbol] is None or last_trough_level[symbol] > 35:
                        last_trough_level[symbol] = 35

                # ========================
                #   ⚡ 진입 로직 (그대로)
                # ========================
                if position[symbol] is None and cooldown_bars[symbol] == 0:

                    # ---- 숏 진입 ----
                    if last_peak_level[symbol] is not None and armed_short_switch[symbol]:
                        peak = last_peak_level[symbol]
                        if (peak - DOORSTEP) <= RSI <= (peak + DOORSTEP):
                            px, q = bybit.entry_position(symbol=symbol, side="Sell", leverage=LEVERAGE)
                            if q > 0 and px is not None:
                                position[symbol]    = "short"
                                entry_px[symbol]    = px
                                qty[symbol]         = q
                                init_margin[symbol] = (px * q) / float(LEVERAGE)
                                cooldown_bars[symbol] = COOLDOWN_BARS
                                last_peak_level[symbol] = None
                                armed_short_switch[symbol] = False
                                continue

                    # ---- 롱 진입 ----
                    if last_trough_level[symbol] is not None and armed_long_switch[symbol]:
                        trough = last_trough_level[symbol]
                        if (trough - DOORSTEP) <= RSI <= (trough + DOORSTEP):
                            px, q = bybit.entry_position(symbol=symbol, side="Buy", leverage=LEVERAGE)
                            if q > 0 and px is not None:
                                position[symbol]    = "long"
                                entry_px[symbol]    = px
                                qty[symbol]         = q
                                init_margin[symbol] = (px * q) / float(LEVERAGE)
                                cooldown_bars[symbol] = COOLDOWN_BARS
                                last_trough_level[symbol] = None
                                armed_long_switch[symbol] = False
                                continue

                # =============================
                #   ⚡ 청산 로직 (TP_MODE 적용)
                # =============================
                if position[symbol] == "short":
                    # 숏: 진입가 - 현재가
                    unreal = (entry_px[symbol] - cur_3) * qty[symbol]
                    roe    = (unreal / init_margin[symbol]) * 100

                    if tp_mode == 1:
                        # SL : ROE 기준
                        if roe <= -sl_roe:
                            bybit.close_position(symbol=symbol, side="Buy")
                            position[symbol] = None
                            continue
                        # 반대 방향 RSI (바닥 근처) → 익절
                        elif RSI <= long_rsi:
                            bybit.close_position(symbol=symbol, side="Buy")
                            position[symbol] = None
                            continue

                    elif tp_mode == 2:
                        # ROE TP/SL만 의존
                        if roe >= tp_roe or roe <= -sl_roe:
                            bybit.close_position(symbol=symbol, side="Buy")
                            position[symbol] = None
                            continue

                elif position[symbol] == "long":
                    # 롱: 현재가 - 진입가
                    unreal = (cur_3 - entry_px[symbol]) * qty[symbol]
                    roe    = (unreal / init_margin[symbol]) * 100

                    if tp_mode == 1:
                        # SL : ROE 기준
                        if roe <= -sl_roe:
                            bybit.close_position(symbol=symbol, side="Sell")
                            position[symbol] = None
                            continue
                        # 반대 방향 RSI (천장 근처) → 익절
                        elif RSI >= short_rsi:
                            bybit.close_position(symbol=symbol, side="Sell")
                            position[symbol] = None
                            continue

                    elif tp_mode == 2:
                        # ROE TP/SL만 의존
                        if roe >= tp_roe or roe <= -sl_roe:
                            bybit.close_position(symbol=symbol, side="Sell")
                            position[symbol] = None
                            continue

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
