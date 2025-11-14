import os
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import numpy as np
from pybit.unified_trading import HTTP

# ====== 사용자 설정 변수 ======
SYMBOL = ["PUMPFUNUSDT","FARTCOINUSDT"]
LEVERAGE = 5
TIMEFRAME = [1,5,15,30]
RSI_PERIOD = [7, 9, 10, 14]
EQUITY = 100.0
START = "2025-02-01"
END = "2025-11-12"
OUT_DIR = "test"

MAX_CANDLES = 20000 

# RSI 트리거 값
OPEN_SHORT_RSI  = 72.0   # 숏 진입 기준 (롱 반대 과상태)
OPEN_LONG_RSI   = 28.0   # 롱 진입 기준 (숏 반대 과상태)
CLOSE_SHORT_RSI = 70.0
CLOSE_LONG_RSI  = 30.0

# DOORSTEP 밴드 (반대 과상태일 때만 쓰는 RSI 범위)
DOORSTEP = 3.0

# ====== TP / SL 배열 ======
TP_ROE_ARR   = [10,15]
SL_ROE_ARR   = [10,15]
TP_MODE_ARR  = [1, 2]   # 1 = DOORSTEP + TP / SL, 2 = TP/SL만 의존
# ==========================

session = HTTP()

# ---------- 유틸 ----------
def _as_list(x):
    return x if isinstance(x, (list, tuple)) else [x]

def parse_date(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s, "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def bybit_interval(tf: str) -> str:
    tf = str(tf).upper()
    mapping = {
        "1":"1","3":"3","5":"5","15":"15","30":"30",
        "60":"60","120":"120","240":"240","360":"360","720":"720",
        "D":"D","W":"W","M":"M"
    }
    if tf not in mapping:
        raise ValueError(f"지원하지 않는 분봉/주기: {tf}")
    return mapping[tf]

def fetch_ohlcv_10000(symbol: str, tf: str, start_ms=None, end_ms=None, max_candles: int = MAX_CANDLES) -> pd.DataFrame:
    interval = bybit_interval(tf)
    if end_ms is None:
        end_ms = int(datetime.now(tz=timezone.utc).timestamp()*1000)

    rows = []
    while len(rows) < max_candles:
        resp = None
        last_err = None
        for attempt in range(3):
            try:
                r = session.get_kline(
                    category="linear",
                    symbol=symbol,
                    interval=interval,
                    end=end_ms,
                    limit=1000
                )
                if r.get("retCode") == 0:
                    resp = r
                    break
                else:
                    last_err = RuntimeError(f"retCode {r.get('retCode')} {r.get('retMsg')}")
            except Exception as e:
                last_err = e
            time.sleep(0.4)

        if resp is None:
            raise last_err if last_err else RuntimeError("Unknown API error")

        lst = resp["result"]["list"]
        if not lst:
            break
        for it in lst:
            ts = int(it[0]); o,h,l,c,v = map(float, it[1:6])
            rows.append((ts,o,h,l,c,v))
        end_ms = min(int(x[0]) for x in lst) - 1
        if len(lst) < 1000:
            break
        time.sleep(0.12)

    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"]).drop_duplicates("ts")
    df.sort_values("ts", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df.head(max_candles)

def compute_rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = (-delta).clip(lower=0)
    roll_up = up.ewm(alpha=1/period, adjust=False).mean()
    roll_down = down.ewm(alpha=1/period, adjust=False).mean()
    rs = roll_up / (roll_down + 1e-12)
    return 100 - (100/(1+rs))

# ---------- 시뮬 ----------
def run(symbol: str, tf: str, rsi_period: int, leverage: float, equity: float,
        start: Optional[str], end: Optional[str], out_dir: str,
        tp_roe: float, sl_roe: float, tp_mode: int) -> str:
    
    start_ms = parse_date(start)
    end_ms   = parse_date(end)

    ohlc = fetch_ohlcv_10000(symbol, tf, start_ms, end_ms)
    if ohlc.empty:
        raise SystemExit("❌ 시세 데이터가 비었습니다. 심볼/기간/분봉을 확인하세요.")

    ohlc["rsi"] = compute_rsi(ohlc["close"], rsi_period)

    cols = ["datetime","symbol","timeframe","close","rsi","포지션","비고","entry_price","미실현PnL","ROE"]
    log = []

    position    = None
    entry_px    = None
    qty         = None
    init_margin = None

    for i in range(len(ohlc)):
        ts = int(ohlc.loc[i, "ts"]) // 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        px = float(ohlc.loc[i, "close"])
        rv = float(ohlc.loc[i, "rsi"]) if not np.isnan(ohlc.loc[i, "rsi"]) else None

        remark      = ""
        pos_name    = position if position else "FLAT"
        entry_price = entry_px if entry_px is not None else np.nan
        unreal      = 0.0
        roe         = 0.0

        # === 진입 ===
        if position is None and rv is not None:
            if rv >= OPEN_SHORT_RSI:
                position = "SHORT"; entry_px = px
                notional    = equity * leverage
                qty         = notional / entry_px
                init_margin = notional / leverage
                remark      = "SHORT 진입"
            elif rv <= OPEN_LONG_RSI:
                position = "LONG"; entry_px = px
                notional    = equity * leverage
                qty         = notional / entry_px
                init_margin = notional / leverage
                remark      = "LONG 진입"

        # === 보유 중 ===
        elif position is not None and rv is not None:

            if position == "LONG":
                unreal = (px - entry_px) * qty
                roe    = (unreal / init_margin) * 100

                if tp_mode == 1:
                    # 1) SL : 항상 ROE 기준
                    if roe <= -sl_roe:
                        remark = f"close LONG (SL {roe:.1f}%)"; position = None

                    # 2) TP : 조건 분기
                    elif roe >= tp_roe:
                        # 반대 과상태: 과매수 영역 (롱의 반대)
                        if rv >= OPEN_SHORT_RSI:
                            # DOORSTEP 밴드 안에 들어왔을 때만 청산
                            if (OPEN_SHORT_RSI - DOORSTEP) <= rv <= (OPEN_SHORT_RSI + DOORSTEP):
                                remark = f"close LONG (DOORSTEP TP, ROE {roe:.1f}%)"; position = None
                            # DOORSTEP 바깥이면 계속 홀딩
                        else:
                            # 반대 과상태가 아니면 TP에만 의존 → TP 즉시 청산
                            remark = f"close LONG (TP {roe:.1f}%)"; position = None

                elif tp_mode == 2:
                    if roe >= tp_roe:
                        remark = f"close LONG (TP {roe:.1f}%)"; position = None
                    elif roe <= -sl_roe:
                        remark = f"close LONG (SL {roe:.1f}%)"; position = None


            elif position == "SHORT":
                unreal = (entry_px - px) * qty
                roe    = (unreal / init_margin) * 100

                if tp_mode == 1:
                    # 1) SL : 항상 ROE 기준
                    if roe <= -sl_roe:
                        remark = f"close SHORT (SL {roe:.1f}%)"; position = None

                    # 2) TP : 조건 분기
                    elif roe >= tp_roe:
                        # 반대 과상태: 과매도 영역 (숏의 반대)
                        if rv <= OPEN_LONG_RSI:
                            if (OPEN_LONG_RSI - DOORSTEP) <= rv <= (OPEN_LONG_RSI + DOORSTEP):
                                remark = f"close SHORT (DOORSTEP TP, ROE {roe:.1f}%)"; position = None
                            # DOORSTEP 바깥 → 홀딩
                        else:
                            # 반대 과상태 아니면 TP에만 의존
                            remark = f"close SHORT (TP {roe:.1f}%)"; position = None

                elif tp_mode == 2:
                    if roe >= tp_roe:
                        remark = f"close SHORT (TP {roe:.1f}%)"; position = None
                    elif roe <= -sl_roe:
                        remark = f"close SHORT (SL {roe:.1f}%)"; position = None

            # === 청산된 경우만 로그 기록 ===
            if remark and "close" in remark:
                log.append([dt, symbol, tf, px, rv, "CLOSE", remark, entry_px, unreal, roe])
                entry_px = None
                qty = None
                init_margin = None
                continue

    # === 청산된 데이터만 저장 ===
    df = pd.DataFrame(log, columns=cols)
    df = df[df["포지션"] == "CLOSE"].reset_index(drop=True)

    os.makedirs(out_dir, exist_ok=True)
    fname = f"{symbol}_{tf}_{rsi_period}_TP{tp_roe}_SL{sl_roe}_MODE{tp_mode}.csv"
    path = os.path.join(out_dir, fname)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path

# ---------- 실행 ----------
if __name__ == "__main__":
    for s in _as_list(SYMBOL):
        for tf in _as_list(TIMEFRAME):
            for rp in _as_list(RSI_PERIOD):
                for tp in TP_ROE_ARR:
                    for sl in SL_ROE_ARR:
                        for mode in TP_MODE_ARR:
                            csv_path = run(s, tf, rp, LEVERAGE, EQUITY, START, END, OUT_DIR, tp, sl, mode)
                            print(f"✅ 저장 완료: {csv_path}")
