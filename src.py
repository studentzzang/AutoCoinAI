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
LEVERAGE = ["3"] #  must be string
PCT     = 30 # 투자비율 n% (후에 심볼 개수 비례도 구현)

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
    COOLDOWN_SEC = 45  # 거래 후 대기

    # --- 익절 파라미터 ---
    TP_BASE = 0.008       # 기본 목표익 (0.8%)
    TP_STRONG = 0.012     # 강세 시 목표익 (1.2%)
    TP_WEAK = 0.006       # 약세 시 축소 목표익 (0.6%)

    TRAIL_ACTIVATE = 0.006   # 트레일링 발동 최소 이익 (0.6%)
    TRAIL_BACK = 0.004       # 피크 대비 되돌림폭 (0.4%)

    prev_rsi_map = {s: None for s in SYMBOL}
    last_trade_ts = {s: None for s in SYMBOL}

    # 피크/트로프(트레일링용)
    peak_map = {s: None for s in SYMBOL}    # 롱에서 최고가
    trough_map = {s: None for s in SYMBOL}  # 숏에서 최저가

def update():
    global position, entry_price

    COOLDOWN_SEC = 45  # 거래 후 대기
    RSI_ARM = 65       # 익절 무장 임계
    RSI_EXIT_SOFT = 55 # 익절 트리거 (안전선)
    RSI_EXIT_HARD = 50 # 보수적 하드선(보조)

    prev_rsi_map = {s: None for s in SYMBOL}
    last_trade_ts = {s: None for s in SYMBOL}
    rsi_armed = {s: False for s in SYMBOL}   # 포지션 보유 중 65↑ 터치 여부

    while True:
        now_ts = time.time()

        for i in range(len(SYMBOL)):
            symbol = SYMBOL[i]
            leverage = LEVERAGE[i]

            # --- 데이터 준비: 3분봉 전체, 종가 시리즈 ---
            kl = get_kline(symbol, interval=3)  # oldest -> newest
            closes = pd.Series([float(k[4]) for k in kl])

            if len(closes) < 28:  # EMA28, SMA20 계산 안정화용 최소 길이
                continue

            # --- 지표 계산: EMA9/28, 볼린저 중단선(SMA20) ---
            ema9_series  = closes.ewm(span=9, adjust=False, min_periods=9).mean()
            ema28_series = closes.ewm(span=28, adjust=False, min_periods=28).mean()
            sma20_series = closes.rolling(window=20, min_periods=20).mean()

            EMA_9  = float(ema9_series.iloc[-1])
            EMA_28 = float(ema28_series.iloc[-1])
            BB_MID = float(sma20_series.iloc[-1])

            # RSI 최신값
            RSI_14 = float(get_RSI(symbol, interval=3, period=14))
            prev_rsi = prev_rsi_map[symbol]

            # --- 조건 구성 ---
            # 1) EMA 골든크로스(직전<=, 현재>)
            ema_cross_up = (
                not pd.isna(ema9_series.iloc[-2]) and not pd.isna(ema28_series.iloc[-2]) and
                (ema9_series.iloc[-2] <= ema28_series.iloc[-2]) and (ema9_series.iloc[-1] > ema28_series.iloc[-1])
            )

            # 2) 최근 3개 종가가 '각 시점의' EMA9 위
            last3_closes = closes.iloc[-3:]
            last3_ema9   = ema9_series.iloc[-3:]
            last3_sma20  = sma20_series.iloc[-3:]

            three_above_ema9  = (last3_closes > last3_ema9).all()
            three_above_bbmid = (last3_closes > last3_sma20).all()

            # (A) 추세/모멘텀 충족: EMA 크로스 OR (3봉이 EMA9 또는 BB중단선 위)
            trend_ok = ema_cross_up or (three_above_ema9 or three_above_bbmid)

            # (B) RSI 조건: 50 이상
            rsi_ok = (RSI_14 >= 50)

            # 쿨다운 조건
            cooldown_ok = (last_trade_ts[symbol] is None) or (now_ts - last_trade_ts[symbol] >= COOLDOWN_SEC)

            # ========== 포지션 관리 ==========
            # (선택) 이전에 숏이 남아있다면 정리하고 롱 전략만 수행
            if position == 'short':
                close_position(symbol=symbol, side="Buy")
                position = None
                entry_price = None
                last_trade_ts[symbol] = time.time()
                rsi_armed[symbol] = False
                prev_rsi_map[symbol] = RSI_14
                continue

            # ----- 익절 로직: RSI 65↑ 무장 후, 55↓(또는 50↓) 하향 시 청산 -----
            if position == 'long' and entry_price is not None:
                # 무장(arm): 보유 중 RSI가 65 이상을 한 번이라도 터치하면 True
                if not rsi_armed[symbol] and RSI_14 >= RSI_ARM:
                    rsi_armed[symbol] = True

                # 무장 후 55 아래로 내려오면 익절. (하드선 50은 추가 안전장치)
                if rsi_armed[symbol] and (RSI_14 <= RSI_EXIT_SOFT or RSI_14 <= RSI_EXIT_HARD):
                    close_position(symbol=symbol, side="Sell")
                    position = None
                    entry_price = None
                    last_trade_ts[symbol] = time.time()
                    rsi_armed[symbol] = False
                    prev_rsi_map[symbol] = RSI_14
                    continue

            # ----- 신규 롱 진입 -----
            if (position is None) and cooldown_ok and trend_ok and rsi_ok:
                px, qty = entry_position(symbol=symbol, side="Buy", leverage=leverage)
                if qty > 0:
                    position = 'long'
                    entry_price = px
                    last_trade_ts[symbol] = time.time()
                    rsi_armed[symbol] = (RSI_14 >= RSI_ARM)  # 진입 직후 이미 65 이상이라면 곧바로 무장
                    prev_rsi_map[symbol] = RSI_14
                    continue

            # 모니터링 로그
            cur_px = float(closes.iloc[-1])
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🪙 {symbol} "
                  f"💲{cur_px:.6f} | EMA9 {EMA_9:.6f} / EMA28 {EMA_28:.6f} / BBmid {BB_MID:.6f} | "
                  f"RSI {RSI_14:.2f} | pos {position} | "
                  f"entry {entry_price if entry_price else '-'} | "
                  f"arm {rsi_armed[symbol]} | cool {cooldown_ok}")

            prev_rsi_map[symbol] = RSI_14

        time.sleep(9)



start()
update()