from dotenv import load_dotenv
from pybit.unified_trading import HTTP
import os
from dotenv import load_dotenv
from datetime import datetime
import time 

load_dotenv()

_api_key = os.getenv("API_KEY")
_api_secret = os.getenv("API_KEY_SECRET")

session = HTTP(api_key = _api_key, api_secret = _api_secret)

# ------ SETTING LINE ----------------

# 가져올 코인
coin_name = "DOGEUSDT"

# interval 분봉가져옴 1=1min
interval = "1"

# ------  ---------------- -----------



# get USER INFO
balance_info = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0] # 전체 자산(USD 기준)
total_balance_usd = balance_info["totalAvailableBalance"]


print(f"자산: {total_balance_usd}$ (USD)")


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
        
        print(f"[{date} {hour}:{minute}:{sec}] {coin_name} 가격 {price}$")
        
    except:
        print("# # 에러 # #")
    time.sleep(3)