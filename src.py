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
LEVERAGE = ["3"] #  must be string
PCT     = 30 # 투자비율 n% (후에 심볼 개수 비례도 구현)
INTERVAL = 1 #min
EMA_PERIOD = 9
MA_PERIOD = 28

# ---- FUNC LINE -----

def get_usdt():
    bal = session.get_wallet_balance(accountType="UNIFIED")
    usdt = float(bal["result"]["list"][0]["totalAvailableBalance"])
    
    return usdt

def set_leverage(symbol, leverage):
    session.set_leverage(
        category='linear',
        symbol=symbol,
        leverage=leverage
    )

def get_kline(symbol):
    
    resp = session.get_kline(
        symbol=symbol,    
        interval="1",        
        limit=200,           
        category="linear",   
    )
    klines = resp["result"]["list"]
    
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
    cur_price=cur_price(symbol)
    
    closes =  [float(k[4]) for k in kline]
    closes.append(cur_price)
    
    series = pd.Series(closes)
    
    ema9_latest = series.ewm(span=9, adjust=False).mean().iloc[-1]
    ma28_latest = series.rolling(window=28).mean().iloc[-1]
    
    return list(ema9_latest, ma28_latest)

def get_position_size(symbol): #진입해있는 선물 개수
    pos = session.get_position(category='linear', symbol=symbol)
    
    size = float(pos['result']['list'][0]['size'])
    

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
    
def close_position(symbol, side): # side "Buy"=short , "Sell"=long
    
    qty = int(get_position_size(symbol=symbol))
    
    session.place_order(
        category='linear',
        symbol=symbol,
        orderType="Market",
        side=side,
        reduceOnly=True,
        isLeverage=1,
        qty=qty,
    )
    

# ---- MAIN LOOP ---

def start():
    for i in range(len(SYMBOL)):
        set_leverage(symbol=SYMBOL[i], leverage=LEVERAGE[i])
    
def update():
    
    status = ""
    
    while True:
        
        for i in range(len(SYMBOL)):
            m_avgs = get_MAs(SYMBOL[i])
            
            EMA_short = m_avgs[0]
            MA_long = m_avgs[1]
            
            print(EMA_short, MA_long)
        
        time.sleep(10)
