from dotenv import load_dotenv
from pybit.unified_trading import HTTP
import os
import pandas as pd
from datetime import datetime
import time

# ------ GET API KEY -----------------
load_dotenv()

_api_key = os.getenv("API_KEY")
_api_secret = os.getenv("API_KEY_SECRET")

session = HTTP(api_key = _api_key, api_secret = _api_secret,  recv_window=10000)

# ---- PARAMITER LINE ---- # 이 후 UI개발에 사용
SYMBOL = ["DOGEUSDT"]
LEVERAGE = ["2"] #  must be string
PCT     = 40 # 투자비율 n% (후에 심볼 개수 비례도 구현)

# --- GLOBAL VARIABLE LINE ---- #

init_regime = None   # "golden" 또는 "dead"
primed = False       # 반대 크로스가 한 번 나와 거래 시작 가능한지
    
position= None
entry_price = None #포지션 진입가
tp_price = None

# ---- FUNC LINE -----

def get_usdt():
    bal = session.get_coin_balance(accountType="UNIFIED", coin="USDT")
    usdt = float(bal["result"]["balance"]["walletBalance"])
    
    return usdt

def set_leverage(symbol, leverage):
    
    try:
        session.set_leverage(
            category='linear',
            symbol=symbol,
            buy_leverage=leverage,
            sell_leverage=leverage,
        )
        
        print(f"✅ {symbol} 레버리지 설정 완료: {leverage}x")
    except:
        
        print(f"📛 {symbol} 레버리지 에러-> 이미 설정이 되어있습니다.")
        
        return

def get_kline(symbol, interval):
    
    resp = session.get_kline(
        symbol=symbol,    
        interval=str(interval),        
        limit=700,           
        category="linear",   
    )
    klines = resp["result"]["list"][::-1]
    
    return klines
    return klines

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

def get_current_price(symbol):
    t_res = session.get_tickers(
        category="linear",
        symbol=symbol
    )
    current_price = float(t_res["result"]["list"][0]["lastPrice"])
    
    return current_price

def get_EMA(symbol, period, interval): # index 0 = EMA(9), 1 = MA(28)
    
    kline = get_kline(symbol, interval)
    
    closes =  [float(k[4]) for k in kline]
    
    series = pd.Series(closes)
    
    ema_latest = series.ewm(span=period, adjust=False, min_periods=period).mean().iloc[-1]
    
    return ema_latest

def get_position_size(symbol): #진입해있는 선물 개수
    pos = session.get_positions(category='linear', symbol=symbol)
    
    size = int(pos['result']['list'][0]['size'])
    
    return size
    
def get_close_price(symbol, interval):
    resp = session.get_kline(
        symbol=symbol,
        interval = str(interval),
        limit =3, # 종료된 봉2, 현재진행봉1, 종료된 봉만 리턴
        category = 'linear',
    )
    
    klines = resp["result"]["list"][::-1] # 0=3번째 전 1=2번째 전 2(-1)=현재 진행봉

    return [float(k[4]) for k in klines]

def get_gap(ema_short, ma_long):
    return abs(ema_short - ma_long)

def entry_position(symbol, leverage, side): #side "Buy"=long, "Sell"=short
    
    value = get_usdt() * (PCT/ 100) # 구매할 usdt어치
    cur_price = get_current_price(symbol)
    
    qty = int((value * int(leverage)) / cur_price)
    
    session.place_order(
        category='linear',
        symbol=symbol,
        orderType="Market",
        qty = str(qty),
        isLeverage=1,
        side = side,
        reduceOnly=False
    )
    
    print(f"💡[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {symbol} 진입 / 수량 {qty} ({side})")
    
    return cur_price, qty
    
def close_position(symbol, side): # side "Buy"=short , "Sell"=long
    
    global entry_price

    qty = get_position_size(symbol=symbol)
    
    if qty <= 0:
        print("📍 닫을 포지션 없음")
        return
    
    current_price = get_current_price(symbol)

    # 수익률 계산
    if side == "Sell":  # 롱 포지션 청산
        profit_pct = ((current_price - entry_price) / entry_price) * 100
    elif side == "Buy":  # 숏 포지션 청산
        profit_pct = ((entry_price - current_price) / entry_price) * 100
    else:
        profit_pct = 0
    
    session.place_order(
        category='linear',
        symbol=symbol,
        orderType="Market",
        side=side,
        reduceOnly=True,
        isLeverage=1,
        qty=str(qty),
    )
    
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
    dipped35_after_entry = {s: False for s in SYMBOL}  # 숏용: 35 이하 찍었는가
    peaked65_after_entry = {s: False for s in SYMBOL}  # 롱용: 65 이상 찍었는가

    # 바 교체 감지용(최근 닫힌 캔들의 종가)
    last_closed_map = {s: None for s in SYMBOL}

    while True:
        for i in range(len(SYMBOL)):
            symbol = SYMBOL[i]
            leverage = LEVERAGE[i]

            # === 지표/가격 ===
            EMA_9  = get_EMA(symbol, interval=INTERVAL, period=9)
            EMA_28 = get_EMA(symbol, interval=INTERVAL, period=28)

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
            if position is None and ((48 <= RSI_14 <= 52) or (RSI_14 >= 65 or RSI_14 <= 35)):
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Wait")
                continue

            # =======================
            # 포지션 보유 시: 익절 로직(사용자 지정)
            # =======================
            # 숏 보유 중
            if position == 'short':
                if not dipped35_after_entry[symbol] and RSI_14 <= 35:
                    dipped35_after_entry[symbol] = True
                if (
                    (dipped35_after_entry[symbol] and RSI_14 > 40)          # 과매도 되돌림 시 익절
                    or ((c_prev1 > EMA_9) and (RSI_14 >= 50))               # 단기 반등 시 손절
                    or (EMA_9 > EMA_28)                                     # 추세 역전(상방) 시 청산
                ):
                    close_position(symbol=symbol, side="Buy")
                    position = None; entry_price = None; tp_price = None
                    dipped35_after_entry[symbol] = False; peaked65_after_entry[symbol] = False
                    time.sleep(SELL_COOLDOWN)

            # 롱 보유 중
            elif position == 'long':
                if not peaked65_after_entry[symbol] and RSI_14 >= 65:
                    peaked65_after_entry[symbol] = True
                if (
                    (peaked65_after_entry[symbol] and RSI_14 < 60)          # 과매수 되돌림 시 익절
                    or ((c_prev1 < EMA_9) and (RSI_14 <= 50))               # 단기 약세 시 손절
                    or (EMA_9 < EMA_28)                                     # 추세 역전(하방) 시 청산
                ):
                    close_position(symbol=symbol, side="Sell")
                    position = None; entry_price = None; tp_price = None
                    peaked65_after_entry[symbol] = False; dipped35_after_entry[symbol] = False
                    time.sleep(SELL_COOLDOWN)


            # =======================
            # 빈 포지션: 진입 (닫힌 바 기준으로만)
            # =======================
            # 숏 진입: EMA9<EMA28 + RSI 40~50 + 닫힌 두 바 연속 EMA9 아래 + EMA 간격 최소(≈0.1%)
            if position is None and new_bar:
                if (
                    (EMA_9 < EMA_28)
                    and (40 <= RSI_14 <= 50)
                    and (c_prev2 <= EMA_9 and c_prev1 <= EMA_9)
                    and (get_gap(EMA_9, EMA_28) >= 0.001 * c_prev1)   # 약 0.1% 이상 벌어짐
                ):
                    px, qty = entry_position(symbol=symbol, side="Sell", leverage=leverage)
                    if qty > 0:
                        position = 'short'
                        entry_price = px
                        tp_price = None
                        dipped35_after_entry[symbol] = False
                        peaked65_after_entry[symbol] = False

                # 롱 진입: EMA9>EMA28 + RSI 50~60 + 닫힌 두 바 연속 EMA9 위 + EMA 간격 최소
                elif (
                    (EMA_9 > EMA_28)
                    and (50 <= RSI_14 <= 60)
                    and (c_prev2 >= EMA_9 and c_prev1 >= EMA_9)
                    and (get_gap(EMA_9, EMA_28) >= 0.001 * c_prev1)
                ):
                    px, qty = entry_position(symbol=symbol, side="Buy", leverage=leverage)
                    if qty > 0:
                        position = 'long'
                        entry_price = px
                        tp_price = None
                        peaked65_after_entry[symbol] = False
                        dipped35_after_entry[symbol] = False


            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🪙 {symbol} 💲 현재가: {cur_3}$  🚩 포지션 {position} /  📶 EMA(9): {EMA_9:.6f}  EMA(28): {EMA_28:.6f} | ❣ RSI: {RSI_14}")

        time.sleep(10)


start()
update()