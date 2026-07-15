import os
import time
import threading
from flask import Flask
import pandas as pd
import telebot
from pocketoptionapi.stable_api import PocketOption

# --- CONFIG ---
app = Flask(__name__)
bot_running = threading.Event()
bot_running.set() 

ssid = os.environ.get("PO_SSID")
telegram_token = os.environ.get("TELEGRAM_TOKEN")
chat_id = os.environ.get("CHAT_ID")
api = PocketOption(ssid)
tg_bot = telebot.TeleBot(telegram_token)

# --- CORE LOGIC ---
def is_in_compression(macd_df, price, window=5):
    threshold = price * 0.00001
    recent_diffs = abs(macd_df['MACD_12_26_9'].iloc[-window:] - macd_df['MACDs_12_26_9'].iloc[-window:])
    return all(diff < threshold for diff in recent_diffs)

def get_market_data(asset):
    candles = api.get_candles(asset, 300, 100, time.time())
    if not candles: return None, None
    df = pd.DataFrame(candles)
    macd = ta.macd(df['close'])
    rsi = ta.rsi(df['close'], length=14).iloc[-1]
    ema_200 = ta.ema(df['close'], length=200).iloc[-1]
    return df, {"macd": macd, "rsi": rsi, "ema": ema_200, "price": df['close'].iloc[-1]}

def analyze_market(asset):
    df, ind = get_market_data(asset)
    if df is None: return None
    if is_in_compression(ind['macd'], ind['price']): return None
    
    macd_line = ind['macd']['MACD_12_26_9'].iloc[-1]
    signal_line = ind['macd']['MACDs_12_26_9'].iloc[-1]
    
    if ind['price'] > ind['ema'] and macd_line > signal_line and 30 <= ind['rsi'] <= 45: return "CALL"
    if ind['price'] < ind['ema'] and macd_line < signal_line and 55 <= ind['rsi'] <= 70: return "PUT"
    return None

# --- TRADE MONITORING ---
def monitor_trade_result(trade_id, asset):
    """Waits for the trade to finish and reports result."""
    time.sleep(305)  # Wait for 5-minute trade duration
    result = api.check_win(trade_id)  # Returns profit/loss
    status = "WON 💰" if result > 0 else "LOST 📉"
    msg = f"📊 *Result for {asset}*\nStatus: {status}\nProfit: {result:.2f}"
    tg_bot.send_message(chat_id, msg, parse_mode="Markdown")

# --- ENGINE ---
def run_trading_engine():
    if api.connect():
        while True:
            if bot_running.is_set():
                # Sync to 5-minute mark
                if time.localtime().tm_min % 5 == 0 and time.localtime().tm_sec < 5:
                    try:
                        profits = api.get_all_profit()
                        assets = [a for a, p in profits.items() if p >= 90 and "_otc" in a.lower()]
                        for asset in assets:
                            signal = analyze_market(asset)
                            if signal:
                                # Place order
                                buy_info = api.buy(1, asset, signal.lower(), 300)
                                trade_id = buy_info["id"] # Capture unique trade ID
                                
                                # Send initial alert
                                df, ind = get_market_data(asset)
                                diff = abs(ind['macd']['MACD_12_26_9'].iloc[-1] - ind['macd']['MACDs_12_26_9'].iloc[-1])
                                msg = (f"🚀 *Trade Executed*\nAsset: {asset}\nSignal: {signal}\n"
                                       f"Price: {ind['price']:.5f}\nMACD Diff: {diff:.6f}\nTime: {time.strftime('%H:%M:%S')}")
                                tg_bot.send_message(chat_id, msg, parse_mode="Markdown")
                                
                                # Start monitoring thread
                                threading.Thread(target=monitor_trade_result, args=(trade_id, asset)).start()
                        time.sleep(60) 
                    except Exception as e: print(f"Error: {e}")
            time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=run_trading_engine, daemon=True).start()
    threading.Thread(target=tg_bot.infinity_polling, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
