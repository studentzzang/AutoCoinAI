#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSI 50 기준 단순화 버전
- 진입: 50±DOORSTEP_ENTRY
- 익절: 50±DOORSTEP_CLOSE 도달 후 peak/trough 기반 CLOSE_BAND 트레일링
- 손절: ARM 전에 RSI가 50 재진입시 손절
- 재진입 차단: 청산 후 RSI가 50을 '통과'하기 전까지 진입 금지
"""

import os
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import numpy as np
from pybit.unified_trading import HTTP

# ====== 사용자 설정 변수 ======
SYMBOL = ["ETHUSDT","1000PEPEUSDT","DOGEUSDT","BTCUSDT"]
LEVERAGE = 5
TIMEFRAME = ["5","15","30","60"]
RSI_PERIOD = [7,9,12, 15]
EQUITY = 100.0
START = "2025-01-01"
END   = "2025-10-12"
OUT_DIR = "tests"
MAX_CANDLES = 10000

# ====== 파라미터 스윕 ======
DOORSTEP_ENTRY_ARR = [5,7,10,14]     # 50±진입 문턱
DOORSTEP_CLOSE_ARR = [16,20,22]   # 50±익절 ARM 문턱
CLOSE_BAND_ARR     = [4]# ARM 후 peak/trough에서 되돌림 폭

REENTRY_UNTIL_RSI = 50.0            # 청산 이후 50 '통과' 전 재진입 금지
COOLDOWN_BARS = 0

session = HTTP()

# ---------- 유틸 ----------
def parse_date(s: Optional[str]) -> Optional[int]:
    if not s: return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s, "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def bybit_interval(tf: str) -> str:
    tf = str(tf).upper()
    mapping = {"1":"1","3":"3","5":"5","15":"15","30":"30",
               "60":"60","120":"120","240":"240","360":"360","720":"720",
               "D":"D","W":"W","M":"M"}
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
            if ts < start_ms: continue
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
def run(symbol: str, tf: str, rsi_period: int, leverage: float, equity: float,
        start: Optional[str], end: Optional[str],
        doorstep_entry: float, doorstep_close: float, close_band: float,
        out_dir: str) -> str:

    start_ms = parse_date(start)
    end_ms = parse_date(end)

    ohlc = fetch_ohlcv_capped(symbol, tf, start_ms, end_ms, MAX_CANDLES)
    if ohlc.empty:
        raise SystemExit("❌ 시세 데이터가 비었습니다. 심볼/기간/분봉을 확인하세요.")

    ohlc["rsi"] = compute_rsi(ohlc["close"], rsi_period)

    cols = ["datetime","symbol","timeframe","close","rsi","포지션","비고","entry_price","미실현PnL","ROE"]
    log = []

    position = None            # 'LONG'/'SHORT'/None
    entry_px = None
    qty = None
    init_margin = None
    cooldown = 0

    # 익절 ARM 상태
    arm_long = False
    arm_short = False
    peak_rsi = None
    trough_rsi = None

    # 재진입 차단
    reentry_block = False
    block_side = None  # 'above' or 'below' (청산 시점의 50 기준 위치)

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

        # 재진입 차단 해제: RSI가 50을 '통과'해야 해제
        if reentry_block and rv is not None:
            if block_side == 'above' and rv <= REENTRY_UNTIL_RSI:
                reentry_block = False; block_side = None
            elif block_side == 'below' and rv >= REENTRY_UNTIL_RSI:
                reentry_block = False; block_side = None
            elif block_side is None and abs(rv - REENTRY_UNTIL_RSI) < 1e-9:
                reentry_block = False

        if cooldown > 0:
            cooldown -= 1

        # ============ FLAT → 진입 ============
        if position is None and rv is not None and cooldown == 0 and (not reentry_block):
            long_trigger = 50.0 + doorstep_entry
            short_trigger = 50.0 - doorstep_entry

            if rv >= long_trigger:
                notional = equity * leverage
                entry_px = px
                qty = notional / entry_px
                init_margin = notional / leverage
                position = "LONG"
                remark = f"LONG 진입 (RSI≥{long_trigger:.1f})"
                pos_name = position; entry_price = entry_px
                arm_long = False; peak_rsi = None
                arm_short = False; trough_rsi = None
                cooldown = COOLDOWN_BARS

            elif rv <= short_trigger:
                notional = equity * leverage
                entry_px = px
                qty = notional / entry_px
                init_margin = notional / leverage
                position = "SHORT"
                remark = f"SHORT 진입 (RSI≤{short_trigger:.1f})"
                pos_name = position; entry_price = entry_px
                arm_short = False; trough_rsi = None
                arm_long = False; peak_rsi = None
                cooldown = COOLDOWN_BARS

        # ============ 보유 중 → 익절/손절 ============
        elif position is not None and rv is not None:
            # 미실현 / ROE
            if position == "LONG":
                unreal = (px - entry_px) * qty
            else:
                unreal = (entry_px - px) * qty
            roe = unreal / init_margin if init_margin else 0.0

            # LONG 포지션
            if position == "LONG":
                # 익절 ARM: 50 + doorstep_close 이상이면 ARM
                tp_arm_level = 50.0 + doorstep_close
                if (not arm_long) and (rv >= tp_arm_level):
                    arm_long = True
                    peak_rsi = rv

                # ARM 전 손절: RSI가 50 재진입하면 손절
                if (not arm_long) and (rv <= 50.0):
                    remark = f"stop LONG (RSI≤50.0)"
                    pos_name = "CLOSE"
                    log.append([dt, symbol, tf, px, rv, pos_name, remark, entry_px, unreal, roe])
                    # 재진입 차단 시작
                    reentry_block = True
                    block_side = 'below' if rv < 50.0 else None
                    # 포지션 종료
                    position = None; entry_px = None; qty = None; init_margin = None
                    arm_long = False; peak_rsi = None
                    cooldown = COOLDOWN_BARS
                    continue

                # ARM 이후 트레일링 익절
                if arm_long:
                    peak_rsi = max(peak_rsi, rv)
                    trigger_down = peak_rsi - close_band
                    if rv <= trigger_down:
                        remark = f"close LONG (RSI≤{trigger_down:.1f})"
                        pos_name = "CLOSE"
                        log.append([dt, symbol, tf, px, rv, pos_name, remark, entry_px, unreal, roe])
                        # 재진입 차단 시작
                        reentry_block = True
                        block_side = 'above' if rv > 50.0 else ('below' if rv < 50.0 else None)
                        # 포지션 종료
                        position = None; entry_px = None; qty = None; init_margin = None
                        arm_long = False; peak_rsi = None
                        cooldown = COOLDOWN_BARS
                        continue

            # SHORT 포지션
            elif position == "SHORT":
                # 익절 ARM: 50 - doorstep_close 이하이면 ARM
                tp_arm_level = 50.0 - doorstep_close
                if (not arm_short) and (rv <= tp_arm_level):
                    arm_short = True
                    trough_rsi = rv

                # ARM 전 손절: RSI가 50 재진입하면 손절
                if (not arm_short) and (rv >= 50.0):
                    remark = f"stop SHORT (RSI≥50.0)"
                    pos_name = "CLOSE"
                    log.append([dt, symbol, tf, px, rv, pos_name, remark, entry_px, unreal, roe])
                    # 재진입 차단 시작
                    reentry_block = True
                    block_side = 'above' if rv > 50.0 else None
                    # 포지션 종료
                    position = None; entry_px = None; qty = None; init_margin = None
                    arm_short = False; trough_rsi = None
                    cooldown = COOLDOWN_BARS
                    continue

                # ARM 이후 트레일링 익절
                if arm_short:
                    trough_rsi = min(trough_rsi, rv)
                    trigger_up = trough_rsi + close_band
                    if rv >= trigger_up:
                        remark = f"close SHORT (RSI≥{trigger_up:.1f})"
                        pos_name = "CLOSE"
                        log.append([dt, symbol, tf, px, rv, pos_name, remark, entry_px, unreal, roe])
                        # 재진입 차단 시작
                        reentry_block = True
                        block_side = 'above' if rv > 50.0 else ('below' if rv < 50.0 else None)
                        # 포지션 종료
                        position = None; entry_px = None; qty = None; init_margin = None
                        arm_short = False; trough_rsi = None
                        cooldown = COOLDOWN_BARS
                        continue

        # 매 행 로깅
        log.append([dt, symbol, tf, px, rv, pos_name, remark, entry_price, unreal, roe])

    df = pd.DataFrame(log, columns=cols)
    os.makedirs(out_dir, exist_ok=True)
    # 기본 파일명(루프에서 리네임)
    fname = f"{symbol}_{tf}_{rsi_period}.csv"
    path = os.path.join(out_dir, fname)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path

if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    for s in _as_list(SYMBOL):
        for tf in _as_list(TIMEFRAME):
            for rp in _as_list(RSI_PERIOD):
                for de in DOORSTEP_ENTRY_ARR:
                    for dc in DOORSTEP_CLOSE_ARR:
                        for cb in CLOSE_BAND_ARR:
                            base_csv_path = run(
                                s, tf, rp, LEVERAGE, EQUITY, START, END,
                                doorstep_entry=de, doorstep_close=dc, close_band=cb,
                                out_dir=OUT_DIR
                            )
                            new_name = f"{s}_{tf}_{rp}_DE{de:g}_DC{dc:g}_CB{cb:g}.csv"
                            new_path = os.path.join(OUT_DIR, new_name)
                            if os.path.exists(new_path):
                                os.remove(new_path)
                            os.replace(base_csv_path, new_path)
                            print(f"✅ 저장: {new_path}")
