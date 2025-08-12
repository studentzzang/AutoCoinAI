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

    # ===== 파라미터 =====
    COOLDOWN_SEC = 45
    RSI_ARM = 68            # 익절 무장 임계
    RSI_EXIT_SOFT = 60      # 무장 후 하향시 익절
    RSI_EXIT_HARD = 55      # 추가 안전선

    ATR_PERIOD = 14
    ATR_STOP_MULT = 1.2     # 초기 스탑: entry - 1.2*ATR (롱)
    MIN_BB_WIDTH = 0.008    # BB 상대폭 >= 0.8% 일 때만 진입(저변동 회피)

    # *** 꼭대기 추격 방지 ***
    MAX_EXT_ATR = 0.6       # (닫힌봉종가-EMA9)/ATR <= 0.6
    MAX_EXT_PCT = 0.005     # (닫힌봉종가-EMA9)/가격 <= 0.5%
    BIG_RANGE_ATR = 1.2     # 전봉(high-low) >= 1.2*ATR 이면 추격 금지
    RETEST_LOOKBACK = 3     # 최근 3봉 안에 EMA9 '터치' 후 재이탈 확인
    CROSS_LOOKBACK = 5      # 최근 5봉 내 골든크로스 허용
    ONE_TRADE_PER_BAR = True
    TIMEOUT_BARS = 10       # ARM 못 찍으면 10봉 내 정리

    prev_rsi_map = {s: None for s in SYMBOL}
    last_trade_ts = {s: None for s in SYMBOL}
    rsi_armed = {s: False for s in SYMBOL}
    sl_map = {s: None for s in SYMBOL}
    entry_bar_idx = {s: None for s in SYMBOL}
    last_trade_bar_idx = {s: None for s in SYMBOL}

    while True:
        now_ts = time.time()

        for i in range(len(SYMBOL)):
            symbol = SYMBOL[i]
            leverage = LEVERAGE[i]
<<<<<<< HEAD
=======
            
            EMA_9 = get_EMA(symbol, interval=3, period=9) # get MAs
            EMA_28 = get_EMA(symbol, interval=3, period=28)
            
            klines_3 = get_close_price(symbol, interval=3) # get close price min 1
            
            kline_1 = klines_3[1] # 1x3분전
            kline_2 = klines_3[0] # 2~3x3분전
            cur_3 = klines_3[-1] # 현재 진행
>>>>>>> parent of d99564b (Feat: get RSI function)

            # --- 데이터 ---
            kl = get_kline(symbol, interval=3)  # oldest -> newest
            if len(kl) < 40:
                continue

            closes = pd.Series([float(k[4]) for k in kl])
            highs  = pd.Series([float(k[2]) for k in kl])
            lows   = pd.Series([float(k[3]) for k in kl])

            # 지표
            ema9  = closes.ewm(span=9,  adjust=False, min_periods=9).mean()
            ema28 = closes.ewm(span=28, adjust=False, min_periods=28).mean()
            sma20 = closes.rolling(window=20, min_periods=20).mean()
            std20 = closes.rolling(window=20, min_periods=20).std()
            upper = sma20 + 2*std20
            lower = sma20 - 2*std20
            bb_width = (upper - lower) / sma20

            prev_close = closes.shift(1)
            tr = pd.concat([
                highs - lows,
                (highs - prev_close).abs(),
                (lows  - prev_close).abs()
            ], axis=1).max(axis=1)
            atr = tr.ewm(alpha=1/ATR_PERIOD, adjust=False).mean()

            # 최신값(참고), '전봉' 값(신호판단용)
            EMA9_CUR, EMA28_CUR = float(ema9.iloc[-1]), float(ema28.iloc[-1])
            EMA9_PREV, EMA28_PREV = float(ema9.iloc[-2]), float(ema28.iloc[-2])
            BB_MID_PREV = float(sma20.iloc[-2]) if not pd.isna(sma20.iloc[-2]) else None
            BBW = float(bb_width.iloc[-1]) if not pd.isna(bb_width.iloc[-1]) else 0.0
            ATR = float(atr.iloc[-1])
            c_cur = get_current_price(symbol)
            c_prev = float(closes.iloc[-2])
            h_prev = float(highs.iloc[-2])
            l_prev = float(lows.iloc[-2])

            # RSI 시리즈(전봉/현재 둘 다 확보)
            delta = closes.diff()
            up = delta.clip(lower=0)
            down = -delta.clip(upper=0)
            avg_gain = up.ewm(alpha=1/14, adjust=False).mean()
            avg_loss = down.ewm(alpha=1/14, adjust=False).mean()
            rs = avg_gain / avg_loss.replace(0, 1e-10)
            rsi_series = 100 - (100 / (1 + rs))
            RSI_PREV = float(rsi_series.iloc[-2])
            RSI_CUR  = float(rsi_series.iloc[-1])

            bar_idx = len(closes)
            per_bar_ok = (not ONE_TRADE_PER_BAR) or (last_trade_bar_idx[symbol] is None) or (bar_idx > last_trade_bar_idx[symbol])

            # ---- 추세/기울기 ----
            ema_trend_up = (EMA9_CUR > EMA28_CUR) and (ema9.iloc[-1] > ema9.iloc[-2])

            # ---- 최근 골든크로스(완료봉 기준) ----
            cross_up_series = (ema9.shift(1) <= ema28.shift(1)) & (ema9 > ema28)
            ema_cross_up_recent = bool(cross_up_series.iloc[-(CROSS_LOOKBACK+1):-1].any())

            # ---- 3봉 기준(완료봉) ----
            last3_cl = closes.iloc[-(RETEST_LOOKBACK+1):-1]
            last3_e9 = ema9.iloc[-(RETEST_LOOKBACK+1):-1]
            last3_sm = sma20.iloc[-(RETEST_LOOKBACK+1):-1]
            three_above_ema9  = (last3_cl > last3_e9).all()
            three_above_bbmid = (not last3_sm.isna().any()) and (last3_cl > last3_sm).all()

            # ---- 리테스트 확인: 최근 N봉 중 '저가가 EMA9 터치' + '종가가 EMA9 위' ----
            touched = (lows.iloc[-(RETEST_LOOKBACK+1):-1] <= (ema9.iloc[-(RETEST_LOOKBACK+1):-1] * 1.001)).any()
            confirm = (last3_cl.iloc[-1] > last3_e9.iloc[-1]) if len(last3_cl) > 0 else False
            retest_ok = bool(touched and confirm)

            # ---- 과확장(꼭대기 추격 방지) ----
            ext_atr = (c_prev - EMA9_PREV) / max(ATR, 1e-12)
            ext_pct = (c_prev - EMA9_PREV) / max(c_prev, 1e-12)
            no_overextend = (ext_atr <= MAX_EXT_ATR) and (ext_pct <= MAX_EXT_PCT)
            big_range = (h_prev - l_prev) >= BIG_RANGE_ATR * ATR

            # ---- 변동성/쿨다운 ----
            vol_ok = (BBW >= MIN_BB_WIDTH) if BBW == BBW else False
            cooldown_ok = (last_trade_ts[symbol] is None) or (now_ts - last_trade_ts[symbol] >= COOLDOWN_SEC)

            # ====== 스탑(가격) ======
            if position == 'long' and sl_map[symbol] is not None and c_cur <= sl_map[symbol]:
                close_position(symbol=symbol, side="Sell")
                position = None; entry_price = None
                last_trade_ts[symbol] = time.time()
                rsi_armed[symbol] = False
                sl_map[symbol] = None
                entry_bar_idx[symbol] = None
                last_trade_bar_idx[symbol] = bar_idx
                prev_rsi_map[symbol] = RSI_CUR
                continue

            # ====== RSI 익절(ARM → EXIT) ======
            if position == 'long' and entry_price is not None:
                if not rsi_armed[symbol] and RSI_CUR >= RSI_ARM:
                    rsi_armed[symbol] = True
                if rsi_armed[symbol] and (RSI_CUR <= RSI_EXIT_SOFT or RSI_CUR <= RSI_EXIT_HARD):
                    close_position(symbol=symbol, side="Sell")
                    position = None; entry_price = None
                    last_trade_ts[symbol] = time.time()
                    rsi_armed[symbol] = False
                    sl_map[symbol] = None
                    entry_bar_idx[symbol] = None
                    last_trade_bar_idx[symbol] = bar_idx
                    prev_rsi_map[symbol] = RSI_CUR
                    continue

                # 타임아웃: ARM 못 찍고 TIMEOUT_BARS 경과 → 정리
                if entry_bar_idx[symbol] is not None and (bar_idx - entry_bar_idx[symbol] >= TIMEOUT_BARS) and (not rsi_armed[symbol]):
                    if (c_cur <= entry_price*1.001) or (RSI_CUR < 50):
                        close_position(symbol=symbol, side="Sell")
                        position = None; entry_price = None
                        last_trade_ts[symbol] = time.time()
                        rsi_armed[symbol] = False
                        sl_map[symbol] = None
                        entry_bar_idx[symbol] = None
                        last_trade_bar_idx[symbol] = bar_idx
                        prev_rsi_map[symbol] = RSI_CUR
                        continue

            # ====== 신규 롱 진입 (닫힌 봉 기준) ======
            # 조건: 추세상승 & (최근 크로스 or 3봉 상방) & 리테스트 확인 & 과확장 아님 & 전봉 과대범위 아님 & RSI 50 상향 돌파
            rsi_cross_up_50 = (prev_rsi_map[symbol] is not None) and (prev_rsi_map[symbol] <= 50) and (RSI_CUR > 50)
            if (position is None) and cooldown_ok and per_bar_ok and vol_ok and ema_trend_up:
                if ( (ema_cross_up_recent or (three_above_ema9 or three_above_bbmid))
                     and retest_ok and no_overextend and (not big_range) and rsi_cross_up_50 ):
                    px, qty = entry_position(symbol=symbol, side="Buy", leverage=leverage)
                    if qty > 0:
                        position = 'long'; entry_price = px
                        last_trade_ts[symbol] = time.time()
                        rsi_armed[symbol] = (RSI_CUR >= RSI_ARM)
                        sl_map[symbol] = px - ATR_STOP_MULT * ATR
                        entry_bar_idx[symbol] = bar_idx
                        last_trade_bar_idx[symbol] = bar_idx
                        prev_rsi_map[symbol] = RSI_CUR
                        continue

            # ---- 로그 ----
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {symbol} "
                  f"px {c_cur:.6f} | EMA9 {EMA9_CUR:.6f}/EMA28 {EMA28_CUR:.6f} | "
                  f"ext_atr {ext_atr:.2f} ext_pct {ext_pct*100:.2f}% | "
                  f"BBW {BBW:.4f} ATR {ATR:.6f} | RSI {RSI_CUR:.2f} | "
                  f"trend {ema_trend_up} crossRecent {ema_cross_up_recent} retest {retest_ok} "
                  f"| overext {not no_overextend} bigbar {big_range} | pos {position} SL {sl_map[symbol]}")

            prev_rsi_map[symbol] = RSI_CUR

        time.sleep(9)


<<<<<<< HEAD
=======
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🪙 {symbol} 💲 현재가: {cur_3}$  🚩 포지션 {position} /  📶 EMA(9): {EMA_9:.6f}  EMA(22): {EMA_28:.6f}")                
  
        time.sleep(4)
>>>>>>> parent of d99564b (Feat: get RSI function)

start()
update()