import os, time
import pandas as pd
import numpy as np
from datetime import datetime, timezone
import bybit
from bybit import (
    get_kline_http, get_current_price, entry_position, close_position,
    get_position_size, set_leverage, get_usdt, get_ROE, get_PnL
)

# ================= 사용자 설정 =================
SYMBOLS        = ["PUMPFUNUSDT"]
TIMEFRAMES     = ["15"]
STOCH_PERIODS  = [9]
K_SMOOTH_ARR   = [5]
D_SMOOTH_ARR   = [3]
TP_ROE_ARR     = [15]
SL_ROE_ARR     = [15]
GAP_ARR        = [1]      # K-D 최소 차이(%) 조건
LEVERAGE_ARR   = [5]
PCT_ARR        = [50]     # ← 심볼 개수와 길이 맞춤

# ================= 전역상태 =================
open_positions = {s: None for s in SYMBOLS}   # "LONG"/"SHORT"/None
entry_px       = {s: None for s in SYMBOLS}

# ================= 유틸 =================
def utc_now_str():
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def kline_list_to_df(kl):
    if not kl:
        return pd.DataFrame(columns=["ts","open","high","low","close","volume"])
    if isinstance(kl[0], (list, tuple)):
        df = pd.DataFrame(kl)
        if df.shape[1] < 6:
            raise ValueError(f"kline columns < 6: got {df.shape[1]}")
        df = df.iloc[:, :6].copy()
        df.columns = ["ts","open","high","low","close","volume"]
    elif isinstance(kl[0], dict):
        df = pd.DataFrame(kl).copy()
        if "start" in df.columns: df.rename(columns={"start":"ts"}, inplace=True)
        if "startTime" in df.columns: df.rename(columns={"startTime":"ts"}, inplace=True)
        need = ["ts","open","high","low","close","volume"]
        missing = [c for c in need if c not in df.columns]
        if missing:
            raise ValueError(f"missing keys in kline dict: {missing}")
        df = df[need].copy()
    else:
        raise TypeError(f"unexpected kline row type: {type(kl[0])}")
    df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.dropna(subset=["ts","open","high","low","close"], inplace=True)
    df.sort_values("ts", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

def compute_stoch(df, period:int, k_smooth:int, d_smooth:int):
    low_min  = df["low"].rolling(period).min()
    high_max = df["high"].rolling(period).max()
    df["%K_raw"] = 100 * (df["close"] - low_min) / (high_max - low_min + 1e-9)
    df["%K"] = df["%K_raw"].rolling(k_smooth).mean()
    df["%D"] = df["%K"].rolling(d_smooth).mean()
    return df.dropna()

def get_stoch(symbol, interval, period, k_smooth, d_smooth):
    kl = get_kline_http(symbol, interval, limit=50)
    df = kline_list_to_df(kl)
    df = compute_stoch(df, period, k_smooth, d_smooth)
    # 직전/현재 값 반환
    return float(df["%K"].iloc[-2]), float(df["%D"].iloc[-2]), float(df["%K"].iloc[-1]), float(df["%D"].iloc[-1])

# ================= 실행 =================
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

            # 실시간 값
            k_prev, d_prev, k_now, d_now = get_stoch(sym, tf, period, ks, ds)
            roe = get_ROE(sym)
            pnl = get_PnL(sym)
            pos_size = get_position_size(sym)
            px = get_current_price(sym)

            # === 진입 조건 ===
            if pos_size == 0:
                bybit.PCT = pct
                # 숏 진입: K↓D 교차 + (K-D)≥gap + K>80
                if (k_prev > d_prev) and (k_now < d_now) and (k_prev - d_prev >= gap) and (k_now > 80):
                    print(f"📉 [{sym}] 숏 진입 | K={k_now:.2f} D={d_now:.2f}")
                    entry_px[sym], qty = entry_position(sym, lev, "Sell")
                    open_positions[sym] = "SHORT"

                # 롱 진입: K↑D 교차 + (D-K)≥gap + K<20
                elif (k_prev < d_prev) and (k_now > d_now) and (d_prev - k_prev >= gap) and (k_now < 20):
                    print(f"📈 [{sym}] 롱 진입 | K={k_now:.2f} D={d_now:.2f}")
                    entry_px[sym], qty = entry_position(sym, lev, "Buy")
                    open_positions[sym] = "LONG"

            else:
                # === 청산 조건 (TP/SL + 반대 크로스+반대 과상태) ===
                opp_close = False
                opp_reason = ""

                if open_positions[sym] == "LONG":
                    crossed_down = (k_prev > d_prev) and (k_now < d_now)   # K↓D
                    overbought   = max(k_prev, d_prev, k_now, d_now) >= 80
                    if crossed_down and overbought:
                        opp_close = True
                        opp_reason = f"OppX K↓D@80+ (K={k_now:.2f}, D={d_now:.2f})"

                elif open_positions[sym] == "SHORT":
                    crossed_up = (k_prev < d_prev) and (k_now > d_now)     # K↑D
                    oversold   = min(k_prev, d_prev, k_now, d_now) <= 20
                    if crossed_up and oversold:
                        opp_close = True
                        opp_reason = f"OppX K↑D@20- (K={k_now:.2f}, D={d_now:.2f})"

                if roe >= tp_roe:
                    print(f"💰 [{sym}] TP 도달 (ROE={roe:.2f}%) → 포지션 종료")
                    side = "Buy" if open_positions[sym] == "SHORT" else "Sell"
                    close_position(sym, side)
                    open_positions[sym] = None
                    entry_px[sym] = None

                elif roe <= -sl_roe:
                    print(f"🛑 [{sym}] SL 도달 (ROE={roe:.2f}%) → 포지션 종료")
                    side = "Buy" if open_positions[sym] == "SHORT" else "Sell"
                    close_position(sym, side)
                    open_positions[sym] = None
                    entry_px[sym] = None

                elif opp_close:
                    print(f"🔄 [{sym}] {opp_reason} → 포지션 종료")
                    side = "Buy" if open_positions[sym] == "SHORT" else "Sell"
                    close_position(sym, side)
                    open_positions[sym] = None
                    entry_px[sym] = None

            # === 상태 출력 (항상) ===
            pos_str = open_positions.get(sym) or "-"
            print(
                f"[{utc_now_str()}] 🪙{sym} @{tf} "
                f"💲현재가: {px:.6f}  🚩포지션 {pos_str}  "
                f"| ST%K/%D({period},{ks},{ds}) = {k_now:.2f}/{d_now:.2f} (prev {k_prev:.2f}/{d_prev:.2f}) "
                f"| 💎PnL: {pnl:.6f} ⚜️ROE: {roe:.2f}%"
            )

        time.sleep(30)  # 30초 주기
    except Exception as e:
        print(f"⚠️ 오류 발생: {e}")
        time.sleep(10)
