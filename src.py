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
SYMBOL = ["PUMPFUNUSDT"]
SYMBOL = [s.strip().upper() for s in SYMBOL]
LEVERAGE = ["2"] #  must be string
PCT     = 25 # 투자비율 n% (후에 심볼 개수 비례도 구현)

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
    global position, entry_price, tp_price  # tp_price는 건드리지 않지만 전역은 유지
    
    is_first = True

    SELL_COOLDOWN = 60 #익절, 손절 후 쿨타임
    INTERVAL = 15 # 분봉

    # 상태 플래그: 포지션 진입 후 RSI 임계 통과 여부
    dipped20_after_entry = {s: False for s in SYMBOL}
    dipped30_after_entry = {s: False for s in SYMBOL}

    peaked70_after_entry = {s: False for s in SYMBOL}
    peaked80_after_entry = {s: False for s in SYMBOL}


    # 바 교체 감지용(최근 닫힌 캔들의 종가)
    last_closed_map = {s: None for s in SYMBOL}

    while True:
        for i in range(len(SYMBOL)):
            symbol = SYMBOL[i]
            leverage = LEVERAGE[i]

            # === 지표/가격 ===
            EMA_9  = get_EMA(symbol, interval=INTERVAL, period=9)
            BB_MID = get_BB_middle(symbol, interval=INTERVAL, period=20)

            closes3 = get_close_price(symbol, interval=INTERVAL)  # [2~3바 전, 1~2바 전, 진행중]
            c_prev2 = closes3[0]
            c_prev1 = closes3[1]  # 가장 최근에 닫힌 캔들의 종가
            cur_3   = closes3[2]  # 진행 중 캔들(실시간)

            RSI_14 = get_RSI(symbol, interval=INTERVAL, period=14)
            
            if (RSI_14 >= 65 or RSI_14 <=35) and is_first:
                is_first=False
                continue

            # === 바 교체 감지 ===
            new_bar = (last_closed_map[symbol] is None) or (last_closed_map[symbol] != c_prev1)
            if new_bar:
                last_closed_map[symbol] = c_prev1
                
            # == 횡보장 / 과구간 진입 방지 ==
            if position is None and ((48 <= RSI_14 <= 52) or (RSI_14 >= 70 or RSI_14 <= 30)):
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Wait")
                continue

            # =======================
            # 포지션 보유 시: 익절 로직(사용자 지정)
            # =======================
            if position == 'short':
              # ---- 바닥 레벨 기록: 20 우선, 아니면 30 ----
              if RSI_14 <= 20:
                  dipped20_after_entry[symbol] = True
                  dipped30_after_entry[symbol] = False
                  time.sleep(20)
              elif RSI_14 <= 30 and not dipped20_after_entry[symbol]:
                  dipped30_after_entry[symbol] = True
                  time.sleep(20)


              # ---- 되돌림 시 청산: 20이 최우선, 아니면 30 ----
              if (
                  (dipped20_after_entry[symbol] and RSI_14 > 20)  # 20 찍고 20 회복
                  #or (dipped30_after_entry[symbol] and RSI_14 > 30)  # 30 찍고 30 회복
                  or (EMA_9 > BB_MID) or (EMA_9 >= 49)  # 보조장치, 손절
              ):
                  close_position(symbol=symbol, side="Buy")
                  position = None; entry_price = None; tp_price = None
                  dipped20_after_entry[symbol] = False
                  dipped30_after_entry[symbol] = False
                  peaked70_after_entry[symbol] = False
                  peaked80_after_entry[symbol] = False
                  time.sleep(SELL_COOLDOWN)


                    
            elif position == 'long':
              # ---- 피크 레벨 기록: 80 우선, 아니면 70 ----
              if RSI_14 >= 80:
                  peaked80_after_entry[symbol] = True
                  peaked70_after_entry[symbol] = False
                  
                  time.sleep(20)
                  
              elif RSI_14 >= 70 and not peaked80_after_entry[symbol]:
                  peaked70_after_entry[symbol] = True
                  
                  time.sleep(20)

              # ---- 되돌림 시 청산: 80이 최우선, 아니면 70 ----
              if (
                  (peaked80_after_entry[symbol] and RSI_14 < 80)  # 80 찍고 80 하회
                  #or (peaked70_after_entry[symbol] and RSI_14 < 70)  # 70 찍고 70 하회
                  or (EMA_9 < BB_MID) or (EMA_9<=51)  # 보조장치,  손절
              ):
                  close_position(symbol=symbol, side="Sell")
                  position = None; entry_price = None; tp_price = None
                  peaked80_after_entry[symbol] = False
                  peaked70_after_entry[symbol] = False
                  dipped20_after_entry[symbol] = False
                  dipped30_after_entry[symbol] = False
                  time.sleep(SELL_COOLDOWN)



            # =======================
            # 빈 포지션: 진입 (닫힌 바 기준으로만)
            # =======================
            if position is None and new_bar:
                # 숏 진입
                if (
                    (EMA_9 < BB_MID  and 36 <= RSI_14 <= 46 and get_gap(EMA_9, BB_MID) >= 0.0004 * c_prev1)
                    and (cur_3 <= EMA_9 and c_prev1 <= EMA_9 and c_prev2<=EMA_9)
                ):
                    px, qty = entry_position(symbol=symbol, side="Sell", leverage=leverage)
                    if qty > 0:
                        position = 'short'
                        entry_price = px
                        tp_price = None
                        # 사용 중인 플래그만 초기화
                        dipped20_after_entry[symbol] = False
                        dipped30_after_entry[symbol] = False
                        peaked70_after_entry[symbol] = False
                        peaked80_after_entry[symbol] = False

                # 롱 진입
                elif (
                    (EMA_9 > BB_MID and 62 >= RSI_14 >= 54 and get_gap(EMA_9, BB_MID) >= 0.0004 * c_prev1)
                    and (cur_3 >= EMA_9 and c_prev1 >= EMA_9 and c_prev2 >=EMA_9)
                ):
                    px, qty = entry_position(symbol=symbol, side="Buy", leverage=leverage)
                    if qty > 0:
                        position = 'long'
                        entry_price = px
                        tp_price = None
                        # 사용 중인 플래그만 초기화
                        peaked70_after_entry[symbol] = False
                        peaked80_after_entry[symbol] = False
                        dipped20_after_entry[symbol] = False
                        dipped30_after_entry[symbol] = False



            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🪙 {symbol} 💲 현재가: {cur_3}$  🚩 포지션 {position} /  📶 EMA(9): {EMA_9:.6f}  BB: {BB_MID:.6f} | ❣ RSI: {RSI_14}")

        time.sleep(10)


start()
update()