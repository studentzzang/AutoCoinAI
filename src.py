from dotenv import load_dotenv
from pybit.unified_trading import HTTP
import os
from dotenv import load_dotenv
from datetime import datetime
import time 

load_dotenv()

_api_key = os.getenv("API_KEY")
_api_secret = os.getenv("API_KEY_SERCRET")

session = HTTP(api_key = _api_key, api_secret = _api_secret)

# 가져올 코인
coin_name = "DOGEUSDT"

# interval 분봉가져옴 1=1min
interval = "1"

while True:
    try:
        response = session.get_tickers(
            category="linear",
            symbol = coin_name,
            
        )
        price = response['result']['list'][0]['lastPrice']
        date = datetime.now().date()
        hour = datetime.now().hour
        minute = datetime.now().minute
        sec = datetime.now().second
        
        print(f"[{date} {hour} : {minute} : {sec}] {coin_name} 가격 {price}")
        
    except:
        print("# # 에러 # #")
    time.sleep(3)