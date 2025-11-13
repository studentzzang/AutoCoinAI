from dotenv import load_dotenv, find_dotenv
from pybit.unified_trading import HTTP
import os, json, time
import pandas as pd
from datetime import datetime
from decimal import Decimal

# ====== 환경 설정 ======
load_dotenv(find_dotenv(), override=True)
_api_key = os.getenv("API_KEY")
_api_secret = os.getenv("API_KEY_SECRET")

# ====== Bybit 세션 생성 (recv_window 60초 설정) ======
session = HTTP(
    testnet=False,
    api_key=_api_key,
    api_secret=_api_secret,
    recv_window=60000
)

BYBIT_BASE = "https://api.bybit.com"

# -- 실행 코드에서 할당
PCT = 0
SYMBOLS = []
entry_px = {s: None for s in SYMBOLS}


# ====== 함수 정의 ======

def get_usdt():
    d = session.get_wallet_balance(accountType="UNIFIED")
    coin = next(c for c in d["result"]["list"][0]["coin"] if c["coin"] == "USDT")
    return float(coin.get("equity"))


def set_leverage(symbol, leverage):
    try:
        r = session.set_leverage(
            category="linear",
            symbol=str(symbol).upper(),
            buyLeverage=str(leverage),
            sellLeverage=str(leverage),
        )
        print(f"✅ {symbol} 레버리지 설정 완료: {leverage}x")
    except Exception as e:
        print(f"📛 {symbol} 레버리지 이미 설정 되었습니다.")


def get_kline_http(symbol, interval, limit=200):
    r = session.get_kline(
        category="linear",
        symbol=str(symbol).upper(),
        interval=str(interval),
        limit=int(limit),
    )
    return r["result"]["list"][::-1]


def get_kline(symbol, interval):
    return get_kline_http(symbol, interval)


def get_PnL(symbol):
    try:
        r = session.get_positions(category="linear", symbol=str(symbol).upper())
        lst = r.get("result", {}).get("list") or []
        if not lst:
            return 0.0
        v = lst[0].get("unrealisedPnl")
        return float(v) if v not in ("", None) else 0.0
    except Exception as e:
        print(f"📛 get_PnL 오류: {e}")
        return 0.0


def get_ROE(symbol):
    try:
        r = session.get_positions(category="linear", symbol=str(symbol).upper())
        lst = r.get("result", {}).get("list") or []
        if not lst:
            return 0.0
        pos = lst[0]
        unreal = float(pos.get("unrealisedPnl", 0) or 0)
        position_im = float(pos.get("positionIM", 0) or 0)
        return (unreal / position_im * 100) if position_im > 0 else 0.0
    except Exception as e:
        print(f"📛 get_ROE 오류: {e}")
        return 0.0


def get_RSI(symbol, interval, period=14):
    closes = [float(k[4]) for k in get_kline(symbol, interval)]
    series = pd.Series(closes)
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    avg_gain = up.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = down.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def get_current_price(symbol):
    s = str(symbol).upper()
    r = session.get_tickers(category="linear", symbol=s)
    lst = r["result"]["list"]
    if not lst:
        r = session.get_tickers(category="spot", symbol=s)
        lst = r["result"]["list"]
        if not lst:
            raise RuntimeError(f"/market/tickers empty for {s}")
    return float(lst[0]["lastPrice"])


def get_position_size(symbol):
    r = session.get_positions(category="linear", symbol=str(symbol).upper())
    lst = r.get("result", {}).get("list") or []
    return 0.0 if not lst else float(lst[0].get("size", 0) or 0.0)


def get_close_price(symbol, interval):
    kl = get_kline_http(symbol, interval, limit=3)
    return [float(k[4]) for k in kl]


def get_lot_size(symbol):
    r = session.get_instruments_info(category="linear", symbol=str(symbol).upper())
    lot = r["result"]["list"][0]["lotSizeFilter"]
    min_qty = float(lot["minOrderQty"])
    step = float(lot["qtyStep"])
    return min_qty, step


def quantize_qty(qty, step):
    q = Decimal(str(qty))
    s = Decimal(str(step))
    return float((q // s) * s)


def entry_position(symbol, leverage, side):
    try:
        min_qty, step = get_lot_size(symbol)
    except Exception as e:
        print(f"📛[{datetime.now().strftime('%H:%M:%S')}] {symbol} lotInfo 실패: {e}")
        return None, 0

    avail = get_usdt()
    price = get_current_price(symbol)
    raw_qty = (avail * (PCT / 100) * int(leverage)) / price
    adj_qty = quantize_qty(raw_qty, step)

    if adj_qty < min_qty:
        print(f"📛 수량 부족: raw={raw_qty:.8f}, adj={adj_qty:.8f}, min={min_qty}")
        return None, 0

    r = session.place_order(
        category="linear",
        symbol=str(symbol).upper(),
        side=side,
        orderType="Market",
        qty=str(adj_qty),
        isLeverage=1,
        reduceOnly=False,
    )
    if r.get("retCode") != 0:
        print(f"📛 주문 실패: {r.get('retMsg')}")
        return None, 0

    print(f"💡 {symbol} {side} 진입 | 수량 {adj_qty} | lev={leverage} | price={price:.6f}")
    return price, adj_qty


def close_position(symbol, side):
    qty = get_position_size(symbol)
    if qty <= 0:
        print("📍 닫을 포지션 없음")
        return

    current_price = get_current_price(symbol)
    ep = entry_px.get(symbol)
    profit_pct = ((current_price - ep) / ep * 100) if ep else 0.0

    r = session.place_order(
        category="linear",
        symbol=str(symbol).upper(),
        side=side,
        orderType="Market",
        qty=str(qty),
        isLeverage=1,
        reduceOnly=True,
    )
    print(f"📍 {symbol} 포지션 종료 / 수량 {qty} / 💹 수익률 {profit_pct:.2f}%")

