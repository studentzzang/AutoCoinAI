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

session = HTTP(api_key = _api_key, api_secret = _api_secret,  recv_window=10000)

if not _api_key or not _api_secret:
    print("❌ API 키 또는 시크릿을 불러오지 못했습니다. .env 파일을 확인하세요.")
    sys.exit(1)  # 비정상 종료 (exit code 1)

# ------ SETTING LINE -----------------

    # 가져올 코인
coin_name = "DOGEUSDT"

    # 레버리지 설정(초보자 1이하 추천 x배);
leverage = 1

    # interval 분봉가져옴 1=1min
interval = "1"

    # 최저가 기준 가져올 n일전 기준의 n
get_lowest_day = 2.5


# -------- ------ GETTING LINE (다른 함수에서 설정해줌) -------- ---------

usdt_balance = 0
lowest = 0
# 기준 수익률 % (매도 기준 수익률 1~20 정도)
revenue_per = 0
revenue_line = 0

# 최저가에 조금 곱해줘서 최저가 기준을 높여 매수가능성 높임 (너무 높이면 수익률 하락, 0~5)
proper_lowest_per = 0

isHavingCoin = False

# ----Get USER INFO ---------------------
def get_usdt():
    global usdt_balance
    try:
        res = session.get_coins_balance(accountType="UNIFIED", coin="USDT")
        balance_list = res["result"]["balance"]
        
        
        usdt_item = next((item for item in balance_list if item["coin"] == "USDT"), None)
        usdt_balance = float(usdt_item["walletBalance"]) if usdt_item else 0.0
        
        print(f"USDT잔액 : {usdt_balance}")
    except Exception as e:
        print(f"❌ FUND 계정 USDT 잔액 정보를 가져오는 중 오류 발생: {e}")
        usdt_balance = 0.0

def get_target_info():
    # 확인할 코인 지정
  
    try:
      result = session.get_positions(category="linear", symbol=coin_name)
      balance = float(result["result"]["list"][0]['size'])
        
      
      if(balance>0):
        global isHavingCoin
        isHavingCoin = True
      
      print(f"{coin_name} 보유량 {balance}, isHavingCoinn={isHavingCoin}")
      
    except Exception as e:
      print(f"❌ FUND 계정 {coin_name} 잔액 정보를 가져오는 중 오류 발생: {e}")


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
    
    while(True):
      global lowest
      proper_lowest_per = float(input("매수 최저가 보정(%, 0~10) :"))
      lowest = float(_lowest[3]) + float(_lowest[3]) * (proper_lowest_per/100.0)

      isDone = input(f"📉 매수 라인(최저가 {proper_lowest_per}%): {lowest:.4f} USDT at {lowest_time} 시작하겠습니까? (Y/N)")
      
      if(isDone.upper()=="Y"): 
        break
      else:
        continue
    
def set_revenue_line():
    
    global revenue_line
    
    revenue_per = float(input("목표 수익률 입력: "))
    
    revenue_line = lowest + (lowest * (revenue_per/100))    
    
    print(f"목표 수익률 {revenue_per}% ⬆️ / 매도 최저 라인 {revenue_line:.4f}$ 💡")

def main_loop():
    
    prev_price = None
    
    last_lowest_update = time.time()
    
    while True:
        
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
                status = "🔥 매수!"
                
                buy()
            elif price < prev_price:
                status = "🚨 매수 준비"

            
        #매도 준비 체크 --------
    
        if prev_price is not None and isHavingCoin and price >= revenue_line:
            if price <= prev_price:
                status = "✨ 매도!"
                
                sell()
            elif price > prev_price:
                status = "🚨 매도 준비"

                
        
        prev_price = price
        
        
        
        print(f"[{date} {hour}:{minute}:{sec}] {coin_name} 가격 {price}$  |  상태 {status}")
        
                
        time.sleep(3)
      
# 보유한 코인 리턴  
def get_position_qty():
    result = session.get_positions(category="linear", symbol=coin_name)
    pos = result["result"]["list"][0]
    return float(pos["size"]) if pos["side"] == "Buy" else 0.0  # 롱 포지션일 때만 매도

def buy():
    global isHavingCoin
    
    if usdt_balance <= 0:
        print("❌ USDT 잔고가 0입니다. 매수 중단.")
        return

    buy_price_usdt = usdt_balance * leverage

    # ✅ 최신 가격으로 환산해서 qty 계산
    ticker = session.get_tickers(category="linear", symbol=coin_name)
    price = float(ticker["result"]["list"][0]["lastPrice"])

    qty = int(buy_price_usdt / price)  # 일단 정수화
    qty = qty // 10 * 10        

    isHavingCoin = True

    order = session.place_order(
    category="linear",
    symbol=coin_name,
    side="Buy",
    order_type="Market",
    qty=str(qty),
    reduce_only=False
    )

    if order and order.get("retCode") == 0:
        data = order["result"]
        print(f"✅ 매수 완료 amount: {qty} leverage: {leverage}")
    else:
        print(f"❌ 매수 실패")
    

def sell():
    
    result = session.get_positions(category="linear", symbol=coin_name)
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
            print(f"✅ 전량 매도 완료: {pos['size']}")

            global isHavingCoin
            isHavingCoin = False
        else:
            print(f"❌ 매도 실패")
    else:
        print("⛔ 롱 포지션이 없습니다.")
    

get_usdt()
get_target_info()
get_lowest_price()
set_revenue_line()
main_loop()
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

session = HTTP(api_key = _api_key, api_secret = _api_secret,  recv_window=10000)

if not _api_key or not _api_secret:
    print("❌ API 키 또는 시크릿을 불러오지 못했습니다. .env 파일을 확인하세요.")
    sys.exit(1)  # 비정상 종료 (exit code 1)

# ------ SETTING LINE -----------------

    # 가져올 코인
coin_name = "DOGEUSDT"

    # 레버리지 설정(초보자 1이하 추천 x배);
leverage = 1

    # interval 분봉가져옴 1=1min
interval = "1"

    # 최저가 기준 가져올 n일전 기준의 n
get_lowest_day = 2.5

    

# -------- ------ GETTING LINE (다른 함수에서 설정해줌) -------- ---------

usdt_balance = 0
lowest = 0
# 기준 수익률 % (매도 기준 수익률 1~20 정도)
revenue_per = 0
revenue_line = 0

# 최저가에 조금 곱해줘서 최저가 기준을 높여 매수가능성 높임 (너무 높이면 수익률 하락, 0~5)
proper_lowest_per = 0

isHavingCoin = False

# ----Get USER INFO ---------------------
def get_usdt():
    global usdt_balance

    res = session.get_coins_balance(accountType="FUND", coin="USDT")
    balance_list = res["result"]["balance"]
    
    
    usdt_item = next((item for item in balance_list if item["coin"] == "USDT"), None)
    usdt_balance = float(usdt_item["availableToTrade"]) if usdt_item else 0.0
    
    print(f"USDT잔액 : {usdt_balance}")

def get_target_info():
    # 확인할 코인 지정
  
  res = session.get_coins_balance(accountType="FUND", coin=coin_name.replace("USDT",""))
  balance_list = res["result"]["balance"]
  
  
  target = next((item for item in balance_list if item["coin"] == coin_name.replace("USDT","")), None)
  balance = float(target["walletBalance"]) if target else 0.0
  
  if(balance>0):
    global isHavingCoin
    isHavingCoin = True
      
  print(f"{coin_name} 보유 개수: {balance}, isHavingCoin {isHavingCoin}")
      
def set_leverage():
  global leverage
  leverage = float(input("레버리지x :")) 
  
  if not leverage:
    leverage=1
  
  res = session.set_leverage(
    category="linear",
    symbol=coin_name,
    buy_leverage=str(leverage),
    sell_leverage=str(leverage)
  )

  
  print(f"✅ 레버리지 설정 완료: {res}")
    

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
    
    while(True):
      global lowest
      proper_lowest_per = float(input("매수 최저가 보정(%, 0~10) :"))
      lowest = float(_lowest[3]) + float(_lowest[3]) * (proper_lowest_per/100.0)

      isDone = input(f"📉 매수 라인(최저가 {proper_lowest_per}%): {lowest:.4f} USDT at {lowest_time} 시작하겠습니까? (Y/N)")
      
      if(isDone.upper()=="Y"): 
        break
      else:
        continue
    
def set_revenue_line():
    
    global revenue_line
    
    revenue_per = float(input("목표 수익률 입력: "))
    
    revenue_line = lowest + (lowest * (revenue_per/100))    
    
    print(f"목표 수익률 {revenue_per}% ⬆️ / 매도 최저 라인 {revenue_line:.4f}$ 💡")

def main_loop():
    
    prev_price = None
    
    last_lowest_update = time.time()
    
    # 1) 잔고 조회
    get_usdt()
    # 2) 레버리지 설정 (여기서 한 번만 호출)
    set_leverage()
    # 3) 보유 코인 정보 초기화
    get_target_info()
    # 4) 매수/매도 라인 설정
    get_lowest_price()
    set_revenue_line()
    
    while True:
        
      # 최저가 갱신 by sec
      now = time.time()
      if now - last_lowest_update > 21600:
          get_lowest_price()
          set_revenue_line()
          last_lowest_update = now  #갱신

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
              status = "🔥 매수!"
              
              buy(price=price)
          elif price < prev_price:
              status = "🚨 매수 준비"

          
      #매도 준비 체크 --------
  
      if prev_price is not None and isHavingCoin and price >= revenue_line:
          if price <= prev_price:
              status = "✨ 매도!"
              
              sell()
          elif price > prev_price:
              status = "🚨 매도 준비"

              
      
      prev_price = price
      
      
      
      print(f"[{date} {hour}:{minute}:{sec}] {coin_name} 가격 {price}$  |  상태 {status}")
      
          
      time.sleep(3)
      
# 보유한 코인 리턴  
def get_position_qty():
    result = session.get_positions(category="linear", symbol=coin_name)
    pos = result["result"]["list"][0]
    return float(pos["size"]) if pos["side"] == "Buy" else 0.0  # 롱 포지션일 때만 매도

def buy(price):

    global isHavingCoin, usdt_balance

    get_usdt()

    if usdt_balance <= 0:
        print("❌ USDT 잔고가 0입니다. 매수 중단.")
        return

    buy_price_usdt = usdt_balance

    qty = int((buy_price_usdt / price) / 2) // 10 * 10      

    
    order = session.place_order(
        category="linear",
        symbol=coin_name,
        isLeverage=1,
        side="Buy",
        order_type="Market",
        qty=str(qty),
        reduce_only=True
    )

    if order and order.get("retCode") == 0:
        print(f"✅ 매수 완료: {qty}개")
    else:
        print(f"❌ 매수 실패")
    

def sell():
    
    result = session.get_positions(category='linear', symbol=coin_name)
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
            print(f"✅ 전량 매도 완료: {pos['size']}개")
            
            global isHavingCoin, usdt_balanc
            isHavingCoin=False
            get_usdt()
        else:
            print(f"❌ 매도 실패: {order['retMsg']}")
    else:
        print("⛔ 롱 포지션이 없습니다.")
        

    
if __name__ == "__main__":
  
  # 5) 메인 루프 진입
  main_loop()