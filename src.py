from dotenv import load_dotenv
from pybit.unified_trading import HTTP
import os
import pandas as pd
from datetime import datetime, timedelta, timezone
import time

# ------ GET API KEY -----------------
load_dotenv()

_api_key = os.getenv("API_KEY")
_api_secret = os.getenv("API_KEY_SECRET")

session = HTTP(api_key = _api_key, api_secret = _api_secret,  recv_window=10000)

# ---- PARAMITER LINE ---- # 이 후 UI개발에 사용
SYMBOL = ["DOGEUSDT"]
LEVERAGE = ["1"] #  must be string
PCT     = 20 # 투자비율 n% (후에 심볼 개수 비례도 구현)

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
        
        print(f"🎯 {symbol} 레버리지 설정 완료: {leverage}x")
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

def get_current_price(symbol, interval):
    t_res = session.get_tickers(
        category="linear",
        interval=interval,
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
    
    klines = resp["result"]["list"][::-1] #과거->현재

    return [float(k[4]) for k in klines]

def get_gap(ema_short, ma_long):
    return abs(ema_short - ma_long)

def entry_position(symbol, leverage, side): #side "Buy"=long, "Sell"=short
    
    value = get_usdt() * (PCT/ 100) # 구매할 usdt어치
    cur_price = get_current_price(symbol, 1)
    
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
    
    print(f"💡 {symbol} 진입 / 수량 {qty} ({side})")
    
    return cur_price, qty
    
def close_position(symbol, side): # side "Buy"=short , "Sell"=long
    
    qty = get_position_size(symbol=symbol)
    
    session.place_order(
        category='linear',
        symbol=symbol,
        orderType="Market",
        side=side,
        reduceOnly=True,
        isLeverage=1,
        qty=str(qty),
    )
    
    print(f"📍 {symbol} 익절 / 수량 {qty}")
    

# ---- MAIN LOOP ---

def start():
    for i in range(len(SYMBOL)):
        set_leverage(symbol=SYMBOL[i], leverage=LEVERAGE[i])
    
    
position= None
entry_price = None #포지션 진입가
tp_price = None
def update():
    
    global position
    global entry_price
    global tp_price
    
    status=""
    
    while True:
        
        for i in range(len(SYMBOL)):
            
            symbol = SYMBOL[i]
            leverage = LEVERAGE[i]
            
            EMA_1_9 = get_EMA(symbol, interval=1, period=9) # get MAs
            EMA_1_22 = get_EMA(symbol, interval=1, period=22)
            
            klines_1 = get_close_price(symbol, interval=1) # get close price min 1
            
            current_price_1 = get_current_price(symbol, interval=1)
            kline_1 = klines_1[1] # 2분전
            kline_2 = klines_1[0] # 1분전

            EMA_5_21 = get_EMA(symbol, interval=5, period=21)
            current_price_5 = get_current_price(symbol, interval=5)
            
            # -- 조건부 -- #
            
                # 필터 (1차, 큰방향)
            long_filter = (current_price_5 > EMA_5_21)
            short_filter = (current_price_5 < EMA_5_21)
            
            longSign_candle = kline_1 > EMA_1_9 and kline_2 > EMA_1_9 and current_price_1 > EMA_1_9
            shortSign_candle = kline_1 < EMA_1_9 and kline_2 < EMA_1_9 and current_price_1 < EMA_1_9
            
            longSign_EMA = (EMA_1_9 > EMA_1_22)
            shortSign_EMA = (EMA_1_22 > EMA_1_9)
            
            # --조건 검사 및 실행--#
                # 롱 진입
            if(position is None) and long_filter and longSign_candle and longSign_EMA:
                
                px, _ = entry_position(symbol, leverage= leverage, side="Buy")
                
                position = "long"
                entry_price = px
                TP_PCT = 0.008  # 0.8%
                tp_price = entry_price * (1 + TP_PCT)
                
                # 롱 익절 (스위칭 금지: 여기서 끝내고 대기)
            if (position == "long") and (current_price_1 >= tp_price):
                close_position(symbol, leverage= leverage, side="Sell")
                position = None
                entry_price = None
                tp_price = None
                
            #  숏 진입
            if (position is None) and short_filter and shortSign_candle and shortSign_EMA:
                
                px, _ = entry_position(symbol, leverage= leverage, side="Sell")
                
                position = "short"
                entry_price = px
                TP_PCT = 0.008  # 0.8% 예시
                tp_price = entry_price * (1 - TP_PCT)
                
            # 4) 숏 익절
            if (position == "short") and (current_price_1 <= tp_price):
                close_position(symbol, leverage= leverage, side="Buy")
                position = None
                entry_price = None
                tp_price = None
                
            # -- 정보 출력 -- #
            
            print(f"🪙 {symbol} 💲현재가: {current_price_1}$ 포지션 {position} / EMA(9): {EMA_1_9:.6f}  EMA(22): {EMA_1_22:.6f}")
                        
  
        time.sleep(4)

start()
update()