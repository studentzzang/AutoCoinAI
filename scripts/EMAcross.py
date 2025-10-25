
import os
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import pandas as pd
pd.set_option('future.no_silent_downcasting', True)

from dotenv import load_dotenv, find_dotenv
import bybit  # 네 로컬 모듈
# =============== 사용자 설정 (심볼별 단일 TF) ===============
SYMBOLS = {
    "PUMPFUNUSDT": {"interval": "30", "fast": 5, "slow": 13},
   
}
LEVERAGE = 5           # 공통 레버리지
PCT      = 50          # 각 심볼당 투자 비중(%), bybit.entry_position 내부에서 사용

TP_ROE   = 7.5         # ROE% 익절
SL_ROE   = 7.5         # ROE% 손절
LOOKBACK = 400         # EMA 계산용 캔들 개수

POLL_SEC = 2.0         # 심볼 간 루프 주기
CALL_GAP = 0.35        # API 호출 간 최소 간격
USE_CURRENT_CANDLE = True   # True: 현재(미확정) 봉 포함 / False: 닫힌 봉만
COOLDOWN_BARS = 0      # 청산 후 N봉 동안 재진입 금지 (깊은 연속거래 방지용, 0이면 비활성)
# ==========================================================
load_dotenv(find_dotenv(), override=True)
# ===== 상태 (심볼 단위) =====
position_side: Dict[str, Optional[str]] = {s: None for s in SYMBOLS}  # "LONG"/"SHORT"/None
entry_price:   Dict[str, Optional[float]] = {s: None for s in SYMBOLS}
qty_map:       Dict[str, Optional[float]] = {s: None for s in SYMBOLS}

# 봉/신호 관리
last_bar_ts:       Dict[str, Optional[int]] = {s: None for s in SYMBOLS}  # 마지막으로 처리한 봉의 ts
last_signal_ts:    Dict[str, Optional[int]] = {s: None for s in SYMBOLS}  # 마지막 "진입 시도"가 일어난 봉 ts (중복 진입 방지)
cooldown_left:     Dict[str, int] = {s: 0 for s in SYMBOLS}              # 청산 후 쿨다운 남은 봉 수

# ===== 유틸 =====
def utc_now_str() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def get_bars(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    """
    bybit.get_kline_http(symbol, interval, limit) 결과를 표준 6컬럼 DF로 정규화
    (ts, open, high, low, close, volume), 시간 오름차순.
    """
    try:
        kl = bybit.get_kline_http(symbol, interval, limit=limit)
    except Exception as e:
        print(f"[ERR] get_kline_http {symbol}@{interval}: {e}")
        return pd.DataFrame(columns=["ts","open","high","low","close","volume"])

    if not kl:
        return pd.DataFrame(columns=["ts","open","high","low","close","volume"])

    first = kl[0]

    if isinstance(first, (list, tuple)):
        n = len(first)
        tmp_cols_all = ["ts","open","high","low","close","volume","turnover","confirm","start","end"]
        tmp_cols = tmp_cols_all[:n]
        df_raw = pd.DataFrame(kl, columns=tmp_cols)
        need = ["ts","open","high","low","close","volume"]
        for c in need:
            if c not in df_raw.columns:
                df_raw[c] = pd.NA
        df = df_raw[need].copy()
    elif isinstance(first, dict):
        rows = []
        for d in kl:
            rows.append({
                "ts":     d.get("ts") or d.get("start") or d.get("startTime") or d.get("timestamp") or d.get("time"),
                "open":   d.get("open"),
                "high":   d.get("high"),
                "low":    d.get("low"),
                "close":  d.get("close"),
                "volume": d.get("volume"),
            })
        df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"])
    else:
        # 예상치 못한 포맷
        return pd.DataFrame(columns=["ts","open","high","low","close","volume"])

    for c in ["ts","open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["ts","close"]).sort_values("ts").reset_index(drop=True)
    return df

def set_leverage_all():
    # bybit 모듈에 비중/심볼 반영 + 레버리지 설정
    bybit.PCT = PCT
    try:
        bybit.SYMBOLS.clear()
    except Exception:
        pass
    for s in SYMBOLS:
        try:
            bybit.SYMBOLS.append(s)
        except Exception:
            pass
        try:
            bybit.set_leverage(symbol=s, leverage=str(LEVERAGE))
        except Exception as e:
            print(f"[WARN] set_leverage({s}) 실패: {e}")
        time.sleep(CALL_GAP)

def enter(symbol: str, side: str, ref_px: float, this_bar_ts: int):
    """시장가 진입 (네 bybit.entry_position 사용) — 동일 봉 중복 진입 방지"""
    # 쿨다운 체크
    if cooldown_left[symbol] > 0:
        return

    # 동일 봉에서 이미 진입했는지 방지
    if last_signal_ts[symbol] == this_bar_ts:
        return

    try:
        price, qty = bybit.entry_position(symbol=symbol,
                                          side=("Buy" if side=="LONG" else "Sell"),
                                          leverage=str(LEVERAGE))
    except Exception as e:
        print(f"[ERR] ENTER {symbol} {side}: {e}")
        return
    if not price or qty <= 0:
        print(f"[WARN] ENTER {symbol} 실패 또는 0수량")
        return

    position_side[symbol] = side
    entry_price[symbol]   = float(price)
    qty_map[symbol]       = float(qty)
    last_signal_ts[symbol]= this_bar_ts  # 이 봉에 이미 진입했음

    # 네 모듈 close 출력 수익률 기준 동기화
    try:
        bybit.entry_px[symbol] = float(price)
    except Exception:
        pass

    print(f"[{utc_now_str()}] 🟢 ENTER {symbol} {side} qty={qty} @~{price:.6f}")

def close(symbol: str, reason: str, this_bar_ts: int):
    """시장가 reduceOnly 청산 — 청산 후 동일 봉 재진입 방지 + 쿨다운 시작"""
    side = position_side[symbol]
    if side is None:
        return
    try:
        bybit.close_position(symbol=symbol, side=("Sell" if side=="LONG" else "Buy"))
    except Exception as e:
        print(f"[ERR] CLOSE {symbol} {side}: {e}")
        return

    print(f"[{utc_now_str()}] 🔴 CLOSE {symbol} {reason}")
    position_side[symbol] = None
    entry_price[symbol]   = None
    qty_map[symbol]       = None

    # 같은 봉 재진입 방지
    last_signal_ts[symbol] = this_bar_ts
    # 쿨다운 시작
    if COOLDOWN_BARS > 0:
        cooldown_left[symbol] = COOLDOWN_BARS

def handle_symbol(symbol: str):
    # 설정 로드
    cfg  = SYMBOLS[symbol]
    tf   = cfg["interval"]
    fast = int(cfg["fast"])
    slow = int(cfg["slow"])
    if fast >= slow:
        print(f"[WARN] {symbol} EMA 설정 오류 (fast={fast}, slow={slow})")
        return

    # 캔들 로드
    df = get_bars(symbol, tf, limit=max(LOOKBACK, slow + 10))
    if df.empty or len(df) < slow + 5:
        print(f"[SKIP] {symbol}@{tf}: 캔들 부족")
        return

    # 봉 선택 (현재 봉 포함/제외)
    bars = df.copy() if USE_CURRENT_CANDLE else (df.iloc[:-1].copy() if len(df) > 1 else df.copy())
    this_bar_ts = int(bars["ts"].iloc[-1])
    price       = float(bars["close"].iloc[-1])

    # 봉 변경 감지 → 쿨다운 카운터 감소
    if last_bar_ts[symbol] is None:
        last_bar_ts[symbol] = this_bar_ts
    elif this_bar_ts != last_bar_ts[symbol]:
        last_bar_ts[symbol] = this_bar_ts
        if cooldown_left[symbol] > 0:
            cooldown_left[symbol] -= 1

    # EMA & 교차
    close_ser = bars["close"].astype(float)
    ef = ema(close_ser, fast)
    es = ema(close_ser, slow)

    above      = (ef > es).fillna(False).astype(bool)
    above_prev = above.shift(1).fillna(False).astype(bool)
    cross_up   = (~above_prev) & (above)     # 골든
    cross_dn   = (above_prev) & (~above)     # 데드

    # 진입/반대교차 평가는 매 봉 1회만
    opp_reason = None

    # 진입
    if position_side[symbol] is None:
        if bool(cross_up.iloc[-1]):
            enter(symbol, "LONG", price, this_bar_ts)
        elif bool(cross_dn.iloc[-1]):
            enter(symbol, "SHORT", price, this_bar_ts)
    else:
        # 반대교차는 보호 청산 후보
        if position_side[symbol] == "LONG" and bool(cross_dn.iloc[-1]):
            opp_reason = "XC LONG"
        if position_side[symbol] == "SHORT" and bool(cross_up.iloc[-1]):
            opp_reason = "XC SHORT"

    # 보유 중이면 TP/SL/반대교차 체크
    side = position_side[symbol]
    if side is not None:
        try:
            pnl = bybit.get_PnL(symbol)
        except Exception as e:
            print(f"[ERR] get_PnL {symbol}: {e}"); pnl = 0.0
        try:
            roe = bybit.get_ROE(symbol)
        except Exception as e:
            print(f"[ERR] get_ROE {symbol}: {e}"); roe = 0.0

        do_close = None
        reason   = None
        if roe >= TP_ROE:
            do_close, reason = True, f"TP {side}"
        elif roe <= -SL_ROE:
            do_close, reason = True, f"SL {side}"
        elif opp_reason is not None:
            do_close, reason = True, opp_reason

        if do_close:
            close(symbol, reason, this_bar_ts)

        # 출력용 현재가(진행중 봉)
        try:
            _p2, _p1, cur = bybit.get_close_price(symbol, interval=tf)
            last_px = cur
        except Exception:
            last_px = price

        print(f"[{utc_now_str()}] 🪙{symbol} @{tf} "
              f"💲현재가: {last_px:.6f} 🚩포지션 {position_side[symbol]} "
              f"| EMA{fast}/{slow} = {float(ef.iloc[-1]):.6f}/{float(es.iloc[-1]):.6f} "
              f"| 💎PnL: {pnl:.6f} ⚜️ROE: {roe:.2f}% "
              f"| ⏳CD:{cooldown_left[symbol]}")
    else:
        print(f"[{utc_now_str()}] 🪙{symbol} @{tf} "
              f"💲현재가: {price:.6f} 🚩포지션 None "
              f"| EMA{fast}/{slow} = {float(ef.iloc[-1]):.6f}/{float(es.iloc[-1]):.6f} "
              f"| ⏳CD:{cooldown_left[symbol]}")

def main():
    set_leverage_all()
    print(f"▶ EMA Cross 실거래 시작 (lev={LEVERAGE}x, TP={TP_ROE}%, SL={SL_ROE}%, alloc={PCT}%, "
          f"use_current={USE_CURRENT_CANDLE}, cooldown_bars={COOLDOWN_BARS})")

    while True:
        for s in SYMBOLS.keys():
            handle_symbol(s)
            time.sleep(CALL_GAP)
        time.sleep(POLL_SEC)

if __name__ == "__main__":
    main()
