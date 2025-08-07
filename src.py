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

# ---- FUNC LINE -----


def get_kline():
    
    resp = session.get_kline(
        symbol="BTCUSDT",    
        interval="1",        
        limit=200,           
        category="linear",   
    )
    klines = resp["result"]["list"]
    
    return klines
    