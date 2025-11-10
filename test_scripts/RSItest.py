import os
import time
from datetime import datetime, timezone
from typing import Optional
import pandas as pd
import numpy as np
from pybit.unified_trading import HTTP

# ====================== 사용자 설정 ======================
SYMBOL = ["PUMPFUNUSDT","FARTCOINUSDT"]
LEVERAGE = 10
TIMEFRAME = ["1","5","15","30"]
RSI_PERIOD = [7,9,12,14]
EQUITY = 100.0
START = "2025-01-01"
END = "2025-11-03"
OUT_DIR = "test"
MAX_CANDLES = 35000  # 더 오래 가져오려면 None

# RSI 진입 조건
LONG_SWITCH_RSI = 28.0
SHORT_SWITCH_RSI = 72.0

# 진입 / 청산 문턱 배열 
DOORSTEP_ENTRY_ARR = [3, 5, 7, 9]
DOORSTEP_CLOSE_ARR = [2, 3, 4]
COOLDOWN_BARS = 0

# TP/SL
TP_ROE_ARR = [5, 10, 15]
SL_ROE_ARR = [5, 10, 15]

# TP_MODE: 1~3
# 1 = TP만 의존
# 2 = TP 후 RSI 반대구간 진입시 익절
# 3 = RSI로만 청산
TP_MODE_ARR = [1, 2, 3]

TIME_WAIT = 0.1

session = HTTP()

# ====================== 유틸 ======================
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
    mapping = {
        "1":"1","3":"3","5":"5","15":"15","30":"30",
        "60":"60","120":"120","240":"240","720":"720",
        "D":"D","W":"W","M":"M"
    }
    if tf not in mapping:
        raise ValueError(f"지원하지 않는 분봉: {tf}")
    return mapping[tf]

def fetch_ohlcv_capped(symbol, tf, start_ms, end_ms, max_candles):
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
            rows.append((ts, float(it[1]), float(it[2]), float(it[3]), float(it[4]), float(it[5])))

        cur_end = min(int(x[0]) for x in lst) - 1
        if len(lst) < req_limit:
            break
        time.sleep(TIME_WAIT)

    if not rows:
        return pd.DataFrame(columns=["ts","open","high","low","close","volume"])

    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"]).drop_duplicates("ts")
    df.sort_values("ts", inplace=True)
    if max_candles:
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

# ====================== 시뮬 ======================
def run(symbol, tf, rsi_period, leverage, equity, start, end, out_dir, tp_roe, sl_roe, tp_mode, doorstep_entry, doorstep_close):
    start_ms = parse_date(start)
    end_ms = parse_date(end)

    ohlc = fetch_ohlcv_capped(symbol, tf, start_ms, end_ms, MAX_CANDLES)
    if ohlc.empty:
        raise SystemExit(f"❌ 데이터 없음 ({symbol}, {tf})")

    ohlc["rsi"] = compute_rsi(ohlc["close"], rsi_period)
    cols = ["datetime","symbol","timeframe","close","rsi","포지션","비고","entry_price","미실현PnL","ROE"]
    log = []

    position = None
    entry_px = qty = init_margin = None
    cooldown = 0
    armed_short_switch = armed_long_switch = False
    last_peak_level = last_trough_level = None

    for i in range(len(ohlc)):
        ts = int(ohlc.loc[i, "ts"]) // 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        px = float(ohlc.loc[i, "close"])
        rv = float(ohlc.loc[i, "rsi"]) if not np.isnan(ohlc.loc[i, "rsi"]) else None

        remark = ""
        pos_name = position if position else "FLAT"
        entry_price = entry_px if entry_px else np.nan
        unreal = 0.0
        roe = 0.0

        # arm
        if rv is not None:
            if rv <= LONG_SWITCH_RSI: armed_long_switch = True
            if rv >= SHORT_SWITCH_RSI: armed_short_switch = True
            if rv >= 80: last_peak_level = max(last_peak_level or 0, 80)
            if rv <= 20: last_trough_level = min(last_trough_level or 100, 20)

        if cooldown > 0:
            cooldown -= 1

        # === 진입 ===
        if position is None and rv is not None and cooldown == 0:
            if (last_peak_level is not None) and armed_short_switch:
                short_trigger = last_peak_level - doorstep_entry
                if rv <= short_trigger:
                    notional = equity * leverage
                    entry_px = px
                    qty = notional / entry_px
                    init_margin = notional / leverage
                    position = "SHORT"
                    remark = f"SHORT 진입 (RSI≤{short_trigger:.1f})"
                    cooldown = COOLDOWN_BARS
                    armed_short_switch = False

            elif (last_trough_level is not None) and armed_long_switch:
                long_trigger = last_trough_level + doorstep_entry
                if rv >= long_trigger:
                    notional = equity * leverage
                    entry_px = px
                    qty = notional / entry_px
                    init_margin = notional / leverage
                    position = "LONG"
                    remark = f"LONG 진입 (RSI≥{long_trigger:.1f})"
                    cooldown = COOLDOWN_BARS
                    armed_long_switch = False

        # === 보유 중 ===
        elif position is not None and rv is not None:
            if position == "LONG":
                unreal = (px - entry_px) * qty
                roe = unreal / init_margin * 100 if init_margin else 0.0

                if roe <= -sl_roe:
                    remark = f"close LONG (SL {roe:.1f}%)"
                    position = None
                elif roe >= tp_roe:
                    if tp_mode == 1:
                        remark = f"close LONG (TP {roe:.1f}%)"
                        position = None
                    elif tp_mode == 2:
                        if rv >= SHORT_SWITCH_RSI - doorstep_close:
                            remark = f"close LONG (RSI 과매수, TP모드2)"
                            position = None
                    elif tp_mode == 3:
                        if rv >= SHORT_SWITCH_RSI - doorstep_close:
                            remark = f"close LONG (RSI 청산, TP모드3)"
                            position = None

            elif position == "SHORT":
                unreal = (entry_px - px) * qty
                roe = unreal / init_margin * 100 if init_margin else 0.0

                if roe <= -sl_roe:
                    remark = f"close SHORT (SL {roe:.1f}%)"
                    position = None
                elif roe >= tp_roe:
                    if tp_mode == 1:
                        remark = f"close SHORT (TP {roe:.1f}%)"
                        position = None
                    elif tp_mode == 2:
                        if rv <= LONG_SWITCH_RSI + doorstep_close:
                            remark = f"close SHORT (RSI 과매도, TP모드2)"
                            position = None
                    elif tp_mode == 3:
                        if rv <= LONG_SWITCH_RSI + doorstep_close:
                            remark = f"close SHORT (RSI 청산, TP모드3)"
                            position = None

        # FLAT 완전 제거 — 진입/청산 결과만 저장
        if remark and ("진입" in remark or "close" in remark):
            log.append([dt, symbol, tf, px, rv, position if position else "FLAT", remark, entry_px, unreal, roe])

    df = pd.DataFrame(log, columns=cols)
    os.makedirs(out_dir, exist_ok=True)
    fname = f"{symbol}_{tf}_{rsi_period}_TP{tp_roe}_SL{sl_roe}_MODE{tp_mode}_EN{doorstep_entry}_CL{doorstep_close}.csv"
    path = os.path.join(out_dir, fname)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path

# ====================== 실행 ======================
if __name__ == "__main__":
    for s in _as_list(SYMBOL):
        for tf in _as_list(TIMEFRAME):
            for rp in _as_list(RSI_PERIOD):
                for tp in TP_ROE_ARR:
                    for sl in SL_ROE_ARR:
                        for tm in TP_MODE_ARR:
                            for de in DOORSTEP_ENTRY_ARR:
                                for dc in DOORSTEP_CLOSE_ARR:
                                    csv_path = run(s, tf, rp, LEVERAGE, EQUITY,
                                                START, END, OUT_DIR,
                                                tp, sl, tm, de, dc)
                                    print(f"✅ 저장 완료: {csv_path}")
