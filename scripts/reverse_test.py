#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import numpy as np
from pybit.unified_trading import HTTP

# ====== 사용자 설정 변수 ======
SYMBOLS   = ["PUMPFUNUSDT", "ETHUSDT"]  # 여러 코인
TIMEFRAMES = ["5", "30", "60"]           # 여러 분봉(문자열로)

LEVERAGE  = 5
EQUITY    = 100.0
START     = "2025-01-01"
END       = "2025-09-20"
OUT_DIR   = "tests"
MAX_CANDLES = 10000  # None이면 무제한

# ====== 설정 (역RSI) ======
ENTRY_RSI_PERIOD = 12      # 진입판정용(짧게)
CLOSE_RSI_PERIOD = 12     # 청산판정용(길게)

OPEN_SHORT_RSI  = 41.0    # SHORT 진입: entry_RSI ≤ 이 값
CLOSE_SHORT_RSI = 30.0    # SHORT 청산: close_RSI 하향 돌파 ≤ 이 값 (더 작아졌을 때)

OPEN_LONG_RSI   = 59.0    # LONG  진입: entry_RSI ≥ 이 값
CLOSE_LONG_RSI  = 70.0    # LONG  청산: close_RSI 상향 돌파 ≥ 이 값 (더 커졌을 때)

# 값 일관성 체크(역RSI 규칙)
if not (CLOSE_SHORT_RSI < OPEN_SHORT_RSI):
    raise ValueError("CLOSE_SHORT_RSI는 OPEN_SHORT_RSI보다 작아야 합니다(역RSI).")
if not (CLOSE_LONG_RSI > OPEN_LONG_RSI):
    raise ValueError("CLOSE_LONG_RSI는 OPEN_LONG_RSI보다 커야 합니다(역RSI).")

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
        req_limit = int(min(1000, cap - len(rows)))
        resp = session.get_kline(
            category="linear",
            symbol=symbol,
            interval=interval,
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

# ---------- 시뮬 ----------
def run(symbol: str, tf: str, entry_rsi_period: int, close_rsi_period: int,
        leverage: float, equity: float,
        start: Optional[str], end: Optional[str], out_dir: str) -> str:

    start_ms = parse_date(start)
    end_ms = parse_date(end)

    ohlc = fetch_ohlcv_capped(symbol, tf, start_ms, end_ms, MAX_CANDLES)
    if ohlc.empty:
        raise SystemExit("❌ 시세 데이터가 비었습니다. 심볼/기간/분봉을 확인하세요.")

    # 두 개 RSI 생성
    ohlc["rsi_entry"] = compute_rsi(ohlc["close"], entry_rsi_period)
    ohlc["rsi_close"] = compute_rsi(ohlc["close"], close_rsi_period)

    cols = ["datetime","symbol","timeframe","close","rsi_entry","rsi_close","포지션","비고","entry_price","미실현PnL","ROE"]
    log = []

    position = None
    entry_px = None
    qty = None
    init_margin = None

    for i in range(len(ohlc)):
        ts = int(ohlc.loc[i, "ts"]) // 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        px = float(ohlc.loc[i, "close"])

        rsi_e = float(ohlc.loc[i, "rsi_entry"]) if not np.isnan(ohlc.loc[i, "rsi_entry"]) else None
        rsi_c = float(ohlc.loc[i, "rsi_close"]) if not np.isnan(ohlc.loc[i, "rsi_close"]) else None

        prev_rsi_c = float(ohlc.loc[i-1, "rsi_close"]) if i>0 and not np.isnan(ohlc.loc[i-1, "rsi_close"]) else None

        remark = ""
        pos_name = position if position else "FLAT"
        entry_price = entry_px if entry_px is not None else np.nan
        unreal = 0.0
        roe = 0.0

                # 포지션 없을 때: "진입 RSI"로만 진입 판단
        if position is None and rsi_e is not None:
            # 역RSI: SHORT는 기준 '이하'에서 진입
            if rsi_e <= OPEN_SHORT_RSI:
                position = "SHORT"; entry_px = px
                notional = equity * leverage
                qty = notional / entry_px
                init_margin = notional / leverage
                remark = f"SHORT 진입 (entryRSI≤{OPEN_SHORT_RSI})"
                pos_name = position; entry_price = entry_px

            # 역RSI: LONG는 기준 '이상'에서 진입
            elif rsi_e >= OPEN_LONG_RSI:
                position = "LONG"; entry_px = px
                notional = equity * leverage
                qty = notional / entry_px
                init_margin = notional / leverage
                remark = f"LONG 진입 (entryRSI≥{OPEN_LONG_RSI})"
                pos_name = position; entry_price = entry_px

        # 포지션 있을 때: "청산 RSI" 크로스로만 청산(휩쓸림 방지)
        elif position is not None and rsi_c is not None:
            if position == "LONG":
                unreal = (px - entry_px) * qty
                roe = unreal / init_margin if init_margin else 0.0
                # LONG 청산: closeRSI가 '상향 돌파'로 더 커졌을 때
                if (prev_rsi_c is not None) and (prev_rsi_c < CLOSE_LONG_RSI) and (rsi_c >= CLOSE_LONG_RSI):
                    remark = f"CLOSE LONG (closeRSI↑≥{CLOSE_LONG_RSI})"
                    pos_name = "CLOSE"
                    log.append([dt, symbol, tf, rsi_e, rsi_c, pos_name, remark, entry_px, unreal, roe])
                    position = None; entry_px=None; qty=None; init_margin=None
                    continue

            elif position == "SHORT":
                unreal = (entry_px - px) * qty
                roe = unreal / init_margin if init_margin else 0.0
                # SHORT 청산: closeRSI가 '하향 돌파'로 더 작아졌을 때
                if (prev_rsi_c is not None) and (prev_rsi_c > CLOSE_SHORT_RSI) and (rsi_c <= CLOSE_SHORT_RSI):
                    remark = f"CLOSE SHORT (closeRSI↓≤{CLOSE_SHORT_RSI})"
                    pos_name = "CLOSE"
                    log.append([dt, symbol, tf, rsi_e, rsi_c, pos_name, remark, entry_px, unreal, roe])
                    position = None; entry_px=None; qty=None; init_margin=None
                    continue


        log.append([dt, symbol, tf, px, rsi_e, rsi_c, pos_name, remark, entry_price, unreal, roe])

    df = pd.DataFrame(log, columns=cols)
    os.makedirs(out_dir, exist_ok=True)
    fname = f"{symbol}_{tf}.csv"
    path = os.path.join(out_dir, fname)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path

if __name__ == "__main__":
    for s in _as_list(SYMBOLS):
        for tf in _as_list(TIMEFRAMES):
            csv_path = run(
                s, tf,
                ENTRY_RSI_PERIOD, CLOSE_RSI_PERIOD,
                LEVERAGE, EQUITY,
                START, END, OUT_DIR
            )
            print(f"✅ 저장 완료: {csv_path}")
