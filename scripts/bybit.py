from dotenv import load_dotenv, find_dotenv
from pybit.unified_trading import HTTP
import os, sys
import pandas as pd
from datetime import datetime
import time
from math import floor, isclose
import hmac, hashlib, requests, json
from decimal import Decimal

load_dotenv(find_dotenv(),override=True)
_api_key = os.getenv("API_KEY"); _api_secret = os.getenv("API_KEY_SECRET")

BYBIT_BASE = "https://api.bybit.com"

# -- 실행 코드에서 할당
PCT      = 0    # 코인별 투자 비중(%)
SYMBOLS = []
entry_px  = {s: None for s in SYMBOLS}

def get_usdt():
    base="https://api.bybit.com"; api_key=_api_key.strip(); api_secret=_api_secret.strip()
    ts=str(int(requests.get(base+"/v5/market/time",timeout=5).json()["result"]["timeSecond"])*1000); recv="10000"
    params={"accountType":"UNIFIED","coin":"USDT"}
    canonical="&".join(f"{k}={params[k]}" for k in sorted(params))
    payload=ts+api_key+recv+canonical
    sign=hmac.new(api_secret.encode(),payload.encode(),hashlib.sha256).hexdigest()
    headers={"X-BAPI-API-KEY":api_key,"X-BAPI-TIMESTAMP":ts,"X-BAPI-RECV-WINDOW":recv,"X-BAPI-SIGN":sign,"X-BAPI-SIGN-TYPE":"2"}
    d=requests.get(f"{base}/v5/account/wallet-balance?{canonical}",headers=headers,timeout=10).json()
    if d.get("retCode")!=0: raise RuntimeError(f"wallet-balance {d.get('retCode')} {d.get('retMsg')}")
    coin=next(c for c in d["result"]["list"][0]["coin"] if c["coin"]=="USDT")
    return float(coin.get("equity"))

def set_leverage(symbol, leverage):
    base="https://api.bybit.com"; api_key=_api_key.strip(); api_secret=_api_secret.strip()
    s=str(symbol).strip().upper(); lev=str(leverage)
    try:
        ts=str(int(requests.get(base+"/v5/market/time",timeout=5).json()["result"]["timeSecond"])*1000); recv="10000"
        body={"category":"linear","symbol":s,"buyLeverage":lev,"sellLeverage":lev}
        payload=json.dumps(body,separators=(",",":"),ensure_ascii=False)
        sign=hmac.new(api_secret.encode(),(ts+api_key+recv+payload).encode(),hashlib.sha256).hexdigest()
        headers={"X-BAPI-API-KEY":api_key,"X-BAPI-TIMESTAMP":ts,"X-BAPI-RECV-WINDOW":recv,"X-BAPI-SIGN":sign,"X-BAPI-SIGN-TYPE":"2","Content-Type":"application/json"}
        r=requests.post(base+"/v5/position/set-leverage",data=payload,headers=headers,timeout=10).json()
        if r.get("retCode")==0: print(f"✅ {symbol} 레버리지 설정 완료: {leverage}x")
        else: 
            print(f"📛 {symbol} 레버리지 에러: 이미 {leverage}x 설정됨")
    except Exception as e:
        print(f"📛 {symbol} 레버리지 에러: {e}")

def get_kline_http(symbol, interval, limit=200, start=None, end=None, timeout=10):
    s=str(symbol).strip().upper(); iv=str(interval).upper()
    params={"category":"linear","symbol":s,"interval":iv,"limit":int(limit)}
    if start is not None: params["start"]=int(start)
    if end   is not None: params["end"]=int(end)
    r=requests.get(f"{BYBIT_BASE}/v5/market/kline",params=params,timeout=timeout)
    if r.status_code!=200: raise RuntimeError(f"/v5/market/kline HTTP {r.status_code}: {r.text}")
    data=r.json(); lst=data.get("result",{}).get("list") or []
    return lst[::-1]

def get_kline(symbol, interval):
    return get_kline_http(symbol, interval)

def get_PnL(symbol):
    base="https://api.bybit.com"; api_key=_api_key.strip(); api_secret=_api_secret.strip(); s=str(symbol).strip().upper()
    ts=str(int(time.time()*1000)); recv="30000"
    params={"category":"linear","symbol":s}; qs="&".join(f"{k}={params[k]}" for k in sorted(params))
    payload=ts+api_key+recv+qs; sign=hmac.new(api_secret.encode(),payload.encode(),hashlib.sha256).hexdigest()
    headers={"X-BAPI-API-KEY":api_key,"X-BAPI-TIMESTAMP":ts,"X-BAPI-RECV-WINDOW":recv,"X-BAPI-SIGN":sign,"X-BAPI-SIGN-TYPE":"2"}
    d=requests.get(f"{base}/v5/position/list?{qs}",headers=headers,timeout=10).json()
    if d.get("retCode")!=0: raise RuntimeError(f"position/list {d.get('retCode')} {d.get('retMsg')}")
    lst=d.get("result",{}).get("list") or []
    if not lst: return 0.0
    v = lst[0].get("unrealisedPnl")
    if v in ("", None): return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0

def get_ROE(symbol):
    base="https://api.bybit.com"; api_key=_api_key.strip(); api_secret=_api_secret.strip(); s=str(symbol).strip().upper()
    ts=str(int(time.time()*1000)); recv="30000"
    params={"category":"linear","symbol":s}; qs="&".join(f"{k}={params[k]}" for k in sorted(params))
    sign=hmac.new(api_secret.encode(),(ts+api_key+recv+qs).encode(),hashlib.sha256).hexdigest()
    headers={"X-BAPI-API-KEY":api_key,"X-BAPI-TIMESTAMP":ts,"X-BAPI-RECV-WINDOW":recv,"X-BAPI-SIGN":sign,"X-BAPI-SIGN-TYPE":"2"}
    d=requests.get(f"{base}/v5/position/list?{qs}",headers=headers,timeout=10).json()
    if d.get("retCode")!=0: raise RuntimeError(f"position/list {d.get('retCode')} {d.get('retMsg')}")
    lst=d.get("result",{}).get("list") or []
    if not lst: return 0.0
    pos=lst[0]
    unreal=get_PnL(symbol)
    im = pos.get("positionIM")
    if im in ("", None): return 0.0
    try:
        position_im=float(im)
    except (TypeError, ValueError):
        position_im=0.0
    return (unreal/position_im*100) if position_im>0 else 0.0

def get_RSI(symbol, interval, period=14):
    closes=[float(k[4]) for k in get_kline(symbol, interval)]
    series=pd.Series(closes); delta=series.diff()
    up=delta.clip(lower=0); down=-delta.clip(upper=0)
    avg_gain=up.ewm(alpha=1/period,adjust=False).mean()
    avg_loss=down.ewm(alpha=1/period,adjust=False).mean()
    rs=avg_gain/avg_loss.replace(0,1e-10); rsi=100-(100/(1+rs))
    return float(rsi.iloc[-1])

def get_current_price(symbol, timeout=10):
    s=str(symbol).strip().upper()
    params={"category":"linear","symbol":s}
    r=requests.get(f"{BYBIT_BASE}/v5/market/tickers",params=params,timeout=timeout).json()
    lst=r.get("result",{}).get("list") or []
    if not lst:
        params={"category":"spot","symbol":s}
        r=requests.get(f"{BYBIT_BASE}/v5/market/tickers",params=params,timeout=timeout).json()
        lst=r.get("result",{}).get("list") or []
        if not lst: raise RuntimeError(f"/v5/market/tickers empty for {s}: {r}")
    return float(lst[0]["lastPrice"])

def get_position_size(symbol):
    base="https://api.bybit.com"; api_key=_api_key.strip(); api_secret=_api_secret.strip(); s=str(symbol).strip().upper()
    ts=str(int(requests.get(base+"/v5/market/time",timeout=5).json()["result"]["timeSecond"])*1000); recv="10000"
    params={"category":"linear","symbol":s}; qs="&".join(f"{k}={params[k]}" for k in sorted(params))
    sign=hmac.new(api_secret.encode(),(ts+api_key+recv+qs).encode(),hashlib.sha256).hexdigest()
    headers={"X-BAPI-API-KEY":api_key,"X-BAPI-TIMESTAMP":ts,"X-BAPI-RECV-WINDOW":recv,"X-BAPI-SIGN":sign,"X-BAPI-SIGN-TYPE":"2"}
    d = requests.get(base+"/v5/position/list?"+qs, headers=headers, timeout=10).json()
    lst = d.get("result",{}).get("list") or []
    return 0.0 if not lst else float(lst[0].get("size","0") or 0.0)  # 소수 유지

def get_close_price(symbol, interval):
    kl=get_kline_http(symbol, interval, limit=3)
    return [float(k[4]) for k in kl]  # [2~3바 전, 1~2바 전, 진행중]

def get_lot_size(symbol: str):
    """심볼별 최소수량/스텝 조회 (선물 Linear)"""
    r = requests.get(
        f"{BYBIT_BASE}/v5/market/instruments-info",
        params={"category": "linear", "symbol": str(symbol).upper()},
        timeout=10
    ).json()
    if r.get("retCode") != 0 or not r.get("result", {}).get("list"):
        raise RuntimeError(f"instruments-info {r.get('retCode')} {r.get('retMsg')}")
    lot = r["result"]["list"][0]["lotSizeFilter"]
    min_qty = float(lot["minOrderQty"])
    step    = float(lot["qtyStep"])
    return min_qty, step

def quantize_qty(qty: float, step: float) -> float:
    """qty를 step의 배수로 내림 정규화"""
    q = Decimal(str(qty))
    s = Decimal(str(step))
    return float((q // s) * s)

def entry_position(symbol, leverage, side):
    base = "https://api.bybit.com"
    api_key = _api_key.strip()
    api_secret = _api_secret.strip()

    # ① 심볼별 최소/스텝 가져와서
    try:
        min_qty, step = get_lot_size(symbol)
    except Exception as e:
        print(f"📛[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {symbol} lotInfo 실패: {e}")
        return None, 0

    # ② 가용증거금 기준 원시 수량 계산
    avail = get_usdt()
    price = get_current_price(symbol)
    raw_qty = (avail * (PCT/100) * int(leverage)) / price

    # ③ 스텝에 맞춰 내림 정규화
    adj_qty = quantize_qty(raw_qty, step)

    # ④ 최소수량 미만이면 주문 불가
    if adj_qty < min_qty:
        print(
            f"📛[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {symbol} 수량 부족: "
            f"raw={raw_qty:.8f} → adj={adj_qty:.8f}, min={min_qty}, step={step}"
        )
        return None, 0

    ts = str(int(requests.get(base + "/v5/market/time", timeout=5).json()["result"]["timeSecond"]) * 1000)
    recv = "10000"
    body = {
        "category": "linear",
        "symbol": str(symbol).strip().upper(),
        "orderType": "Market",
        "qty": str(adj_qty),       # ← 심볼 규칙에 맞춘 수량
        "isLeverage": 1,
        "side": side,
        "reduceOnly": False
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
    resp = requests.post(base + "/v5/order/create", data=payload, headers=headers, timeout=10).json()
    if resp.get("retCode") != 0:
        print(
            f"📛[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {symbol} 주문 실패: "
            f"{resp.get('retCode')} {resp.get('retMsg')} | qty={adj_qty} (min={min_qty}, step={step})"
        )
        return None, 0

    print(
        f"💡[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {symbol} 진입 / 수량 {adj_qty} ({side})"
        f" | avail={avail:.2f} price={price:.6f} lev={leverage}"
    )
    return price, adj_qty

def close_position(symbol, side):
    qty=get_position_size(symbol=symbol)
    if qty<=0:
        print("📍 닫을 포지션 없음"); return
    current_price=get_current_price(symbol); ep=entry_px.get(symbol)
    if ep:
        profit_pct=((current_price-ep)/ep*100) if side=="Sell" else ((ep-current_price)/ep*100)
    else:
        profit_pct=0.0
    base="https://api.bybit.com"; api_key=_api_key.strip(); api_secret=_api_secret.strip()
    ts=str(int(requests.get(base+"/v5/market/time",timeout=5).json()["result"]["timeSecond"])*1000); recv="10000"
    body={"category":"linear","symbol":str(symbol).strip().upper(),"orderType":"Market","side":side,"reduceOnly":True,"isLeverage":1,"qty":str(qty)}
    payload=json.dumps(body,separators=(",",":"),ensure_ascii=False)
    sign=hmac.new(api_secret.encode(),(ts+api_key+recv+payload).encode(),hashlib.sha256).hexdigest()
    headers={"X-BAPI-API-KEY":api_key,"X-BAPI-TIMESTAMP":ts,"X-BAPI-RECV-WINDOW":recv,"X-BAPI-SIGN":sign,"X-BAPI-SIGN-TYPE":"2","Content-Type":"application/json"}
    requests.post(base+"/v5/order/create",data=payload,headers=headers,timeout=10).json()
    print(f"📍[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {symbol} 익절 / 수량 {qty} / 💹 수익률 {profit_pct:.2f}%")
