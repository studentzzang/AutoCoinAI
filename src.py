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
SYMBOL = ["PUMPFUNUSDT"]
LEVERAGE = ["1"] #  must be string
PCT     = 25 # 투자비율 n% (후에 심볼 개수 비례도 구현)

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
    
    global position, entry_price
    global init_regime, primed
    status=""
    
    while True:
        
        for i in range(len(SYMBOL)):
            
            symbol = SYMBOL[i]
            leverage = LEVERAGE[i]
            
            EMA_9 = get_EMA(symbol, interval=3, period=9) # get MAs
            EMA_28 = get_EMA(symbol, interval=3, period=28)
            
            klines_3 = get_close_price(symbol, interval=3) # get close price min 1
            
            kline_1 = klines_3[1] # 1x3분전
            kline_2 = klines_3[0] # 2~3x3분전
            cur_3 = klines_3[-1] # 현재 진행


            # -- 조건부 -- #
          
            longSign_candle = (kline_1 > kline_2 and cur_3 > kline_1 and cur_3 > EMA_28)
            shortSign_candle = (kline_1 < kline_2 and cur_3 < kline_1 and cur_3 < EMA_9)
            
            longSign_EMA = (EMA_9 > EMA_28)
            shortSign_EMA = (EMA_28 > EMA_9)
            
            """
                # ==== 최초 1회: 현재 상태 저장 ====
            if init_regime is None:
                
                init_regime = "golden" if longSign_EMA else "dead"
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🌱 초기 상태: {init_regime}. 반대 크로스 대기 시작")
                
                continue

            # ==== primed 될 때까지: '반대 크로스'만 보고 대기 ====
            if not primed:
              
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 📶 EMA(9): {EMA_9:.6f}  EMA(22): {EMA_28:.6f}")
                
                if ((init_regime == "golden" and (shortSign_EMA or shortSign_candle)) 
                    or (init_regime == "dead"  and (longSign_EMA or longSign_candle))):
                    
                    primed = True
                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✅ 반대 크로스 발생, 거래 시작")
                    
                else:
                    continue
                    """
            # --- 조건 검사 및 실행 --- #
            
            if position == 'short' and ( longSign_EMA):
                close_position(symbol=symbol, side='Buy')  # leverage 인자 넣지 않음
                position=None
                
            if position == 'long' and (shortSign_EMA):
                close_position(symbol=symbol, side="Sell")
                position = None
                
            if (position is None) and (longSign_EMA):
                px, qty = entry_position(symbol=symbol, side="Buy", leverage=leverage)
                if qty > 0:
                    position = 'long'
                    entry_price = px

            if (position is None) and (shortSign_EMA):
                px, qty = entry_position(symbol=symbol, side="Sell", leverage=leverage)
                if qty > 0:
                    position = 'short'
                    entry_price = px

            
              
            # -- 정보 출력 -- #
            

            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🪙 {symbol} 💲 현재가: {cur_3}$  🚩 포지션 {position} /  📶 EMA(9): {EMA_9:.6f}  EMA(22): {EMA_28:.6f}")                
  
        time.sleep(4)

start()
update()