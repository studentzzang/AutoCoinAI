from dotenv import load_dotenv, find_dotenv
from pybit.unified_trading import HTTP
import os, sys
import pandas as pd
from datetime import datetime
import time
import hmac, hashlib, requests, json

# ------ GET API KEY -----------------
load_dotenv(find_dotenv(),override=True)

_api_key = os.getenv("API_KEY")
_api_secret = os.getenv("API_KEY_SECRET")

if not _api_key or not _api_secret:
    print("❌ API_KEY 또는 API_KEY_SECRET을 .env에서 못 찾았습니다.")
    print(f"cwd={os.getcwd()}  .env={find_dotenv() or 'NOT FOUND'}")
    sys.exit(1)

session = HTTP(
    api_key=_api_key,
    api_secret=_api_secret,
    recv_window=10000,
    max_retries=0     # ❌ retry 꺼짐
)


# ---- PARAMITER LINE ---- # 이 후 UI개발에 사용
SYMBOL = ["XRPUSDT"]
SYMBOL = [s.strip().upper() for s in SYMBOL]
LEVERAGE = ["2"] #  must be string
PCT     = 50 # 투자비율 n% (후에 심볼 개수 비례도 구현)
MAX_ADD_ENTRIES = 1 # 같은 포지션 추가 진입 최대회수

INTERVAL = 5        # 1 또는 3 권장
LONG_SWITCH_RSI = 30   # 숏 -> 롱 전환 허용 최대 RSI (이하일 때만 스위칭)
SHORT_SWITCH_RSI = 70  # 롱  -> 숏 전환 허용 최소 RSI (이상일 때만 스위칭)

RSI_PERIOD = 12
GAP_LIMIT = 2 #RSI 기준점 찍고 이 만큼 복구 시 진입
COOLDOWN_BARS = 2   # 진입/청산 직후 쉬는 '봉' 수

# --- GLOBAL VARIABLE LINE ---- #

init_regime = None   # "golden" 또는 "dead"
primed = False       # 반대 크로스가 한 번 나와 거래 시작 가능한지
    
position= None
entry_price = None #포지션 진입가
tp_price = None

# ---- FUNC LINE -----
def get_usdt():
    base = "https://api.bybit.com"
    api_key = _api_key.strip()
    api_secret = _api_secret.strip()

    ts = str(int(requests.get(base + "/v5/market/time", timeout=5).json()["result"]["timeSecond"]) * 1000)
    recv = "10000"

    # 서명/요청 모두에 '동일한' 정렬 쿼리스트링 사용
    params = {"accountType": "UNIFIED", "coin": "USDT"}
    canonical = "&".join(f"{k}={params[k]}" for k in sorted(params))  # accountType→coin

    payload = ts + api_key + recv + canonical
    sign = hmac.new(api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "X-BAPI-SIGN": sign,
        "X-BAPI-SIGN-TYPE": "2",
    }

    # 실제 요청도 같은 canonical을 그대로 사용(재정렬 방지)
    url = f"{base}/v5/account/wallet-balance?{canonical}"
    d = requests.get(url, headers=headers, timeout=10).json()

    if d.get("retCode") != 0:
        raise RuntimeError(f"wallet-balance {d.get('retCode')} {d.get('retMsg')} | origin[{payload}]")

    coin = next(c for c in d["result"]["list"][0]["coin"] if c["coin"] == "USDT")
    return float(coin.get("availableToWithdraw") or coin.get("totalAvailableBalance") or coin.get("walletBalance") or coin.get("equity") or 0.0)

print("잔액:",get_usdt())

def set_leverage(symbol, leverage):
    base = "https://api.bybit.com"
    api_key = _api_key.strip()
    api_secret = _api_secret.strip()
    s = str(symbol).strip().upper()
    lev = str(leverage)

    try:
        ts = str(int(requests.get(base + "/v5/market/time", timeout=5).json()["result"]["timeSecond"]) * 1000)
        recv = "10000"
        body = {"category":"linear","symbol":s,"buyLeverage":lev,"sellLeverage":lev}
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        sign = hmac.new(api_secret.encode(), (ts + api_key + recv + payload).encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv,
            "X-BAPI-SIGN": sign,
            "X-BAPI-SIGN-TYPE": "2",
            "Content-Type": "application/json",
        }
        r = requests.post(base + "/v5/position/set-leverage", data=payload, headers=headers, timeout=10).json()
        if r.get("retCode") == 0:
            print(f"✅ {symbol} 레버리지 설정 완료: {leverage}x")
        else:
            print(f"📛 {symbol} 레버리지 에러-> 이미 설정이 되어있습니다.")
            return
    except:
        print(f"📛 {symbol} 레버리지 에러-> 이미 설정이 되어있습니다.")
        return


BYBIT_BASE = "https://api.bybit.com"  # 본계

def get_kline_http(symbol, interval, limit=200, start=None, end=None, timeout=10):
    s = str(symbol).strip().upper()
    iv = str(interval).upper()   # "15", "1", "D" 등
    params = {"category":"linear", "symbol":s, "interval":iv, "limit":int(limit)}
    if start is not None: params["start"] = int(start)
    if end   is not None: params["end"]   = int(end)

    r = requests.get(f"{BYBIT_BASE}/v5/market/kline", params=params, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"/v5/market/kline HTTP {r.status_code}: {r.text}")
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"/v5/market/kline BYBIT {data.get('retCode')} {data.get('retMsg')}: {data}")
    lst = data.get("result", {}).get("list") or []
    if not lst:
        raise RuntimeError(f"/v5/market/kline empty list: {data}")
    return lst[::-1]  # 과거→현재 순서로

# 기존 함수 대체
def get_kline(symbol, interval):
    return get_kline_http(symbol, interval)

def get_PnL(symbol: str):
    base = "https://api.bybit.com"
    api_key = _api_key.strip()
    api_secret = _api_secret.strip()
    s = str(symbol).strip().upper()

    # 서버 시간 → timestamp
    ts = str(int(requests.get(base + "/v5/market/time", timeout=5).json()["result"]["timeSecond"]) * 1000)
    recv = "10000"

    # 쿼리 스트링 (category=linear&symbol=...)
    params = {"category": "linear", "symbol": s}
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))  # category 먼저, 그다음 symbol

    # 서명 (timestamp + api_key + recv_window + queryString)
    payload = ts + api_key + recv + qs
    sign = hmac.new(api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "X-BAPI-SIGN": sign,
        "X-BAPI-SIGN-TYPE": "2",
    }

    url = f"{base}/v5/position/list?{qs}"
    d = requests.get(url, headers=headers, timeout=10).json()

    if d.get("retCode") != 0:
        raise RuntimeError(f"position/list {d.get('retCode')} {d.get('retMsg')} | {d}")

    lst = d.get("result", {}).get("list") or []
    if not lst:
        return 0.0

    return float(lst[0].get("unrealisedPnl", 0.0) or 0.0)

def get_ROE(symbol: str):
    # 먼저 포지션 조회 (requests 기반)
    base = "https://api.bybit.com"
    api_key = _api_key.strip()
    api_secret = _api_secret.strip()
    s = str(symbol).strip().upper()

    ts = str(int(requests.get(base + "/v5/market/time", timeout=5).json()["result"]["timeSecond"]) * 1000)
    recv = "10000"
    params = {"category": "linear", "symbol": s}
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sign = hmac.new(api_secret.encode(), (ts + api_key + recv + qs).encode(), hashlib.sha256).hexdigest()

    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "X-BAPI-SIGN": sign,
        "X-BAPI-SIGN-TYPE": "2",
    }

    url = f"{base}/v5/position/list?{qs}"
    d = requests.get(url, headers=headers, timeout=10).json()
    if d.get("retCode") != 0:
        raise RuntimeError(f"position/list {d.get('retCode')} {d.get('retMsg')} | {d}")

    lst = d.get("result", {}).get("list") or []
    if not lst:
        return 0.0

    pos = lst[0]

    # PnL은 기존 함수 호출
    unrealised_pnl = get_PnL(symbol)

    # IM(Initial Margin) 가져오기
    position_im = float(pos.get("positionIM", 0.0))

    roe_pct = (unrealised_pnl / position_im * 100) if position_im > 0 else 0.0
    return roe_pct


def get_RSI(symbol, interval, period=14):
    kline = get_kline(symbol, interval) 
    closes = [float(k[4]) for k in kline]
    series = pd.Series(closes)

    delta = series.diff()
    up = delta.clip(lower=0)      # 상승폭
    down = -delta.clip(upper=0)   # 하락폭

    # 평균 상승/하락 (Wilder's smoothing)
    avg_gain = up.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = down.ewm(alpha=1/period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))

    return rsi.iloc[-1] 

def get_current_price(symbol, timeout=10):
    s = str(symbol).strip().upper()
    params = {"category": "linear", "symbol": s}
    r = requests.get(f"{BYBIT_BASE}/v5/market/tickers", params=params, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"/v5/market/tickers HTTP {r.status_code}: {r.text}")
    d = r.json()
    if d.get("retCode") != 0:
        raise RuntimeError(f"/v5/market/tickers {d.get('retCode')} {d.get('retMsg')}: {d}")
    lst = d.get("result", {}).get("list") or []
    if not lst:
        raise RuntimeError(f"/v5/market/tickers empty for {s}: {d}")
    return float(lst[0]["lastPrice"])

def get_EMA(symbol, period, interval): # index 0 = EMA(9), 1 = MA(28)
    
    kline = get_kline(symbol, interval)
    
    closes =  [float(k[4]) for k in kline]
    
    series = pd.Series(closes)
    
    ema_latest = series.ewm(span=period, adjust=False, min_periods=period).mean().iloc[-1]
    
    return ema_latest

def get_position_size(symbol): #진입해있는 선물 개수
    base = "https://api.bybit.com"
    api_key = _api_key.strip()
    api_secret = _api_secret.strip()
    s = str(symbol).strip().upper()

    ts = str(int(requests.get(base + "/v5/market/time", timeout=5).json()["result"]["timeSecond"]) * 1000)
    recv = "10000"
    params = {"category":"linear","symbol":s}
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sign = hmac.new(api_secret.encode(), (ts + api_key + recv + qs).encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "X-BAPI-SIGN": sign,
        "X-BAPI-SIGN-TYPE": "2",
    }
    d = requests.get(base + "/v5/position/list?" + qs, headers=headers, timeout=10).json()
    lst = d.get("result", {}).get("list") or []
    if not lst:
        return 0
    size = int(float(lst[0].get("size", "0")))
    return size

    
def get_close_price(symbol, interval):
    # requests 기반 우회 사용 (3개만 가져옴: 닫힌바2 + 진행중1)
    klines = get_kline_http(symbol, interval, limit=3)
    return [float(k[4]) for k in klines]  # [2~3바 전, 1~2바 전, 진행중]

  
def get_BB_middle(symbol, interval, period=20):
    kline = get_kline(symbol, interval)
    closes = [float(k[4]) for k in kline]
    series = pd.Series(closes)
    mb = series.rolling(window=period, min_periods=period).mean().iloc[-1]
    return mb


def get_gap(ema_short, ma_long):
    return abs(ema_short - ma_long)

def entry_position(symbol, leverage, side): #side "Buy"=long, "Sell"=short
    base = "https://api.bybit.com"
    api_key = _api_key.strip()
    api_secret = _api_secret.strip()

    value = get_usdt() * (PCT/ 100) # 구매할 usdt어치
    cur_price = get_current_price(symbol)
    qty = int((value * int(leverage)) / cur_price)

    ts = str(int(requests.get(base + "/v5/market/time", timeout=5).json()["result"]["timeSecond"]) * 1000)
    recv = "10000"
    body = {
        "category":"linear",
        "symbol":str(symbol).strip().upper(),
        "orderType":"Market",
        "qty":str(qty),
        "isLeverage":1,
        "side":side,
        "reduceOnly":False
    }
    payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    sign = hmac.new(api_secret.encode(), (ts + api_key + recv + payload).encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "X-BAPI-SIGN": sign,
        "X-BAPI-SIGN-TYPE": "2",
        "Content-Type": "application/json",
    }
    requests.post(base + "/v5/order/create", data=payload, headers=headers, timeout=10).json()

    print(f"💡[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {symbol} 진입 / 수량 {qty} ({side})")
    return cur_price, qty


    
def close_position(symbol, side): # side "Buy"=short , "Sell"=long
    global entry_price

    qty = get_position_size(symbol=symbol)
    if qty <= 0:
        print("📍 닫을 포지션 없음")
        return

    current_price = get_current_price(symbol)

    if side == "Sell":  # 롱 포지션 청산
        profit_pct = ((current_price - entry_price) / entry_price) * 100
    elif side == "Buy":  # 숏 포지션 청산
        profit_pct = ((entry_price - current_price) / entry_price) * 100
    else:
        profit_pct = 0

    base = "https://api.bybit.com"
    api_key = _api_key.strip()
    api_secret = _api_secret.strip()
    ts = str(int(requests.get(base + "/v5/market/time", timeout=5).json()["result"]["timeSecond"]) * 1000)
    recv = "10000"
    body = {
        "category":"linear",
        "symbol":str(symbol).strip().upper(),
        "orderType":"Market",
        "side":side,
        "reduceOnly":True,
        "isLeverage":1,
        "qty":str(qty),
    }
    payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    sign = hmac.new(api_secret.encode(), (ts + api_key + recv + payload).encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "X-BAPI-SIGN": sign,
        "X-BAPI-SIGN-TYPE": "2",
        "Content-Type": "application/json",
    }
    requests.post(base + "/v5/order/create", data=payload, headers=headers, timeout=10).json()

    print(f"📍[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {symbol} 익절 / 수량 {qty} / 💹 수익률 {profit_pct:.2f}%")

    

# ---- MAIN LOOP ---

def start():
    for i in range(len(SYMBOL)):
        set_leverage(symbol=SYMBOL[i], leverage=LEVERAGE[i])


def update():
    global position, entry_price, tp_price

    last_closed = None
    cooldown = 0
    is_first = True

    last_peak_level = None
    last_trough_level = None

    pending_floor_level = None
    pending_ceiling_level = None

    peaked70_after_entry = peaked75_after_entry = False
    peaked80_after_entry = peaked85_after_entry = False
    dipped30_after_entry = dipped25_after_entry = False
    dipped20_after_entry = dipped15_after_entry = False

    add_count = 0

    while True:
        for i in range(len(SYMBOL)):
            symbol = SYMBOL[i]
            leverage = LEVERAGE[i]
            
            Pnl = get_PnL(symbol)
            ROE = get_ROE(symbol)

            closes3 = get_close_price(symbol, interval=INTERVAL) 
            c_prev2, c_prev1, cur_3 = closes3
            RSI_12 = get_RSI(symbol, interval=INTERVAL, period=RSI_PERIOD)

            if is_first and (RSI_12 >= 65 or RSI_12 <= 35):
                is_first = False
                continue
            is_first = False

            new_bar = (last_closed is None) or (last_closed != c_prev1)
            if new_bar:
                last_closed = c_prev1
                if cooldown > 0:
                    cooldown -= 1

            if RSI_12 >= 85:
                last_peak_level = 85
            elif RSI_12 >= 80:
                last_peak_level = 85 if last_peak_level == 85 else (80 if (last_peak_level is None or last_peak_level < 80) else last_peak_level)
            elif RSI_12 >= 75:
                if last_peak_level is None or last_peak_level < 75:
                    last_peak_level = 75
            elif RSI_12 >= 70:
                if last_peak_level is None or last_peak_level < 70:
                    last_peak_level = 70

            if RSI_12 <= 15:
                last_trough_level = 15
            elif RSI_12 <= 20:
                if (last_trough_level is None) or (last_trough_level > 20):
                    last_trough_level = 20
            elif RSI_12 <= 25:
                if (last_trough_level is None) or (last_trrough_level > 25):
                    last_trrough_level = 25
            elif RSI_12 <= 30:
                if (last_trough_level is None) or (last_trough_level > 30):
                    last_trough_level = 30

            if position is None and cooldown == 0:
                if last_peak_level is not None:
                    short_trigger = last_peak_level - 3
                    if RSI_12 <= short_trigger:
                        px, qty = entry_position(symbol=symbol, side="Sell", leverage=leverage)
                        if qty > 0:
                            position = 'short'
                            entry_price = px
                            tp_price = None
                            cooldown = COOLDOWN_BARS
                            pending_floor_level = None
                            dipped30_after_entry = dipped25_after_entry = dipped20_after_entry = dipped15_after_entry = False
                            last_peak_level = None
                            peaked70_after_entry = peaked75_after_entry = False
                            peaked80_after_entry = peaked85_after_entry = False
                            add_count = 0

                if position is None and last_trough_level is not None and cooldown == 0:
                    long_trigger = last_trough_level + 3
                    if RSI_12 >= long_trigger:
                        px, qty = entry_position(symbol=symbol, side="Buy", leverage=leverage)
                        if qty > 0:
                            position = 'long'
                            entry_price = px
                            tp_price = None
                            cooldown = COOLDOWN_BARS
                            pending_ceiling_level = None
                            peaked70_after_entry = peaked75_after_entry = False
                            peaked80_after_entry = peaked85_after_entry = False
                            last_trough_level = None
                            dipped30_after_entry = dipped25_after_entry = dipped20_after_entry = dipped15_after_entry = False
                            add_count = 0

            elif position == 'short':
                if RSI_12 <= 30:
                    dipped30_after_entry = True
                    pending_floor_level = 30 if pending_floor_level is None else min(pending_floor_level, 30)
                if RSI_12 <= 25:
                    dipped25_after_entry = True
                    pending_floor_level = 25 if pending_floor_level is None else min(pending_floor_level, 25)
                if RSI_12 <= 20:
                    dipped20_after_entry = True
                    pending_floor_level = 20 if pending_floor_level is None else min(pending_floor_level, 20)
                if RSI_12 <= 15:
                    dipped15_after_entry = True
                    pending_floor_level = 15 if pending_floor_level is None else min(pending_floor_level, 15)

                if (add_count < MAX_ADD_ENTRIES) and (RSI_12 <= 30 or RSI_12 <= 25 or RSI_12 <= 20 or RSI_12 <= 15):
                    px2, qty2 = entry_position(symbol=symbol, side="Sell", leverage=leverage)
                    if qty2 > 0:
                        add_count += 1
                        base = "https://api.bybit.com"
                        api_key = _api_key.strip()
                        api_secret = _api_secret.strip()
                        ts = str(int(requests.get(base + "/v5/market/time", timeout=5).json()["result"]["timeSecond"]) * 1000)
                        recv = "10000"
                        params = {"category": "linear", "symbol": str(symbol).strip().upper()}
                        qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
                        sign = hmac.new(api_secret.encode(), (ts + api_key + recv + qs).encode(), hashlib.sha256).hexdigest()
                        headers = {
                            "X-BAPI-API-KEY": api_key,
                            "X-BAPI-TIMESTAMP": ts,
                            "X-BAPI-RECV-WINDOW": recv,
                            "X-BAPI-SIGN": sign,
                            "X-BAPI-SIGN-TYPE": "2",
                        }
                        d = requests.get(base + "/v5/position/list?" + qs, headers=headers, timeout=10).json()
                        lst = d.get("result", {}).get("list") or []
                        if lst:
                            entry_price = float(lst[0].get("avgPrice") or entry_price)

                if pending_floor_level is not None:
                    trigger_up = pending_floor_level + GAP_LIMIT
                    if RSI_12 >= trigger_up:
                        close_position(symbol=symbol, side="Buy")
                        position = None; entry_price = None; tp_price = None
                        cooldown = COOLDOWN_BARS
                        add_count = 0
                        if RSI_12 <= LONG_SWITCH_RSI:
                            px, qty = entry_position(symbol=symbol, side="Buy", leverage=leverage)
                            if qty > 0:
                                position = 'long'
                                entry_price = px
                                tp_price = None
                                cooldown = COOLDOWN_BARS
                                pending_floor_level = None
                                dipped30_after_entry = dipped25_after_entry = dipped20_after_entry = dipped15_after_entry = False
                                last_trough_level = None
                                add_count = 0

            elif position == 'long':
                if RSI_12 >= 70:
                    peaked70_after_entry = True
                    pending_ceiling_level = 70 if pending_ceiling_level is None else max(pending_ceiling_level, 70)
                if RSI_12 >= 75:
                    peaked75_after_entry = True
                    pending_ceiling_level = 75 if pending_ceiling_level is None else max(pending_ceiling_level, 75)
                if RSI_12 >= 80:
                    peaked80_after_entry = True
                    pending_ceiling_level = 80 if pending_ceiling_level is None else max(pending_ceiling_level, 80)
                if RSI_12 >= 85:
                    peaked85_after_entry = True
                    pending_ceiling_level = 85 if pending_ceiling_level is None else max(pending_ceiling_level, 85)

                if (add_count < MAX_ADD_ENTRIES) and (RSI_12 >= 70 or RSI_12 >= 75 or RSI_12 >= 80 or RSI_12 >= 85):
                    px2, qty2 = entry_position(symbol=symbol, side="Buy", leverage=leverage)
                    if qty2 > 0:
                        add_count += 1
                        base = "https://api.bybit.com"
                        api_key = _api_key.strip()
                        api_secret = _api_secret.strip()
                        ts = str(int(requests.get(base + "/v5/market/time", timeout=5).json()["result"]["timeSecond"]) * 1000)
                        recv = "10000"
                        params = {"category": "linear", "symbol": str(symbol).strip().upper()}
                        qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
                        sign = hmac.new(api_secret.encode(), (ts + api_key + recv + qs).encode(), hashlib.sha256).hexdigest()
                        headers = {
                            "X-BAPI-API-KEY": api_key,
                            "X-BAPI-TIMESTAMP": ts,
                            "X-BAPI-RECV-WINDOW": recv,
                            "X-BAPI-SIGN": sign,
                            "X-BAPI-SIGN-TYPE": "2",
                        }
                        d = requests.get(base + "/v5/position/list?" + qs, headers=headers, timeout=10).json()
                        lst = d.get("result", {}).get("list") or []
                        if lst:
                            entry_price = float(lst[0].get("avgPrice") or entry_price)

                if pending_ceiling_level is not None:
                    trigger_down = pending_ceiling_level - GAP_LIMIT
                    if RSI_12 <= trigger_down:
                        close_position(symbol=symbol, side="Sell")
                        position = None; entry_price = None; tp_price = None
                        cooldown = COOLDOWN_BARS
                        add_count = 0
                        if RSI_12 >= SHORT_SWITCH_RSI:
                            px, qty = entry_position(symbol=symbol, side="Sell", leverage=leverage)
                            if qty > 0:
                                position = 'short'
                                entry_price = px
                                tp_price = None
                                cooldown = COOLDOWN_BARS
                                pending_ceiling_level = None
                                peaked70_after_entry = peaked75_after_entry = False
                                peaked80_after_entry = peaked85_after_entry = False
                                last_peak_level = None
                                add_count = 0

            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🪙{symbol} 💲현재가: {cur_3:.5f}$  🚩포지션 {position} | ❣ RSI: {RSI_12:.2f} | 💎Pnl: {Pnl:.3f} ⚜️ROE: {ROE:.2f}")

        time.sleep(5)




start()
update()