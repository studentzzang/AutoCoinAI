from dotenv import load_dotenv
from pybit.unified_trading import HTTP
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import time
import sys

# ------ GET API KEY -----------------
load_dotenv()

_api_key = os.getenv("API_KEY")
_api_secret = os.getenv("API_KEY_SECRET")

session = HTTP(api_key = _api_key, api_secret = _api_secret)

if not _api_key or not _api_secret:
    print("❌ API 키 또는 시크릿을 불러오지 못했습니다. .env 파일을 확인하세요.")
    sys.exit(1)  # 비정상 종료 (exit code 1)

# ------ SETTING LINE -----------------

    # 가져올 코인
coin_name = "DOGEUSDT"

    # 레버리지 설정(초보자 1이하 추천 x배);
leverage = 0.8;

    # interval 분봉가져옴 1=1min
interval = "1"

    # 기준 수익률 % (매도 기준 수익률 1~20 정도)
revenue_per = 6

    # 최저가 기준 가져올 n일전 기준의 n
get_lowest_day = 2.5

    # 최저가에 조금 곱해줘서 최저가 기준을 높여 매수가능성 높임 (너무 높이면 수익률 하락, 0~0.1)
proper_lowest_per = 0.02

# -------- ------ GETTING LINE (다른 함수에서 설정해줌) -------- ---------

lowest = 0
revenue_line = 0

isHavingCoin = False

# ----Get USER INFO ---------------------
balance_info = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0] # 전체 자산(USD 기준)
total_balance_usd = balance_info["totalAvailableBalance"]


print(f"자산: {total_balance_usd}$ (USD)")

# USDT 잔액 확인
balance_res = session.get_wallet_balance(accountType="UNIFIED")
coin_list = balance_res["result"]["list"][0]["coin"]

# USDT 찾기
usdt_balance = next((coin for coin in coin_list if coin["coin"] == "USDT"), None)

if usdt_balance or usdt_balance==0:
    print(f"✅ USDT 잔액: {usdt_balance['walletBalance']} USDT")
else:
    print("❌ USDT 잔액 정보를 찾을 수 없습니다. USDT가 입금 되었는지 확인하세요.", usdt_balance)

# -------- FUNCTION LINE --------- ------------

def get_lowest_price():
    
    # 1. 기준치 전부터 현재까지 타임스탬프(ms)
    
    now = datetime.now(timezone.utc)
    end_time = int(now.timestamp() * 1000)
    start_time = int((now - timedelta(days=get_lowest_day)).timestamp() * 1000)
    
    # 2. Kline구하기
    res = session.get_kline(
        category = 'linear', # 무기한 선물
        symbol = coin_name,
        interval = '5', # 기준 분봉
        start = start_time,
        end= end_time,
        limit=1000,
    )
    
    klines = res['result']['list']
    
    # 3. 최저가 찾기
    _lowest = min(klines, key=lambda x: float(x[3]))  # x[3] = lowPrice
    lowest_time = datetime.fromtimestamp(int(_lowest[0]) / 1000).astimezone(timezone.utc)
    
    global lowest
    lowest = float(_lowest[3]) + float(_lowest[3]) * proper_lowest_per

    print(f"📉 최저가: {lowest:.4f} USDT at {lowest_time}")
    
def set_revenue_line():
    
    global revenue_line
    revenue_line = lowest + (lowest * (revenue_per/100))
    
    print(f"목표 수익률 {revenue_per}% ⬆️ / 매도 최저 라인 {revenue_line:.4}$ 💡")

def main_loop():
    
    prev_price = None
    
    last_lowest_update = time.time()
    
    while True:
        
        # 최저가 갱신 by sec
        now = time.time()
        if now - last_lowest_update > 21600:
            get_lowest_price()
            set_revenue_line()
            last_lowest_update = now  #갱신
        try:
            response = session.get_tickers(
                category="linear",
                symbol = coin_name,
                
            )
            
            price = float(response['result']['list'][0]['lastPrice'])
            date = datetime.now().date()
            hour = datetime.now().hour
            minute = datetime.now().minute
            sec = datetime.now().second
            
            status = "default"
            
            # 매수 준비 체크 ---------

            if prev_price is not None and not isHavingCoin and price<=lowest:
                if price >= prev_price:
                    status = "매수!"
                    
                    buy()
                elif price < prev_price:
                    status = "🚨 매수 준비"
     
                
            #매도 준비 체크 --------
        
            if prev_price is not None and isHavingCoin and price >= revenue_line:
                if price <= prev_price:
                    status = "매도!"
                    
                    sell()
                elif price > prev_price:
                    status = "🚨 매도 준비"

                    
            
            prev_price = price
            
            
            
            print(f"[{date} {hour}:{minute}:{sec}] {coin_name} 가격 {price}$  |  상태 {status}")
        
                
            
        except Exception as e:
            print("# # 에러 # #", e)
        time.sleep(3)
      
# 보유한 코인 리턴  
def get_position_qty():
    result = session.get_positions(category="linear", symbol=coin_name)
    pos = result["result"]["list"][0]
    return float(pos["size"]) if pos["side"] == "Buy" else 0.0  # 롱 포지션일 때만 매도

def buy():
    global isHavingCoin
    isHavingCoin = True
    
    buy_price_usdt = usdt_balance * leverage

    order = session.place_order(
        category="linear",
        symbol = coin_name,
        side = "Buy",
        order_type = "Market",
        qty = buy_price_usdt,
        reduce_only = False
        
    )
    
    if order and order.get("retCode") == 0:
        data = order["result"]
        qty = data.get("cumExecQty")  # 체결된 코인 수량
        value = data.get("cumExecValue")  # 체결된 총 USDT

        # 현재 USDT 잔고 다시 조회
        balance = session.get_wallet_balance(accountType="UNIFIED")
        usdt_now = float(balance["result"]["list"][0]["totalAvailableBalance"])

        print(f"✅ 매수 완료: 코인 {qty}개 약 {value} USDT")
        print(f"📦 남은 USDT 잔량: {usdt_now} USDT")

    else:
        print(f"❌ 매수 실패: {order['retMsg']}")
    

def sell():
    global isHavingCoin
    isHavingCoin = False
    
    result = session.get_positions()
    pos = result["result"]["list"][0]

    if pos["side"] == "Buy":
        qty = float(pos["size"])

        order = session.place_order(
            category="linear",
            symbol=coin_name,
            side="Sell",
            order_type="Market",
            qty=qty,
            time_in_force="GoodTillCancel",
            reduce_only=True
        )

        # 결과 출력만 하고 리턴 안 함
        if order and order.get("retCode") == 0:
            data = order["result"]
            print(f"✅ 전량 매도 완료: {data['qty']}개 @ 약 {data['cumExecValue']} USDT")
        else:
            print(f"❌ 매도 실패: {order['retMsg']}")
    else:
        print("⛔ 롱 포지션이 없습니다.")
    

    
get_lowest_price()
set_revenue_line()
main_loop()