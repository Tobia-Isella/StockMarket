from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from dotenv import load_dotenv
from pathlib import Path
import os


# Load env
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)


client = TradingClient(
    api_key=os.getenv("APCA_API_KEY_ID"),
    secret_key=os.getenv("APCA_API_SECRET_KEY"),
    paper=True
)


# Buy Order
Stock_buy = MarketOrderRequest(
    symbol="AAPL",
    qty=1,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.DAY 
)


order = client.submit_order(Stock_buy )
print("Buy order sent:", order)