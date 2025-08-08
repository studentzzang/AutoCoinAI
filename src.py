from dotenv import load_dotenv
from pybit.unified_trading import HTTP
import os
import pandas as pd
from datetime import datetime, timedelta, timezone
import time
import sys

# ------ GET API KEY -----------------
load_dotenv()

_api_key = os.getenv("API_KEY")
_api_secret = os.getenv("API_KEY_SECRET")

session = HTTP(api_key = _api_key, api_secret = _api_secret,  recv_window=10000)

# ---- PARAMITER LINE ---- # 이 후 UI개발에 사용
SYMBOL = ["DOGEUSDT"]
LEVERAGE = ["5"] #  must be string
PCT     = 50 # 투자비율 n% (후에 심볼 개수 비례도 구현)
INTERVAL = 1 #min
EMA_PERIOD = 9
MA_PERIOD = 19

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
    except:
        return

def get_kline(symbol):
    
    resp = session.get_kline(
        symbol=symbol,    
        interval="1",        
        limit=700,           
        category="linear",   
    )
    klines = resp["result"]["list"][::-1]
    
    return klines

def get_current_price(symbol):
    t_res = session.get_tickers(
        category="linear",
        symbol=symbol
    )
    current_price = float(t_res["result"]["list"][0]["lastPrice"])
    
    return current_price

def get_MAs(symbol): # index 0 = EMA(9), 1 = MA(28)
    
    kline = get_kline(symbol)
    cur_price=get_current_price(symbol)
    
    closes =  [float(k[4]) for k in kline]
    #closes.append(cur_price)
    
    series = pd.Series(closes)
    
    ema9_latest = series.ewm(span=EMA_PERIOD, adjust=False, min_periods=EMA_PERIOD).mean().iloc[-1]
    ma_latest = series.rolling(window=MA_PERIOD, min_periods=MA_PERIOD).mean().iloc[-1]
    
    return[ema9_latest, ma_latest]

def get_position_size(symbol): #진입해있는 선물 개수
    pos = session.get_positions(category='linear', symbol=symbol)
    
    size = int(pos['result']['list'][0]['size'])
    
    return size
    
def get_close_price(symbol):
    resp = session.get_kline(
        symbol=symbol,
        interval = INTERVAL,
        limit =3, # 종료된 봉2, 현재진행봉1, 종료된 봉만 리턴
        category = 'linear',
    )
    
    klines = resp["result"]["list"][::-1] #과거->현재

    return [float(k[4]) for k in klines]

def get_gap(ema_short, ma_long):
    return abs(ema_short - ma_long)

def entry_position(symbol, side): #side "Buy"=long, "Sell"=short
    
    value = get_usdt() * (PCT/ 100) # 구매할 usdt어치
    cur_price = get_current_price(symbol)
    
    qty = int(value / cur_price)
    
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
def update():
    
    global position
    
    status=""
    
    while True:
        
        for i in range(len(SYMBOL)):
            
            symbol = SYMBOL[i]
            
            m_avgs = get_MAs(symbol) # get MAs
            EMA_short = m_avgs[0]
            MA_long = m_avgs[1]
            
            klines = get_close_price(symbol) # get close price
            
            current_price = get_current_price(symbol)
            kline_1 = klines[1] # 2분전
            kline_2 = klines[0] # 1분전
            
            if MA_long > EMA_short:
                status = "데드 크로스"
                
                if position == "long":
                    
                    position = "short"
                    
                    close_position(symbol, side='Buy')
                    entry_position(symbol, side='Sell')
                    
                elif not position: #최초 한 번
                    entry_position(symbol=symbol, side='Sell')
                    position="short"
                
            elif EMA_short > MA_long:
                status="골든 크로스"
                
                if position == "short":
                    
                    position = "long"
                    
                    close_position(symbol, side='Sell')
                    entry_position(symbol, side='Buy')
                    
                elif not position: #최초 한 번
                    entry_position(symbol, side='Buy')
                    position="long"
                    
            
            print(f"현재가: {current_price} / 1분전: {kline_1} 2분전: {kline_2} / EMA({EMA_PERIOD}) : {EMA_short:.7f}, MA({MA_PERIOD}): {MA_long:.7f} / {status} {position}")
            
            

        
        time.sleep(8)

start()
update()