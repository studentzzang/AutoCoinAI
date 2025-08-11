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

    RSI_LO, RSI_HI = 35, 65
    NEUTRAL_LO, NEUTRAL_HI = 45, 55
    COOLDOWN_SEC = 45 #거래 후 대기

    prev_rsi_map = {s: None for s in SYMBOL}
    last_trade_ts = {s: None for s in SYMBOL}

    while True:
        now_ts = time.time()

        for i in range(len(SYMBOL)):
            symbol = SYMBOL[i]
            leverage = LEVERAGE[i]

            EMA_9  = get_EMA(symbol, interval=3, period=9)
            EMA_28 = get_EMA(symbol, interval=3, period=28)
            klines_3 = get_close_price(symbol, interval=3)
            kline_1 = klines_3[1]
            kline_2 = klines_3[0]
            cur_3   = klines_3[-1]

            RSI_14 = get_RSI(symbol, interval=3, period=14)
            prev_rsi = prev_rsi_map[symbol]

            longSign_EMA  = (EMA_9 > EMA_28)
            shortSign_EMA = (EMA_28 > EMA_9)

            # --- RSI 교차 ---
            rsi_cross_up_30 = (prev_rsi is not None) and (prev_rsi <= RSI_LO) and (RSI_14 > RSI_LO)
            rsi_cross_dn_70 = (prev_rsi is not None) and (prev_rsi >= RSI_HI) and (RSI_14 < RSI_HI)

            # --- 중립 밴드 ---
            rsi_neutral = (NEUTRAL_LO <= RSI_14 <= NEUTRAL_HI)

            # --- 모멘텀 진입 허용: EMA9 재돌파 + RSI가 50선 방향 ---
            momo_long  = (RSI_14 >= 52) and (kline_1 <= EMA_9) and (cur_3 > EMA_9)
            momo_short = (RSI_14 <= 48) and (kline_1 >= EMA_9) and (cur_3 < EMA_9)

            # --- 최종 타이밍 신호(둘 중 하나면 OK) ---
            rsi_long_ok  = rsi_cross_up_30  or momo_long
            rsi_short_ok = rsi_cross_dn_70 or momo_short

            cooldown_ok = (last_trade_ts[symbol] is None) or (now_ts - last_trade_ts[symbol] >= COOLDOWN_SEC)

            # ===== 청산 (OR) =====
            if position == 'long' and (shortSign_EMA or rsi_cross_dn_70 or (RSI_14 <= RSI_LO)):
                close_position(symbol=symbol, side="Sell")
                position = None; entry_price = None
                last_trade_ts[symbol] = time.time()
                prev_rsi_map[symbol] = RSI_14
                continue

            if position == 'short' and (longSign_EMA or rsi_cross_up_30 or (RSI_14 >= RSI_HI)):
                close_position(symbol=symbol, side="Buy")
                position = None; entry_price = None
                last_trade_ts[symbol] = time.time()
                prev_rsi_map[symbol] = RSI_14
                continue

            # ===== 신규 진입 (AND) =====
            if (position is None) and cooldown_ok and (not rsi_neutral):
                if longSign_EMA and rsi_long_ok:
                    px, qty = entry_position(symbol=symbol, side="Buy", leverage=leverage)
                    if qty > 0:
                        position = 'long'; entry_price = px
                        last_trade_ts[symbol] = time.time()
                        prev_rsi_map[symbol] = RSI_14
                        continue

                if shortSign_EMA and rsi_short_ok:
                    px, qty = entry_position(symbol=symbol, side="Sell", leverage=leverage)
                    if qty > 0:
                        position = 'short'; entry_price = px
                        last_trade_ts[symbol] = time.time()
                        prev_rsi_map[symbol] = RSI_14
                        continue

            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🪙 {symbol} 💲 현재가: {cur_3}$  🚩 포지션 {position} /  📶 EMA(9): {EMA_9:.6f}  EMA(28): {EMA_28:.6f} | ❣ RSI: {RSI_14:.2f}")

            prev_rsi_map[symbol] = RSI_14

        time.sleep(9)

start()
update()