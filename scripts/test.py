# RSI 거래법 그대로@ -1,355 +0,0 @@
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
SYMBOL = ["ETHUSDT"]                # 심볼 배열 (네가 수정)
LEVERAGE = 5                         # 레버리지
TIMEFRAME = ["30"]                   # 분봉/주기 배열 ("1","3","5","15","30","60","120","240","720","D","W","M")
RSI_PERIOD = [7,12]                     # RSI 기간 배열 (여러 개면 각각 파일 생성)
EQUITY = 100.0                       # 가정 자본(USDT)
START = "2025-01-01"                 # 시작일
END   = "2025-10-12"                 # 종료일
OUT_DIR = "tests"                    # 저장 경로
MAX_CANDLES = 10000                  # 최대 캔들 수 (None 무제한)

# ====== 네 “원래 방법” 핵심 파라미터 ======
LONG_SWITCH_RSI  = 28.0    # 이 값 이하 찍히면 롱 스위치 arm
SHORT_SWITCH_RSI = 72.0    # 이 값 이상 찍히면 숏 스위치 arm
DOORSTEP_ENTRY = 7   # 진입 트리거용 되돌림(레벨±이 값)
DOORSTEP_CLOSE = 3   # 청산 트리거용 되돌림(레벨±이 값)
ENTRY_BAND     = 4   # 진입 허용 밴드(트리거 주변 슬랙)

COOLDOWN_BARS    = 0       # 진입/청산 후 대기 봉 수

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

# ---------- 시뮬 ----------
def run(symbol: str, tf: str, rsi_period: int, leverage: float, equity: float,
        start: Optional[str], end: Optional[str], out_dir: str) -> str:
    start_ms = parse_date(start)
    end_ms = parse_date(end)

    ohlc = fetch_ohlcv_capped(symbol, tf, start_ms, end_ms, MAX_CANDLES)
    if ohlc.empty:
        raise SystemExit("❌ 시세 데이터가 비었습니다. 심볼/기간/분봉을 확인하세요.")

    # 단일 RSI (실시간 로직처럼 같은 RSI로 판단)
    ohlc["rsi"] = compute_rsi(ohlc["close"], rsi_period)

    cols = ["datetime","symbol","timeframe","close","rsi","포지션","비고","entry_price","미실현PnL","ROE"]
    log = []

    # 상태 변수
    position = None           # 'LONG'/'SHORT'/None
    entry_px = None
    qty = None
    init_margin = None
    cooldown = 0

    # 스위치/레벨/대기 레벨
    armed_short_switch = False
    armed_long_switch  = False
    last_peak_level    = None   # 72/75/80/84
    last_trough_level  = None   # 30/27/25/20/15
    pending_floor_lvl  = None   # 숏 보유 시 바닥 후보
    pending_ceil_lvl   = None   # 롱 보유 시 천장 후보
    max_rsi_since_ent  = None
    min_rsi_since_ent  = None

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

        # 스위치 arm
        if rv is not None:
            if rv <= LONG_SWITCH_RSI:
                armed_long_switch = True
            if rv >= SHORT_SWITCH_RSI:
                armed_short_switch = True

            # 피크/트로프 레벨 갱신 (실시간 로직과 동일 단계)
            if rv >= 84: last_peak_level = 84
            elif rv >= 80: last_peak_level = max(last_peak_level or 0, 80)
            elif rv >= 75: last_peak_level = max(last_peak_level or 0, 75)
            elif rv >= 72: last_peak_level = max(last_peak_level or 0, 72)
            elif rv >= 68: last_peak_level = max(last_peak_level or 0, 68)

            if rv <= 15: last_trough_level = 15
            elif rv <= 20: last_trough_level = 20 if (last_trough_level is None) else min(last_trough_level, 20)
            elif rv <= 25: last_trough_level = 25 if (last_trough_level is None) else min(last_trough_level, 25)
            elif rv <= 27: last_trough_level = 27 if (last_trough_level is None) else min(last_trough_level, 27)
            elif rv <= 30: last_trough_level = 30 if (last_trough_level is None) else min(last_trough_level, 30)
            elif rv <= 34: last_trough_level = 32 if (last_trough_level is None) else min(last_trough_level, 32)  # 네 코드의 32 주석 반영

        # 쿨다운
        if cooldown > 0:
            cooldown -= 1

        # ===== 무포지션 → 진입 =====
        if position is None and rv is not None and cooldown == 0:
            # 숏: last_peak_level 존재 + 숏 스위치 armed + 트리거(peak-3) 부근(ENTRY_BAND)
            if (last_peak_level is not None) and armed_short_switch:
                short_trigger = last_peak_level - DOORSTEP_ENTRY
                if (rv <= short_trigger) and (rv >= short_trigger - ENTRY_BAND):
    

                    # 진입
                    notional = equity * leverage
                    entry_px = px
                    qty = notional / entry_px
                    init_margin = notional / leverage
                    position = "SHORT"
                    remark = f"SHORT 진입 (RSI≤{short_trigger:.1f})"
                    pos_name = position; entry_price = entry_px
                    cooldown = COOLDOWN_BARS
                    pending_floor_lvl = None
                    last_peak_level = None
                    armed_short_switch = False
                    max_rsi_since_ent = None
                    armed_long_switch = (rv <= LONG_SWITCH_RSI)
                    min_rsi_since_ent = rv

            # 롱: last_trough_level 존재 + 롱 스위치 armed + 트리거(trough+3) 부근(ENTRY_BAND)
            elif (last_trough_level is not None) and armed_long_switch:
                long_trigger = last_trough_level + DOORSTEP_ENTRY
                if (rv >= long_trigger) and (rv <= long_trigger + ENTRY_BAND):
                    #롱진입
                    notional = equity * leverage
                    entry_px = px
                    qty = notional / entry_px
                    init_margin = notional / leverage
                    position = "LONG"
                    remark = f"LONG 진입 (RSI≥{long_trigger:.1f})"
                    pos_name = position; entry_price = entry_px
                    cooldown = COOLDOWN_BARS
                    pending_ceil_lvl = None
                    last_trough_level = None
                    armed_long_switch = False
                    min_rsi_since_ent = None
                    armed_short_switch = (rv >= SHORT_SWITCH_RSI)
                    max_rsi_since_ent = rv

        # ===== 보유 중 → 청산/리버스 가능 =====
        elif position is not None and rv is not None:
            if position == "LONG":
                unreal = (px - entry_px) * qty
                roe = unreal / init_margin if init_margin else 0.0

                # 천장 후보 갱신
                if rv >= 70: pending_ceil_lvl = 70 if pending_ceil_lvl is None else max(pending_ceil_lvl, 70)
                if rv >= 75: pending_ceil_lvl = 75 if pending_ceil_lvl is None else max(pending_ceil_lvl, 75)
                if rv >= 80: pending_ceil_lvl = 80 if pending_ceil_lvl is None else max(pending_ceil_lvl, 80)
                if rv >= 85: pending_ceil_lvl = 85 if pending_ceil_lvl is None else max(pending_ceil_lvl, 85)

                max_rsi_since_ent = rv if (max_rsi_since_ent is None or rv > max_rsi_since_ent) else max_rsi_since_ent
                if rv >= SHORT_SWITCH_RSI: armed_short_switch = True

                if pending_ceil_lvl is not None:
                    # 천장 후보가 있을 때
                    trigger_down = pending_ceil_lvl - DOORSTEP_CLOSE
                    if (rv <= trigger_down):
                        # CLOSE LONG ...

                        # 롱 청산
                        remark = f"close LONG (RSI≤{trigger_down:.1f})"
                        pos_name = "CLOSE"
                        log.append([dt, symbol, tf, px, rv, pos_name, remark, entry_px, unreal, roe])
                        cooldown = COOLDOWN_BARS
                        # 조건부 숏 전환
                        if armed_short_switch and rv < SHORT_SWITCH_RSI:
                            # 즉시 숏 진입
                            notional = equity * leverage
                            entry_px = px
                            qty = notional / entry_px
                            init_margin = notional / leverage
                            position = "SHORT"
                            remark = f"SHORT 진입 (전환)"
                            pos_name = position; entry_price = entry_px
                            pending_ceil_lvl = None
                            armed_short_switch = False
                            max_rsi_since_ent = None
                            armed_long_switch = (rv <= LONG_SWITCH_RSI)
                            min_rsi_since_ent = rv
                        else:
                            # 포지션 종료
                            position = None
                            entry_px = None; qty = None; init_margin = None
                            pending_ceil_lvl = None
                            armed_short_switch = False
                            max_rsi_since_ent = None
                            last_peak_level = None
                        # 로그는 위에서 이미 찍었으니 다음 루프로
                        continue

            elif position == "SHORT":
                unreal = (entry_px - px) * qty
                roe = unreal / init_margin if init_margin else 0.0

                # 바닥 후보 갱신
                if rv <= 30: pending_floor_lvl = 30 if pending_floor_lvl is None else min(pending_floor_lvl, 30)
                if rv <= 25: pending_floor_lvl = 25 if pending_floor_lvl is None else min(pending_floor_lvl, 25)
                if rv <= 20: pending_floor_lvl = 20 if pending_floor_lvl is None else min(pending_floor_lvl, 20)
                if rv <= 15: pending_floor_lvl = 15 if pending_floor_lvl is None else min(pending_floor_lvl, 15)

                min_rsi_since_ent = rv if (min_rsi_since_ent is None or rv < min_rsi_since_ent) else min_rsi_since_ent
                if rv <= LONG_SWITCH_RSI: armed_long_switch = True

                if pending_floor_lvl is not None:
                    # 바닥 후보가 있을 때
                    trigger_up = pending_floor_lvl + DOORSTEP_CLOSE
                    if (rv >= trigger_up):
                        # CLOSE SHORT ...

                        # 숏 청산
                        remark = f"close SHORT (RSI≥{trigger_up:.1f})"
                        pos_name = "CLOSE"
                        log.append([dt, symbol, tf, px, rv, pos_name, remark, entry_px, unreal, roe])
                        cooldown = COOLDOWN_BARS
                        # 조건부 롱 전환
                        if armed_long_switch and rv >= LONG_SWITCH_RSI:
                            notional = equity * leverage
                            entry_px = px
                            qty = notional / entry_px
                            init_margin = notional / leverage
                            position = "LONG"
                            remark = f"LONG 진입 (전환)"
                            pos_name = position; entry_price = entry_px
                            pending_floor_lvl = None
                            armed_long_switch = False
                            min_rsi_since_ent = None
                            armed_short_switch = (rv >= SHORT_SWITCH_RSI)
                            max_rsi_since_ent = rv
                        else:
                            # 포지션 종료
                            position = None
                            entry_px = None; qty = None; init_margin = None
                            pending_floor_lvl = None
                            armed_long_switch = False
                            min_rsi_since_ent = None
                            last_trough_level = None
                        continue

        # 매 행 로깅(컬럼/순서 그대로)
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
