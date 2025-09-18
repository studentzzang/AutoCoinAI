from dotenv import load_dotenv, find_dotenv
from pybit.unified_trading import HTTP
import os, sys
import pandas as pd
from datetime import datetime
import time
from math import floor, isclose
import hmac, hashlib, requests, json
from decimal import Decimal

# ------ GET API KEY -----------------
load_dotenv(find_dotenv(),override=True)
_api_key = os.getenv("API_KEY"); _api_secret = os.getenv("API_KEY_SECRET")
if not _api_key or not _api_secret:
    print("❌ API_KEY 또는 API_KEY_SECRET을 .env에서 못 찾았습니다.")
    print(f"cwd={os.getcwd()}  .env={find_dotenv() or 'NOT FOUND'}"); sys.exit(1)

session = HTTP(api_key=_api_key, api_secret=_api_secret, recv_window=10000, max_retries=0)

# ---- USER PARAMS (리스트 3개) ----
SYMBOLS      = ["SOLUSDT","XRPUSDT","1000PEPEUSDT"]   # 심볼 목록
RSI_PERIODS  = [7,         7,       6]         # 각 심볼별 RSI 기간
INTERVALS    = [30,      "30",   "D"]     # 각 심볼별 인터벌 ("1","3","15","60","240","D"...)

# 길이 검사
if not (len(SYMBOLS)==len(RSI_PERIODS)==len(INTERVALS)):
    print("❌ SYMBOLS/RSI_PERIODS/INTERVALS 길이가 다릅니다."); sys.exit(1)

LEVERAGE = "7"   # 모든 심볼 동일 레버리지(문자열)
PCT      = 50    # 코인별 투자 비중(%)
LONG_SWITCH_RSI  = 28
SHORT_SWITCH_RSI = 72
ENTRY_BAND = 4
COOLDOWN_BARS = 0
BYBIT_BASE = "https://api.bybit.com"

# =========================
# 심볼별 상태(dict)로 완전 분리
# =========================
position  = {s: None for s in SYMBOLS}  # 'long'/'short'/None
entry_px  = {s: None for s in SYMBOLS}
tp_price  = {s: None for s in SYMBOLS}
last_peak_level    = {s: None for s in SYMBOLS}
last_trough_level  = {s: None for s in SYMBOLS}
pending_floor_lvl  = {s: None for s in SYMBOLS}
pending_ceil_lvl   = {s: None for s in SYMBOLS}
armed_short_switch = {s: False for s in SYMBOLS}
armed_long_switch  = {s: False for s in SYMBOLS}
max_rsi_since_ent  = {s: None for s in SYMBOLS}
min_rsi_since_ent  = {s: None for s in SYMBOLS}
last_closed_price1 = {s: None for s in SYMBOLS}
cooldown_bars      = {s: 0    for s in SYMBOLS}

# ---- FUNCs ----
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
    return float(
    coin.get("equity") 
    )
print("잔액:", get_usdt())  

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

def get_kline(symbol, interval): return get_kline_http(symbol, interval)

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
    if qty<=0: print("📍 닫을 포지션 없음"); return
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

# ---- MAIN ----
# 전역
BASE_CASH = None

def start():
    global BASE_CASH
    BASE_CASH = get_usdt()
    print(f"🔧 기준가용(스냅샷): {BASE_CASH:.2f} USDT")
    for s in SYMBOLS:
        set_leverage(symbol=s, leverage=LEVERAGE)

def update():
    prev_rsi={s: None for s in SYMBOLS}
    while True:
        for idx, symbol in enumerate(SYMBOLS):
            try:
                rsi_period = RSI_PERIODS[idx]
                interval   = INTERVALS[idx]
                leverage   = LEVERAGE  # 동일 레버리지

                # PnL/ROE (심볼별)
                Pnl=get_PnL(symbol); ROE=get_ROE(symbol)

                # 시세/RSI (심볼별 interval/period)
                c_prev2,c_prev1,cur_3=get_close_price(symbol, interval=interval)
                RSI=get_RSI(symbol, interval=interval, period=rsi_period)

                # ✱ 스위치 먼저 찍기(arm) — 포지션과 무관하게 기록 (임계값 변경 없음)
                if RSI <= LONG_SWITCH_RSI:
                    armed_long_switch[symbol] = True
                if RSI >= SHORT_SWITCH_RSI:
                    armed_short_switch[symbol] = True

                # 봉 교체/쿨다운
                new_bar=(last_closed_price1[symbol] is None) or (last_closed_price1[symbol]!=c_prev1)
                if new_bar:
                    last_closed_price1[symbol]=c_prev1
                    if cooldown_bars[symbol]>0: cooldown_bars[symbol]-=1

                # ===== 레벨 갱신 (심볼별 RSI) — 네 원본 그대로 =====
                if RSI>=84: last_peak_level[symbol]=84
                elif RSI>=80:
                    if last_peak_level[symbol] is None or last_peak_level[symbol]<80: last_peak_level[symbol]=80
                elif RSI>=75:
                    if last_peak_level[symbol] is None or last_peak_level[symbol]<75: last_peak_level[symbol]=75
                elif RSI>=72:
                    if last_peak_level[symbol] is None or last_peak_level[symbol]<72: last_peak_level[symbol]=72
                elif RSI>=68:
                    if last_peak_level[symbol] is None or last_peak_level[symbol]<68: last_peak_level[symbol]=68
                
                if RSI<=20: last_trough_level[symbol]=20
                elif RSI<=25:
                    if (last_trough_level[symbol] is None) or (last_trough_level[symbol]>25): last_trough_level[symbol]=25
                elif RSI<=27:
                    if (last_trough_level[symbol] is None) or (last_trough_level[symbol]>27): last_trough_level[symbol]=27
                elif RSI<=30:
                    if (last_trough_level[symbol] is None) or (last_trough_level[symbol]>30): last_trough_level[symbol]=30
                elif RSI<=32:
                    if (last_trough_level[symbol] is None) or (last_trough_level[symbol]>34): last_trough_level[symbol]=34
                
                # ===== 무포지션 → 진입 (스위치 먼저 찍고 ±3 되돌림 구간만 허용) =====
                if position[symbol] is None and cooldown_bars[symbol]==0:
                    # 숏
                    if last_peak_level[symbol] is not None and armed_short_switch[symbol]:
                        short_trigger=last_peak_level[symbol]-3
                        if (RSI <= short_trigger) and (RSI >= short_trigger - ENTRY_BAND):
                            px,qty=entry_position(symbol=symbol, side="Sell", leverage=leverage)
                            if qty>0 and px is not None:
                                position[symbol]='short'; entry_px[symbol]=px; tp_price[symbol]=None
                                cooldown_bars[symbol]=COOLDOWN_BARS; pending_floor_lvl[symbol]=None
                                last_peak_level[symbol]=None
                                armed_short_switch[symbol]=False          # 사용한 스위치 소모
                                max_rsi_since_ent[symbol]=None
                                armed_long_switch[symbol]=(RSI<=LONG_SWITCH_RSI)
                                min_rsi_since_ent[symbol]=RSI; prev_rsi[symbol]=RSI; continue
                    # 롱
                    if position[symbol] is None and last_trough_level[symbol] is not None and cooldown_bars[symbol]==0 and armed_long_switch[symbol]:
                        long_trigger=last_trough_level[symbol]+3
                        if (RSI >= long_trigger) and (RSI <= long_trigger + ENTRY_BAND):
                            px,qty=entry_position(symbol=symbol, side="Buy", leverage=leverage)
                            if qty>0 and px is not None:
                                position[symbol]='long'; entry_px[symbol]=px; tp_price[symbol]=None
                                cooldown_bars[symbol]=COOLDOWN_BARS; pending_ceil_lvl[symbol]=None
                                last_trough_level[symbol]=None
                                armed_long_switch[symbol]=False           # 사용한 스위치 소모
                                min_rsi_since_ent[symbol]=None
                                armed_short_switch[symbol]=(RSI>=SHORT_SWITCH_RSI)
                                max_rsi_since_ent[symbol]=RSI; prev_rsi[symbol]=RSI; continue

                # ===== 숏 보유 → 바닥 +3 반등 청산 (+조건부 롱 전환) =====
                elif position[symbol]=='short':
                    if RSI<=30: pending_floor_lvl[symbol]=30 if pending_floor_lvl[symbol] is None else min(pending_floor_lvl[symbol],30)
                    if RSI<=25: pending_floor_lvl[symbol]=25 if pending_floor_lvl[symbol] is None else min(pending_floor_lvl[symbol],25)
                    if RSI<=20: pending_floor_lvl[symbol]=20 if pending_floor_lvl[symbol] is None else min(pending_floor_lvl[symbol],20)
                    if RSI<=15: pending_floor_lvl[symbol]=15 if pending_floor_lvl[symbol] is None else min(pending_floor_lvl[symbol],15)

                    if (min_rsi_since_ent[symbol] is None) or (RSI<min_rsi_since_ent[symbol]): min_rsi_since_ent[symbol]=RSI
                    if RSI<=LONG_SWITCH_RSI: armed_long_switch[symbol]=True

                    if pending_floor_lvl[symbol] is not None:
                        trigger_up=pending_floor_lvl[symbol]+3
                        if RSI>=trigger_up and ROE>0.1:
                            close_position(symbol=symbol, side="Buy")
                            if armed_long_switch[symbol] and RSI>=LONG_SWITCH_RSI:
                                px,qty=entry_position(symbol=symbol, side="Buy", leverage=leverage)
                                if qty>0 and px is not None:
                                    position[symbol]='long'; entry_px[symbol]=px; tp_price[symbol]=None
                                    cooldown_bars[symbol]=COOLDOWN_BARS; pending_floor_lvl[symbol]=None
                                    armed_long_switch[symbol]=False; min_rsi_since_ent[symbol]=None
                                    armed_short_switch[symbol]=(RSI>=SHORT_SWITCH_RSI)
                                    max_rsi_since_ent[symbol]=RSI; last_trough_level[symbol]=None; prev_rsi[symbol]=RSI; continue
                            position[symbol]=None; entry_px[symbol]=None; tp_price[symbol]=None
                            cooldown_bars[symbol]=COOLDOWN_BARS; pending_floor_lvl[symbol]=None
                            armed_long_switch[symbol]=False; min_rsi_since_ent[symbol]=None; last_trough_level[symbol]=None

                # ===== 롱 보유 → 천장 -3 하락 청산 (+조건부 숏 전환) =====
                elif position[symbol]=='long':
                    if RSI>=70: pending_ceil_lvl[symbol]=70 if pending_ceil_lvl[symbol] is None else max(pending_ceil_lvl[symbol],70)
                    if RSI>=75: pending_ceil_lvl[symbol]=75 if pending_ceil_lvl[symbol] is None else max(pending_ceil_lvl[symbol],75)
                    if RSI>=80: pending_ceil_lvl[symbol]=80 if pending_ceil_lvl[symbol] is None else max(pending_ceil_lvl[symbol],80)
                    if RSI>=85: pending_ceil_lvl[symbol]=85 if pending_ceil_lvl[symbol] is None else max(pending_ceil_lvl[symbol],85)

                    if (max_rsi_since_ent[symbol] is None) or (RSI>max_rsi_since_ent[symbol]): max_rsi_since_ent[symbol]=RSI
                    if RSI>=SHORT_SWITCH_RSI: armed_short_switch[symbol]=True

                    if pending_ceil_lvl[symbol] is not None:
                        trigger_down=pending_ceil_lvl[symbol]-3
                        if RSI<=trigger_down and ROE>0.1:
                            close_position(symbol=symbol, side="Sell")
                            if armed_short_switch[symbol] and RSI<SHORT_SWITCH_RSI:
                                px,qty=entry_position(symbol=symbol, side="Sell", leverage=leverage)
                                if qty>0 and px is not None:
                                    position[symbol]='short'; entry_px[symbol]=px; tp_price[symbol]=None
                                    cooldown_bars[symbol]=COOLDOWN_BARS; pending_ceil_lvl[symbol]=None
                                    armed_short_switch[symbol]=False; max_rsi_since_ent[symbol]=None
                                    armed_long_switch[symbol]=(RSI<=LONG_SWITCH_RSI)
                                    min_rsi_since_ent[symbol]=RSI; last_peak_level[symbol]=None; prev_rsi[symbol]=RSI; continue
                            position[symbol]=None; entry_px[symbol]=None; tp_price[symbol]=None
                            cooldown_bars[symbol]=COOLDOWN_BARS; pending_ceil_lvl[symbol]=None
                            armed_short_switch[symbol]=False; max_rsi_since_ent[symbol]=None; last_peak_level[symbol]=None

                # 출력 (RSI(n) + interval 표시)
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                      f"🪙{symbol} @{interval} 💲현재가: {cur_3:.5f}$ 🚩포지션 {position.get(symbol)} "
                      f"| ❣ RSI({rsi_period})={RSI:.2f} | 💎Pnl: {Pnl:.3f} ⚜️ROE: {ROE:.2f}")
                prev_rsi[symbol]=RSI

            except Exception as e:
                print(f"[ERR] {symbol}: {type(e).__name__} {e}")
                continue
          
            time.sleep(5)
        time.sleep(10)


# run
start()
update()
