import os, time
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from bybit import (
    get_kline_http, get_current_price, entry_position, close_position,
    get_position_size, set_leverage, get_usdt, get_ROE
)

# ================= 사용자 설정 =================
SYMBOLS        = ["ETHUSDT", "PUMPFUNUSDT"]
TIMEFRAMES     = ["15", "5"]
STOCH_PERIODS  = [14, 9]
K_SMOOTH_ARR   = [3, 5]
D_SMOOTH_ARR   = [3, 5]
TP_ROE_ARR     = [10, 7.5]
SL_ROE_ARR     = [10, 10]
GAP_ARR        = [1, 3]
LEVERAGE_ARR   = [5, 5]
PCT_ARR        = [50, 50]   # 투자 비중 %

# ================= 전역상태 =================
open_positions = {s: None for s in SYMBOLS}
entry_px       = {s: None for s in SYMBOLS}

# ================= 함수 =================
def compute_stoch(df, period:int, k_smooth:int, d_smooth:int):
    low_min = df["low"].rolling(period).min()
    high_max = df["high"].rolling(period).max()
    df["%K_raw"] = 100 * (df["close"] - low_min) / (high_max - low_min + 1e-9)
    df["%K"] = df["%K_raw"].rolling(k_smooth).mean()
    df["%D"] = df["%K"].rolling(d_smooth).mean()
    return df.dropna()

def get_stoch(symbol, interval, period, k_smooth, d_smooth):
    kl = get_kline_http(symbol, interval, limit=50)
    df = pd.DataFrame(kl, columns=["ts","open","high","low","close","volume"])
    df[["open","high","low","close"]] = df[["open","high","low","close"]].astype(float)
    df = compute_stoch(df, period, k_smooth, d_smooth)
    return float(df["%K"].iloc[-2]), float(df["%D"].iloc[-2]), float(df["%K"].iloc[-1]), float(df["%D"].iloc[-1])

# ================= 실행 =================
print("🚀 실시간 스토캐스틱 전략 시작")
print(f"보유 USDT: {get_usdt():.2f}")

for i, s in enumerate(SYMBOLS):
    set_leverage(s, LEVERAGE_ARR[i])

while True:
    try:
        for i, sym in enumerate(SYMBOLS):
            tf       = TIMEFRAMES[i]
            period   = STOCH_PERIODS[i]
            ks       = K_SMOOTH_ARR[i]
            ds       = D_SMOOTH_ARR[i]
            gap      = GAP_ARR[i]
            tp_roe   = TP_ROE_ARR[i]
            sl_roe   = SL_ROE_ARR[i]
            lev      = LEVERAGE_ARR[i]
            pct      = PCT_ARR[i]

            k_prev, d_prev, k_now, d_now = get_stoch(sym, tf, period, ks, ds)
            roe = get_ROE(sym)
            pos_size = get_position_size(sym)
            px = get_current_price(sym)

            # === 진입 조건 ===
            if pos_size == 0:
                # 숏 진입
                if (k_prev > d_prev) and (k_now < d_now) and (k_prev - d_prev >= gap) and (k_now > 80):
                    print(f"📉 [{sym}] 숏 진입 | K={k_now:.2f} D={d_now:.2f}")
                    entry_px[sym], qty = entry_position(sym, lev, "Sell")
                    open_positions[sym] = "SHORT"
                    continue

                # 롱 진입
                if (k_prev < d_prev) and (k_now > d_now) and (d_prev - k_prev >= gap) and (k_now < 20):
                    print(f"📈 [{sym}] 롱 진입 | K={k_now:.2f} D={d_now:.2f}")
                    entry_px[sym], qty = entry_position(sym, lev, "Buy")
                    open_positions[sym] = "LONG"
                    continue

            # === 청산 조건 ===
            if open_positions[sym]:
                if roe >= tp_roe or roe <= -sl_roe:
                    print(f"💰 [{sym}] TP/SL 도달 (ROE={roe:.2f}%) → 포지션 종료")
                    side = "Buy" if open_positions[sym] == "SHORT" else "Sell"
                    close_position(sym, side)
                    open_positions[sym] = None
                    entry_px[sym] = None
                    continue

        time.sleep(30)  # 30초 주기
    except Exception as e:
        print(f"⚠️ 오류 발생: {e}")
        time.sleep(10)
