from dotenv import load_dotenv
from pybit.unified_trading import HTTP
import os
from dotenv import load_dotenv
from datetime import datetime
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

# ------- ---------------- ------------

# ------ SETTING LINE -----------------

# 가져올 코인
coin_name = "DOGEUSDT"

# 레버리지 설정(초보자 1이하 추천 x배);
leverage = 0.8;

# interval 분봉가져옴 1=1min
interval = "1"

# ------  ---------------- ------------



# get USER INFO
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