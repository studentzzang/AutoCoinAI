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
SYMBOL = ["AVAXUSDT"]
LEVERAGE = 5            # 레버리지
TIMEFRAME = [15,30,60]   # 1h
RSI_PERIOD = [7, 10, 14]
EQUITY = 100.0         # 가정 자본(USDT)
START = "2025-02-01"    # 시작일 (없으면 최대 과거)
END = "2025-09-18"      # 종료일 (없으면 현재)
OUT_DIR = "tests"  # 저장 경로

# 트리거 값 (원하는 대로 수정 가능)
OPEN_SHORT_RSI = 72.0   # SHORT 진입 RSI 이상
OPEN_LONG_RSI = 28.0    # LONG 진입 RSI 이하
CLOSE_SHORT_RSI = 70.0  # SHORT 청산 RSI 이하
CLOSE_LONG_RSI = 30.0   # LONG 청산 RSI 이상
# ===========================

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

def fetch_ohlcv_10000(symbol: str, tf: str, start_ms=None, end_ms=None) -> pd.DataFrame:
    interval = bybit_interval(tf)
    if end_ms is None:
        end_ms = int(datetime.now(tz=timezone.utc).timestamp()*1000)

    rows = []
    while len(rows) < 10000:
        resp = session.get_kline(
            category="linear",
            symbol=symbol,
            interval=interval,
            end=end_ms,
            limit=1000
        )
        if resp.get("retCode") != 0:
            raise RuntimeError(resp.get("retMsg"))
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
    return df.head(10000)


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
        start: Optional[str], end: Optional[str], out_dir: str) -> str:
    start_ms = parse_date(start)
    end_ms = parse_date(end)

    ohlc = fetch_ohlcv_10000(symbol, tf, start_ms, end_ms)
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
                if rv >= CLOSE_LONG_RSI:
                    remark = "close (RSI≥{CLOSE_LONG_RSI})"; pos_name = "CLOSE"
                    log.append([dt, symbol, tf, px, rv, pos_name, remark, entry_px, unreal, roe])
                    position = None; entry_px=None; qty=None; init_margin=None
                    continue
            elif position == "SHORT":
                unreal = (entry_px - px) * qty
                roe = unreal / init_margin
                if rv <= CLOSE_SHORT_RSI:
                    remark = "close (RSI≤{CLOSE_SHORT_RSI})"; pos_name = "CLOSE"
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