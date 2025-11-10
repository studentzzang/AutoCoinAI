import os, time
from datetime import datetime, timezone
from typing import Optional, List
import pandas as pd
import numpy as np
from pybit.unified_trading import HTTP

# ================= 사용자 설정 =================
OUT_DIR        = r"d:\Projects\AutoCoinAI\test"
SYMBOLS        = ["PUMPFUNUSDT"]
TIMEFRAMES     = ["1"]

STOCH_PERIODS  = [50,100]
K_SMOOTH_ARR   = [5,10,20]
D_SMOOTH_ARR   = [5,10]
N_GAP_LIST     = [3,5,8]

TP_ROE_ARR     = [5,10,0]
SL_ROE_ARR     = [5,10,15,0]

#  과매수/과매도 기준값 (같은 인덱스끼리만 조합)
STO_OVERBOUGHT_ARR = [80]
STO_OVERSOLD_ARR   = [20]

USE_STRICT_THRESH = [True]   # 상/하한선 먼저 터치해야만 진입
K_ONLY_OK         = [False]   #  strict일 때만 작동 — K만 반등해도 진입 허용
USE_CROSS_STOPLOSS_ARR = [True, False]  #  반대 크로스 손절 사용 여부

EQUITY         = 100.0
LEVERAGE       = 10
START          = "2025-01-01"
END            = None
MAX_CANDLES    = 30000
SLEEP_PER_REQ  = 0.07
MAX_RETRY      = 3

session = HTTP()

# ================= 도우미 =================
def parse_date(s: Optional[str]) -> Optional[int]:
    if not s: return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s, "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def fetch_ohlcv(symbol: str, tf: str, start_ms: Optional[int], end_ms: Optional[int], cap: Optional[int]) -> pd.DataFrame:
    interval = tf
    if start_ms is None: start_ms = parse_date("2018-01-01")
    if end_ms is None: end_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    rows = []
    cur_end = end_ms

    while len(rows) < cap and cur_end > start_ms:
        try:
            resp = session.get_kline(category="linear", symbol=symbol, interval=interval, end=cur_end, limit=1000)
        except Exception as e:
            print("API Error", e)
            time.sleep(1); continue

        if resp.get("retCode") != 0:
            print(f"❌ API Error {resp.get('retMsg')}"); break

        result = resp.get("result", {})
        lst = result.get("list", result.get("rows", []))
        if not lst:
            print(f"[WARN] No kline data for {symbol} {tf}"); break

        for it in lst:
            ts = int(it[0]) if isinstance(it, list) else int(it.get("start", it.get("startTime", 0)))
            o = float(it[1]) if isinstance(it, list) else float(it.get("open", 0))
            h = float(it[2]) if isinstance(it, list) else float(it.get("high", 0))
            l = float(it[3]) if isinstance(it, list) else float(it.get("low", 0))
            c = float(it[4]) if isinstance(it, list) else float(it.get("close", 0))
            v = float(it[5]) if isinstance(it, list) else float(it.get("volume", 0))
            rows.append((ts, o, h, l, c, v))

        cur_end = min(r[0] for r in rows[-len(lst):]) - 1
        if len(lst) < 1000: break
        time.sleep(SLEEP_PER_REQ)

    if not rows:
        print(f"[EMPTY] {symbol}@{tf}")
        return pd.DataFrame(columns=["ts","open","high","low","close","volume"])

    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"])
    df.drop_duplicates("ts", inplace=True)
    df.sort_values("ts", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df.tail(cap)

def compute_stoch(df, period:int, k_smooth:int, d_smooth:int):
    low_min = df["low"].rolling(period).min()
    high_max = df["high"].rolling(period).max()
    df["%K_raw"] = 100 * (df["close"] - low_min) / (high_max - low_min + 1e-9)
    df["%K"] = df["%K_raw"].rolling(k_smooth).mean()
    df["%D"] = df["%K"].rolling(d_smooth).mean()
    return df

# ================= 백테스트 =================
def backtest(symbol, tf, period, k_smooth, d_smooth, tp_roe, sl_roe, gap,
             overbought, oversold, use_strict, k_only_ok, use_cross_stoploss):   
    start_ms = parse_date(START); end_ms = parse_date(END)
    ohlc = fetch_ohlcv(symbol, tf, start_ms, end_ms, MAX_CANDLES)
    if ohlc.empty: return pd.DataFrame()

    ohlc = compute_stoch(ohlc, period, k_smooth, d_smooth)
    ohlc.dropna(inplace=True)
    ohlc.reset_index(drop=True, inplace=True)
    
    position = None
    entry_px = None
    qty = None
    notional = EQUITY * LEVERAGE
    eq_used = EQUITY
    logs = []

    touched_upper = False
    touched_lower = False

    for i in range(2, len(ohlc)):
        ts = int(ohlc.loc[i, "ts"]) // 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        k_prev, d_prev = ohlc.loc[i-1, "%K"], ohlc.loc[i-1, "%D"]
        k_now,  d_now  = ohlc.loc[i, "%K"],  ohlc.loc[i, "%D"]
        px = ohlc.loc[i, "close"]

        # strict 조건일 때 상/하한 터치 체크
        if use_strict:
            if overbought and ((k_only_ok and k_now >= overbought) or (not k_only_ok and k_now >= overbought and d_now >= overbought)):
                touched_upper = True
            if oversold and ((k_only_ok and k_now <= oversold) or (not k_only_ok and k_now <= oversold and d_now <= oversold)):
                touched_lower = True

        # === 진입 ===
        if position is None:
            # 숏 진입
            if (k_prev > d_prev) and (k_now < d_now) and (k_prev - d_prev >= gap):
                cond_now = (k_now > overbought) and (d_now > overbought)
                if use_strict:
                    cond_now = (k_now > overbought) if k_only_ok else ((k_now > overbought) and (d_now > overbought))
                if cond_now and ((not use_strict) or touched_upper):
                    position = "SHORT"; entry_px = px; qty = notional / px
                    touched_upper = False; continue

            # 롱 진입
            if (k_prev < d_prev) and (k_now > d_now) and (d_prev - k_prev >= gap):
                cond_now = (k_now < oversold) and (d_now < oversold)
                if use_strict:
                    cond_now = (k_now < oversold) if k_only_ok else ((k_now < oversold) and (d_now < oversold))
                if cond_now and ((not use_strict) or touched_lower):
                    position = "LONG"; entry_px = px; qty = notional / px
                    touched_lower = False; continue

        # === 청산 ===
        if position:
            pnl = (px - entry_px) * qty if position == "LONG" else (entry_px - px) * qty
            roe = (pnl / eq_used) * 100
            hit_tp = (tp_roe > 0 and roe >= tp_roe)
            hit_sl = (sl_roe > 0 and roe <= -sl_roe)

            if hit_tp or hit_sl:
                logs.append([dt, symbol, tf, period, gap, f"{position}→{'TP' if hit_tp else 'SL'}",
                             entry_px, px, pnl, roe, k_now, d_now])
                position = None; entry_px = None; qty = None; continue

            # 반대 크로스 손절 옵션 적용
            if use_cross_stoploss:
                if position == "LONG" and (k_prev > d_prev) and (k_now < d_now) and (d_prev - k_prev >= gap):
                    logs.append([dt, symbol, tf, period, gap, "LONG→CrossSL", entry_px, px, pnl, roe, k_now, d_now])
                    position = None; entry_px = None; qty = None; continue
                if position == "SHORT" and (k_prev < d_prev) and (k_now > d_now) and (k_prev - d_prev >= gap):
                    logs.append([dt, symbol, tf, period, gap, "SHORT→CrossSL", entry_px, px, pnl, roe, k_now, d_now])
                    position = None; entry_px = None; qty = None; continue

            # 반대 교차 시 청산 (기본 EXIT)
            if position == "LONG" and (k_prev > d_prev) and (k_now < d_now):
                logs.append([dt, symbol, tf, period, gap, "LONG→EXIT", entry_px, px, pnl, roe, k_now, d_now])
                position = None; entry_px = None; qty = None; continue
            if position == "SHORT" and (k_prev < d_prev) and (k_now > d_now):
                logs.append([dt, symbol, tf, period, gap, "SHORT→EXIT", entry_px, px, pnl, roe, k_now, d_now])
                position = None; entry_px = None; qty = None; continue

    return pd.DataFrame(logs, columns=[
        "datetime","symbol","timeframe","period","gap%","position",
        "entry","exit","PnL","ROE","%K_now","%D_now"
    ])

# ================= 실행 =================
if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    THRESH_PAIRS = list(zip(STO_OVERBOUGHT_ARR, STO_OVERSOLD_ARR))  # 인덱스 동일 조합만 실행

    for s in SYMBOLS:
        for tf in TIMEFRAMES:
            for p in STOCH_PERIODS:
                for ks in K_SMOOTH_ARR:
                    for ds in D_SMOOTH_ARR:
                        for tp in TP_ROE_ARR:
                            for sl in SL_ROE_ARR:
                                for gap in N_GAP_LIST:
                                    for (overbought, oversold) in THRESH_PAIRS:
                                        for use_strict in USE_STRICT_THRESH:
                                            for k_only_ok in K_ONLY_OK:
                                                for use_cross_sl in USE_CROSS_STOPLOSS_ARR:
                                                    label = f"{s}@{tf} ST{p} K{ks}D{ds} gap{gap}% TP{tp} SL{sl} OB{overbought} OS{oversold} strict{use_strict} Konly{k_only_ok} CrossSL{use_cross_sl}"
                                                    print(f"▶ {label}")
                                                    df = backtest(s, tf, p, ks, ds, tp, sl, gap,
                                                                  overbought, oversold, use_strict, k_only_ok, use_cross_sl)
                                                    if df.empty: continue
                                                    fname = f"{s}_{tf}_ST{p}_K{ks}D{ds}_gap{gap}_TP{tp}_SL{sl}_OB{overbought}_OS{oversold}_strict{use_strict}_Konly{k_only_ok}_CrossSL{use_cross_sl}.csv"
                                                    df.to_csv(os.path.join(OUT_DIR, fname), index=False, encoding="utf-8-sig")
                                                    print(f"✅ Saved: {fname}")
