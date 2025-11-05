import os, time
from datetime import datetime, timezone
from typing import Optional, List
import pandas as pd
import numpy as np
from pybit.unified_trading import HTTP

# ================= 사용자 설정 =================
OUT_DIR        = r"d:\Projects\AutoCoinAI\test"
SYMBOLS        = ["PUMPFUNUSDT"]
TIMEFRAMES     = ["5"]

STOCH_PERIODS  = [7, 9, 14, 20]
K_SMOOTH_ARR   = [3, 5]
D_SMOOTH_ARR   = [3, 5]
N_GAP_LIST     = [1, 3, 5]   # % 차이 (K-D) 최소 갭 조건

TP_ROE_ARR     = [10, 15]
SL_ROE_ARR     = [10, 15]

STO_K_THRESH_ARR = [80, 70, 0]  
STO_D_THRESH_ARR = [20, 30, 0]  

EQUITY         = 100.0
LEVERAGE       = 5
START          = "2025-01-01"
END            = None
MAX_CANDLES    = 20000
SLEEP_PER_REQ  = 0.11
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
            time.sleep(1)
            continue

        if resp.get("retCode") != 0:
            print(f"❌ API Error {resp.get('retMsg')}")
            break

        result = resp.get("result", {})
        lst = result.get("list", result.get("rows", []))
        if not lst:
            print(f"[WARN] No kline data for {symbol} {tf}")
            break

        for it in lst:
            ts = int(it[0]) if isinstance(it, list) else int(it.get("start", it.get("startTime", 0)))
            o = float(it[1]) if isinstance(it, list) else float(it.get("open", 0))
            h = float(it[2]) if isinstance(it, list) else float(it.get("high", 0))
            l = float(it[3]) if isinstance(it, list) else float(it.get("low", 0))
            c = float(it[4]) if isinstance(it, list) else float(it.get("close", 0))
            v = float(it[5]) if isinstance(it, list) else float(it.get("volume", 0))
            rows.append((ts, o, h, l, c, v))

        cur_end = min(r[0] for r in rows[-len(lst):]) - 1
        if len(lst) < 1000:
            break
        time.sleep(SLEEP_PER_REQ)

    if not rows:
        print(f"[EMPTY] {symbol}@{tf}")
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
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
def backtest(symbol, tf, period, k_smooth, d_smooth, tp_roe, sl_roe, gap, thresh_K, thresh_D):
    start_ms = parse_date(START); end_ms = parse_date(END)
    ohlc = fetch_ohlcv(symbol, tf, start_ms, end_ms, MAX_CANDLES)
    if ohlc.empty: 
        return pd.DataFrame()

    ohlc = compute_stoch(ohlc, period, k_smooth, d_smooth)
    ohlc.dropna(inplace=True)
    ohlc.reset_index(drop=True, inplace=True)
    
    position = None
    entry_px = None
    qty = None
    notional = EQUITY * LEVERAGE
    eq_used = EQUITY
    logs = []

    for i in range(2, len(ohlc)):
        ts = int(ohlc.loc[i, "ts"]) // 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        k_prev, d_prev = ohlc.loc[i-1, "%K"], ohlc.loc[i-1, "%D"]
        k_now,  d_now  = ohlc.loc[i, "%K"], ohlc.loc[i, "%D"]
        px = ohlc.loc[i, "close"]

        use_threshold = not (thresh_K == 0 and thresh_D == 0)

        # === 진입 조건 ===
        if position is None:
            # 숏 진입
            if (k_prev > d_prev) and (k_now < d_now) and (k_prev - d_prev >= gap):
                if not use_threshold or k_now > thresh_K:
                    position = "SHORT"
                    entry_px = px
                    qty = notional / px
                    continue

            # 롱 진입
            if (k_prev < d_prev) and (k_now > d_now) and (d_prev - k_prev >= gap):
                if not use_threshold or k_now < thresh_D:
                    position = "LONG"
                    entry_px = px
                    qty = notional / px
                    continue

        # === 청산 조건 ===
        if position:
            pnl = (px - entry_px) * qty if position == "LONG" else (entry_px - px) * qty
            roe = (pnl / eq_used) * 100

            if roe >= tp_roe or roe <= -sl_roe:
                logs.append([dt, symbol, tf, period, gap, position, entry_px, px, pnl, roe, k_now, d_now])
                position = None; entry_px = None; qty = None
                continue

            # 반대 신호 → 청산
            if position == "LONG" and (k_prev > d_prev) and (k_now < d_now):
                if not use_threshold or k_now > thresh_K:
                    logs.append([dt, symbol, tf, period, gap, "LONG→EXIT", entry_px, px, pnl, roe, k_now, d_now])
                    position = None; entry_px = None; qty = None
                    continue
            if position == "SHORT" and (k_prev < d_prev) and (k_now > d_now):
                if not use_threshold or k_now < thresh_D:
                    logs.append([dt, symbol, tf, period, gap, "SHORT→EXIT", entry_px, px, pnl, roe, k_now, d_now])
                    position = None; entry_px = None; qty = None
                    continue

    return pd.DataFrame(logs, columns=[
        "datetime", "symbol", "timeframe", "period", "gap%", "position",
        "entry", "exit", "PnL", "ROE", "%K_now", "%D_now"
    ])


# ================= 실행 =================
if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)

    # 🔽 변경 포인트: zip으로 묶어서 같은 인덱스끼리만 실행
    KD_PAIRS = list(zip(STO_K_THRESH_ARR, STO_D_THRESH_ARR))

    for s in SYMBOLS:
        for tf in TIMEFRAMES:
            for p in STOCH_PERIODS:
                for ks in K_SMOOTH_ARR:
                    for ds in D_SMOOTH_ARR:
                        for tp in TP_ROE_ARR:
                            for sl in SL_ROE_ARR:
                                for gap in N_GAP_LIST:
                                    for (k_th, d_th) in KD_PAIRS:  # ← zip 적용
                                        label = f"{s}@{tf} ST{p} K{ks}D{ds} gap{gap}% TP{tp} SL{sl} K{k_th} D{d_th}"
                                        print(f"▶ {label}")
                                        
                                        df = backtest(s, tf, p, ks, ds, tp, sl, gap, k_th, d_th)
                                        if df.empty: 
                                            continue

                                        fname = f"{s}_{tf}_ST{p}_K{ks}D{ds}_gap{gap}_TP{tp}_SL{sl}_K{k_th}_D{d_th}.csv"
                                        df.to_csv(os.path.join(OUT_DIR, fname), index=False, encoding="utf-8-sig")
                                        print(f"✅ Saved: {fname}")
