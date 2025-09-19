#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
사용자가 직접 변수(symbol, leverage, timeframe, rsi_period, out_dir)를 위에서 지정하도록 수정
트리거 RSI 값(진입/청산)도 변수로 지정 가능하게 확장
"""

import os
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import numpy as np
from pybit.unified_trading import HTTP

# ====== 사용자 설정 변수 ======
SYMBOL = ["1000PEPEUSDT"]   # 심볼 배열
LEVERAGE = 5                        # 레버리지
TIMEFRAME = ["D"]                 # 분봉/주기 배열 (1,3,5,15,30,60,120,240,720,D,W,M)

RSI_PERIOD = [6,9,12]               # RSI 기간 배열
EQUITY = 100.0                     # 가정 자본(USDT)
START = "2025-01-01"                # 시작일 (없으면 최대 과거)
END = "2025-09-18"                  # 종료일 (없으면 현재)
OUT_DIR = "tests"              # 저장 경로
MAX_CANDLES = 10000  # 최대 캔들 수 제한 (None이면 제한 없음, 예: 10000, 1500 등)


# 트리거 값 (원하는 대로 수정 가능)
OPEN_SHORT_RSI = 72.0   # SHORT 진입 RSI 이상
OPEN_LONG_RSI = 28.0    # LONG 진입 RSI 이하
CLOSE_SHORT_RSI = 32.0  # SHORT 청산 RSI 이하
CLOSE_LONG_RSI = 68.0   # LONG 청산 RSI 이상
# ===========================

session = HTTP()

# ---------- 유틸 ----------

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


def fetch_ohlcv_capped(symbol: str, tf: str, start_ms: Optional[int], end_ms: Optional[int], max_candles: Optional[int]) -> pd.DataFrame:
    interval = bybit_interval(tf)
    if start_ms is None:
        start_ms = parse_date("2018-01-01")
    if end_ms is None:
        end_ms = int(datetime.now(tz=timezone.utc).timestamp()*1000)

    rows = []
    cap = max_candles if max_candles is not None else 10**18
    cur_end = end_ms

    while len(rows) < cap and cur_end > start_ms:
        req_limit = int(min(1000, cap - len(rows)))  # Bybit 콜당 최대 1000
        resp = session.get_kline(
            category="linear",
            symbol=symbol,
            interval=bybit_interval(tf),
            end=cur_end,
            limit=req_limit,
        )
        if resp.get("retCode") != 0:
            raise RuntimeError(resp.get("retMsg"))
        lst = resp.get("result", {}).get("list", [])
        if not lst:
            break

        for it in lst:
            ts = int(it[0])
            if ts < start_ms:
                continue
            o = float(it[1]); h = float(it[2]); l = float(it[3]); c = float(it[4]); v = float(it[5])
            rows.append((ts, o, h, l, c, v))

        min_ts = min(int(x[0]) for x in lst)
        cur_end = min_ts - 1
        if len(lst) < req_limit:
            break
        time.sleep(0.12)

    if not rows:
        return pd.DataFrame(columns=["ts","open","high","low","close","volume"]).astype({
            "ts":"int64","open":"float64","high":"float64","low":"float64","close":"float64","volume":"float64"
        })

    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"]).drop_duplicates("ts")
    df.sort_values("ts", inplace=True)
    if max_candles is not None:
        df = df.tail(int(max_candles))
    df.reset_index(drop=True, inplace=True)
    return df



def compute_rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = (-delta).clip(lower=0)
    roll_up = up.ewm(alpha=1/period, adjust=False).mean()
    roll_down = down.ewm(alpha=1/period, adjust=False).mean()
    rs = roll_up / (roll_down + 1e-12)
    return 100 - (100/(1+rs))

def _as_list(x):
    return x if isinstance(x, (list, tuple)) else [x]
    up = delta.clip(lower=0)
    down = (-delta).clip(lower=0)
    roll_up = up.ewm(alpha=1/period, adjust=False).mean()
    roll_down = down.ewm(alpha=1/period, adjust=False).mean()
    rs = roll_up / (roll_down + 1e-12)
    return 100 - (100/(1+rs))

# ---------- 시뮬 ----------

def run(symbol: str, tf: str, rsi_period: int, leverage: float, equity: float,
        start: Optional[str], end: Optional[str], out_dir: str) -> str:
    start_ms = parse_date(start)
    end_ms = parse_date(end)

    ohlc = fetch_ohlcv_capped(symbol, tf, start_ms, end_ms, MAX_CANDLES)

    if ohlc.empty:
        raise SystemExit("❌ 시세 데이터가 비었습니다. 심볼/기간/분봉을 확인하세요.")

    ohlc["rsi"] = compute_rsi(ohlc["close"], rsi_period)

    cols = ["datetime","symbol","timeframe","close","rsi","포지션","비고","entry_price","미실현PnL","ROE"]
    log = []

    position = None
    entry_px = None
    qty = None
    init_margin = None

    for i in range(len(ohlc)):
        ts = int(ohlc.loc[i, "ts"]) // 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        px = float(ohlc.loc[i, "close"])
        rv = float(ohlc.loc[i, "rsi"]) if not np.isnan(ohlc.loc[i, "rsi"]) else None
        prev_rv = float(ohlc.loc[i-1, "rsi"]) if i>0 and not np.isnan(ohlc.loc[i-1, "rsi"]) else None

        remark = ""
        pos_name = position if position else "FLAT"
        entry_price = entry_px if entry_px is not None else np.nan
        unreal = 0.0
        roe = 0.0

        if position is None and rv is not None:
            if rv >= OPEN_SHORT_RSI:
                position = "SHORT"; entry_px = px
                notional = equity * leverage
                qty = notional / entry_px
                init_margin = notional / leverage
                remark = "SHORT 진입"; pos_name = position; entry_price = entry_px
            elif rv <= OPEN_LONG_RSI:
                position = "LONG"; entry_px = px
                notional = equity * leverage
                qty = notional / entry_px
                init_margin = notional / leverage
                remark = "LONG 진입"; pos_name = position; entry_price = entry_px

        elif position is not None and rv is not None:
            if position == "LONG":
                unreal = (px - entry_px) * qty
                roe = unreal / init_margin
                # 롱 청산: RSI가 바로 이전봉까지는 < CLOSE_LONG_RSI 이고, 이번 봉에서 >= CLOSE_LONG_RSI 로 '상향 돌파' 했을 때만
                if (prev_rv is not None) and (prev_rv < CLOSE_LONG_RSI) and (rv >= CLOSE_LONG_RSI):
                    remark = f"close (RSI≥{CLOSE_LONG_RSI})"; pos_name = "CLOSE"
                    log.append([dt, symbol, tf, px, rv, pos_name, remark, entry_px, unreal, roe])
                    position = None; entry_px=None; qty=None; init_margin=None
                    continue
            elif position == "SHORT":
                unreal = (entry_px - px) * qty
                roe = unreal / init_margin
                # 숏 청산: RSI가 바로 이전봉까지는 > CLOSE_SHORT_RSI 이고, 이번 봉에서 <= CLOSE_SHORT_RSI 로 '하향 돌파' 했을 때만
                if (prev_rv is not None) and (prev_rv > CLOSE_SHORT_RSI) and (rv <= CLOSE_SHORT_RSI):
                    remark = f"close (RSI≤{CLOSE_SHORT_RSI})"; pos_name = "CLOSE"
                    log.append([dt, symbol, tf, px, rv, pos_name, remark, entry_px, unreal, roe])
                    position = None; entry_px=None; qty=None; init_margin=None
                    continue

        log.append([dt, symbol, tf, px, rv, pos_name, remark, entry_price, unreal, roe])

    df = pd.DataFrame(log, columns=cols)
    os.makedirs(out_dir, exist_ok=True)
    fname = f"{symbol}_{tf}_{rsi_period}.csv"
    path = os.path.join(out_dir, fname)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path

if __name__ == "__main__":
    for s in _as_list(SYMBOL):
        for tf in _as_list(TIMEFRAME):
            for rp in _as_list(RSI_PERIOD):
                csv_path = run(s, tf, rp, LEVERAGE, EQUITY, START, END, OUT_DIR)
                print(f"✅ 저장 완료: {csv_path}")

MAX_CANDLES = None
