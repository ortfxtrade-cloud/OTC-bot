import os
import time
import threading
from flask import Flask
import pandas as pd
import telebot
from telebot import types
import numpy as np
import requests
import uuid

# --- CONFIG ---
app = Flask(__name__)
bot_running = threading.Event()
bot_running.set() 

ssid = os.environ.get("PO_SSID")
telegram_token = os.environ.get("TELEGRAM_TOKEN")
chat_id = os.environ.get("CHAT_ID")
tg_bot = telebot.TeleBot(telegram_token)

# Store pending signals
pending_signals = {}
signal_timeout = 30

# Spread filter settings
MAX_SPREAD_PERCENT = 0.05
MAX_SPREAD_PIPS = 5
SPREAD_COOLDOWN_MINUTES = 60

spread_rejected = {}

# --- POCKET OPTION API CLIENT ---
class PocketOptionAPI:
    def __init__(self, ssid):
        self.ssid = ssid
        self.base_url = "https://pocketoption.com/api"
        self.demo_url = "https://demo.pocketoption.com/api"
        self.session = requests.Session()
        self.session.headers.update({
            'Cookie': f'ssid={ssid}',
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json'
        })
    
    def test_ssid(self):
        """Test if SSID is valid by checking multiple endpoints"""
        results = {
            "ssid_provided": bool(self.ssid),
            "ssid_length": len(self.ssid) if self.ssid else 0,
            "tests": {}
        }
        
        print("\n" + "="*50)
        print("🔍 TESTING SSID CONNECTION")
        print("="*50)
        print(f"SSID: {self.ssid[:10]}...{self.ssid[-5:] if len(self.ssid) > 10 else ''}")
        print(f"Length: {len(self.ssid)} characters")
        print("-"*50)
        
        # Test 1: Real API - Profile
        try:
            url = f"{self.base_url}/profile"
            response = self.session.get(url, timeout=10)
            status = response.status_code
            results["tests"]["real_profile"] = {
                "url": url,
                "status": status,
                "success": status == 200
            }
            
            if status == 200:
                data = response.json()
                print(f"✅ REAL API Profile: OK (Status {status})")
                print(f"   Response: {str(data)[:100]}")
                if 'id' in data or 'email' in data:
                    results["account_info"] = {
                        "id": data.get('id', 'N/A'),
                        "email": data.get('email', 'N/A'),
                        "balance": data.get('balance', 'N/A')
                    }
                    print(f"   Account ID: {data.get('id', 'N/A')}")
                    print(f"   Balance: {data.get('balance', 'N/A')}")
            elif status == 401:
                print(f"❌ REAL API Profile: UNAUTHORIZED - Invalid SSID")
            elif status == 403:
                print(f"❌ REAL API Profile: FORBIDDEN - SSID expired")
            else:
                print(f"⚠️ REAL API Profile: Status {status}")
                
        except Exception as e:
            results["tests"]["real_profile"] = {"success": False, "error": str(e)}
            print(f"❌ REAL API Profile: Error - {str(e)[:80]}")
        
        # Test 2: Demo API - Profile
        try:
            url = f"{self.demo_url}/profile"
            response = self.session.get(url, timeout=10)
            status = response.status_code
            results["tests"]["demo_profile"] = {
                "url": url,
                "status": status,
                "success": status == 200
            }
            
            if status == 200:
                print(f"✅ DEMO API Profile: OK (Status {status})")
                data = response.json()
                print(f"   Account ID: {data.get('id', 'N/A')}")
            elif status == 401:
                print(f"⚠️ DEMO API Profile: Unauthorized")
            else:
                print(f"⚠️ DEMO API Profile: Status {status}")
                
        except Exception as e:
            results["tests"]["demo_profile"] = {"success": False, "error": str(e)}
            print(f"❌ DEMO API Profile: Error - {str(e)[:80]}")
        
        # Test 3: Assets/Profit endpoint
        try:
            url = f"{self.base_url}/assets/profit"
            response = self.session.get(url, timeout=10)
            status = response.status_code
            results["tests"]["assets_profit"] = {
                "url": url,
                "status": status,
                "success": status == 200
            }
            
            if status == 200:
                data = response.json()
                asset_count = len(data) if isinstance(data, dict) else 0
                print(f"✅ Assets Profit: OK - {asset_count} assets found")
                results["asset_count"] = asset_count
            else:
                print(f"❌ Assets Profit: Status {status}")
                
        except Exception as e:
            results["tests"]["assets_profit"] = {"success": False, "error": str(e)}
            print(f"❌ Assets Profit: Error - {str(e)[:80]}")
        
        # Test 4: Get candles (quick test)
        try:
            url = f"{self.base_url}/candles"
            params = {
                'asset': 'EURUSD_otc',
                'timeframe': 60,
                'count': 1,
                'timestamp': int(time.time())
            }
            response = self.session.get(url, params=params, timeout=10)
            status = response.status_code
            results["tests"]["candles"] = {
                "url": url,
                "status": status,
                "success": status == 200
            }
            
            if status == 200:
                data = response.json()
                candles = data.get('candles', [])
                print(f"✅ Candles API: OK - Got {len(candles)} candle(s)")
            else:
                print(f"❌ Candles API: Status {status}")
                
        except Exception as e:
            results["tests"]["candles"] = {"success": False, "error": str(e)}
            print(f"❌ Candles API: Error - {str(e)[:80]}")
        
        # Summary
        print("-"*50)
        passed = sum(1 for t in results["tests"].values() if t.get("success"))
        total = len(results["tests"])
        print(f"📊 RESULTS: {passed}/{total} tests passed")
        
        if passed == total:
            print("✅ SSID IS VALID AND WORKING!")
        elif passed > 0:
            print("⚠️ SSID PARTIALLY WORKING - Some endpoints failed")
        else:
            print("❌ SSID IS INVALID OR EXPIRED!")
        print("="*50 + "\n")
        
        return results
    
    def connect(self):
        """Check connection"""
        try:
            response = self.session.get(f"{self.base_url}/profile", timeout=10)
            return response.status_code == 200
        except:
            return False
    
    def get_candles(self, asset, timeframe, count, timestamp):
        try:
            params = {
                'asset': asset,
                'timeframe': timeframe,
                'count': count,
                'timestamp': timestamp
            }
            response = self.session.get(f"{self.base_url}/candles", params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                return data.get('candles', [])
            return []
        except:
            return []
    
    def get_all_profit(self):
        try:
            response = self.session.get(f"{self.base_url}/assets/profit", timeout=10)
            if response.status_code == 200:
                return response.json()
            return {}
        except:
            return {}
    
    def buy(self, amount, asset, direction, expiry):
        try:
            data = {
                'amount': amount,
                'asset': asset,
                'direction': direction,
                'expiry': expiry
            }
            response = self.session.post(f"{self.base_url}/trade/open", json=data, timeout=10)
            if response.status_code == 200:
                result = response.json()
                return {'id': result.get('trade_id', str(uuid.uuid4()))}
            return {}
        except:
            return {}
    
    def check_win(self, trade_id):
        try:
            response = self.session.get(f"{self.base_url}/trade/result/{trade_id}", timeout=10)
            if response.status_code == 200:
                result = response.json()
                return result.get('profit', 0)
            return 0
        except:
            return 0

api = PocketOptionAPI(ssid)

# --- SSID TEST COMMAND ---
@tg_bot.message_handler(commands=['test_ssid', 'check_ssid'])
def test_ssid_command(message):
    """Test SSID validity via Telegram command"""
    tg_bot.send_message(message.chat.id, "🔍 *Testing SSID connection...*\n\nPlease wait...", parse_mode="Markdown")
    
    results = api.test_ssid()
    
    # Build response message
    msg = "*🔍 SSID TEST RESULTS*\n\n"
    
    msg += f"*SSID:* `{ssid[:10]}...{ssid[-5:] if len(ssid) > 10 else '***'}`\n"
    msg += f"*Length:* {len(ssid)} characters\n\n"
    
    msg += "*Tests:*\n"
    for test_name, test_result in results["tests"].items():
        emoji = "✅" if test_result.get("success") else "❌"
        status = test_result.get("status", "Error")
        msg += f"{emoji} *{test_name}*: {status}\n"
    
    # Account info if available
    if "account_info" in results:
        msg += f"\n*Account Info:*\n"
        msg += f"• ID: `{results['account_info']['id']}`\n"
        msg += f"• Balance: `{results['account_info']['balance']}`\n"
    
    # Asset count
    if "asset_count" in results:
        msg += f"\n*Assets Available:* {results['asset_count']}\n"
    
    # Final verdict
    passed = sum(1 for t in results["tests"].values() if t.get("success"))
    total = len(results["tests"])
    
    msg += f"\n*Verdict:* "
    if passed == total:
        msg += "✅ *SSID VALID!* Bot can trade."
    elif passed > 0:
        msg += "⚠️ *PARTIALLY WORKING* - Check logs."
    else:
        msg += "❌ *INVALID SSID* - Get a new one!"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔄 Retest", callback_data="retest_ssid"))
    
    tg_bot.send_message(message.chat.id, msg, parse_mode="Markdown", reply_markup=markup)

# --- TECHNICAL INDICATORS ---
def calculate_ema(data, period):
    return data.ewm(span=period, adjust=False).mean()

def calculate_macd(data, fast=12, slow=26, signal=9):
    ema_fast = calculate_ema(data, fast)
    ema_slow = calculate_ema(data, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calculate_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return pd.DataFrame({
        'MACD_12_26_9': macd_line,
        'MACDs_12_26_9': signal_line,
        'MACDh_12_26_9': histogram
    })

def calculate_rsi(data, period=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# --- SPREAD CALCULATION & COOLDOWN ---
def calculate_spread(bid_price, ask_price):
    if not bid_price or not ask_price or bid_price <= 0 or ask_price <= 0:
        return None
    spread = ask_price - bid_price
    spread_pips = spread * 10000
    if bid_price < 1:
        spread_pips = spread * 100
    spread_percent = (spread / bid_price) * 100
    return {
        'spread': spread,
        'spread_pips': spread_pips,
        'spread_percent': spread_percent,
        'bid': bid_price,
        'ask': ask_price
    }

def is_in_spread_cooldown(asset):
    if asset in spread_rejected:
        rejection_time = spread_rejected[asset]
        cooldown_seconds = SPREAD_COOLDOWN_MINUTES * 60
        time_since_rejection = time.time() - rejection_time
        if time_since_rejection < cooldown_seconds:
            remaining_minutes = (cooldown_seconds - time_since_rejection) / 60
            return True, remaining_minutes
        else:
            del spread_rejected[asset]
    return False, 0

def set_spread_cooldown(asset):
    spread_rejected[asset] = time.time()

def check_spread_filter(asset, max_spread_percent=MAX_SPREAD_PERCENT, max_spread_pips=MAX_SPREAD_PIPS):
    in_cooldown, remaining = is_in_spread_cooldown(asset)
    if in_cooldown:
        return False, {'spread_pips': 0, 'spread_percent': 0, 'cooldown': True, 'remaining_minutes': remaining}
    
    try:
        candles = api.get_candles(asset, 60, 2, time.time())
        if not candles or len(candles) < 1:
            return True, None
        
        last_candle = candles[-1]
        bid_price = last_candle.get('low', last_candle.get('close'))
        ask_price = last_candle.get('high', last_candle.get('close'))
        
        if 'bid' in last_candle and 'ask' in last_candle:
            bid_price = last_candle['bid']
            ask_price = last_candle['ask']
        else:
            price = last_candle.get('close', 0)
            if price > 0:
                estimated_spread = price * 0.0003
                bid_price = price - estimated_spread/2
                ask_price = price + estimated_spread/2
        
        spread_info = calculate_spread(bid_price, ask_price)
        if spread_info is None:
            return True, None
        
        spread_ok = True
        if spread_info['spread_percent'] > max_spread_percent:
            spread_ok = False
        if spread_info['spread_pips'] > max_spread_pips:
            spread_ok = False
        
        if not spread_ok:
            set_spread_cooldown(asset)
        
        spread_info['cooldown'] = False
        return spread_ok, spread_info
        
    except:
        return True, None

# --- MACD CROSS DETECTION ---
def detect_macd_signal(macd_line, signal_line):
    if len(macd_line) < 2 or len(signal_line) < 2:
        return "NONE"
    
    prev_diff = macd_line.iloc[-2] - signal_line.iloc[-2]
    curr_diff = macd_line.iloc[-1] - signal_line.iloc[-1]
    
    is_touch = abs(curr_diff) < 0.00001
    was_apart = abs(prev_diff) > 0.00001
    
    if is_touch and was_apart:
        return "TOUCH"
    if prev_diff < 0 and curr_diff > 0:
        return "BULL_CROSS"
    if prev_diff > 0 and curr_diff < 0:
        return "BEAR_CROSS"
    return "NONE"

# --- CORE LOGIC ---
def get_market_data(asset, timeframe_seconds):
    candles = api.get_candles(asset, timeframe_seconds, 100, time.time())
    if not candles: 
        return None, None
    df = pd.DataFrame(candles)
    if len(df) < 50:
        return None, None
    macd_df = calculate_macd(df['close'])
    rsi = calculate_rsi(df['close'], length=14).iloc[-1]
    ema_200 = calculate_ema(df['close'], period=200).iloc[-1] if len(df) >= 200 else None
    return df, {"macd": macd_df, "rsi": rsi, "ema_200": ema_200, "price": df['close'].iloc[-1]}

def check_15min_trend(asset):
    df_15m, ind_15m = get_market_data(asset, 900)
    if df_15m is None or ind_15m is None or ind_15m['ema_200'] is None:
        return {"valid": False}
    price_15m = ind_15m['price']
    ema_200_15m = ind_15m['ema_200']
    if price_15m > ema_200_15m:
        return {"valid": True, "trend": "BULLISH", "direction": "BUY", "price": price_15m, "ema_200": ema_200_15m}
    elif price_15m < ema_200_15m:
        return {"valid": True, "trend": "BEARISH", "direction": "SELL", "price": price_15m, "ema_200": ema_200_15m}
    else:
        return {"valid": False}

def analyze_market(asset):
    spread_ok, spread_info = check_spread_filter(asset)
    if not spread_ok:
        return None
    
    trend_15m = check_15min_trend(asset)
    if not trend_15m['valid']:
        return None
    
    df_2m, ind_2m = get_market_data(asset, 120)
    if df_2m is None or ind_2m is None:
        return None
    
    macd_line = ind_2m['macd']['MACD_12_26_9']
    signal_line = ind_2m['macd']['MACDs_12_26_9']
    rsi = ind_2m['rsi']
    macd_signal = detect_macd_signal(macd_line, signal_line)
    
    trade_direction = trend_15m['direction']
    
    if trade_direction == "BUY":
        if macd_signal in ["BULL_CROSS", "TOUCH"]:
            if 30 <= rsi <= 45:
                return {
                    "signal": "CALL", "trend_15m": trend_15m,
                    "macd_signal": macd_signal, "rsi": rsi,
                    "macd_line": macd_line.iloc[-1], "signal_line": signal_line.iloc[-1],
                    "spread_info": spread_info, "valid": True
                }
    elif trade_direction == "SELL":
        if macd_signal in ["BEAR_CROSS", "TOUCH"]:
            if 55 <= rsi <= 70:
                return {
                    "signal": "PUT", "trend_15m": trend_15m,
                    "macd_signal": macd_signal, "rsi": rsi,
                    "macd_line": macd_line.iloc[-1], "signal_line": signal_line.iloc[-1],
                    "spread_info": spread_info, "valid": True
                }
    return None

def execute_trade(signal_id):
    if signal_id not in pending_signals:
        return False, "Signal expired"
    signal_data = pending_signals[signal_id]
    if time.time() - signal_data['timestamp'] > signal_timeout:
        del pending_signals[signal_id]
        return False, "Timeout"
    try:
        asset = signal_data['asset']
        signal = signal_data['signal']
        buy_info = api.buy(1, asset, signal.lower(), 120)
        trade_id = buy_info.get("id")
        if trade_id:
            threading.Thread(target=monitor_trade_result, args=(trade_id, asset, signal_id), daemon=True).start()
            signal_data['status'] = 'executed'
            signal_data['trade_id'] = trade_id
            return True, trade_id
        return False, "Order failed"
    except Exception as e:
        return False, str(e)

def monitor_trade_result(trade_id, asset, signal_id):
    time.sleep(125)
    try:
        result = api.check_win(trade_id)
        status = "WON 💰" if result > 0 else "LOST 📉"
        msg = f"📊 *Trade Result*\n\n*Asset:* `{asset}`\n*Status:* {status}\n*Profit:* {result:.2f}"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Menu", callback_data="main_menu"))
        tg_bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)
        if signal_id in pending_signals:
            pending_signals[signal_id]['result'] = {'status': status, 'profit': result}
    except:
        pass

def send_trade_signal(asset, analysis):
    signal_id = str(uuid.uuid4())
    pending_signals[signal_id] = {
        'asset': asset, 'signal': analysis['signal'], 'analysis': analysis,
        'timestamp': time.time(), 'status': 'pending', 'trade_id': None, 'result': None
    }
    signal_type = analysis['signal']
    expiry_time = time.strftime("%H:%M:%S", time.localtime(time.time() + 120))
    
    signal_msg = f"""
🚨 *NEW TRADE SIGNAL*

*Asset:* `{asset}`
*Signal:* {signal_type} {'🟢' if signal_type == 'CALL' else '🔴'}

📈 *15-Minute Trend ✅*
• Price: {analysis['trend_15m']['price']:.5f}
• EMA200: {analysis['trend_15m']['ema_200']:.5f}

⏱️ *2-Minute MACD ✅*
• Type: {analysis['macd_signal']}
• MACD: {analysis['macd_line']:.6f}
• Signal: {analysis['signal_line']:.6f}

📊 *RSI:* {analysis['rsi']:.2f} ✅

🎯 *ALL CONDITIONS MET!*
⏱️ *Expiry:* 2 MIN ({expiry_time})
🕒 *ID:* `{signal_id[:8]}...`

⚠️ *Expires in {signal_timeout}s*
    """
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ ACCEPT", callback_data=f"accept_{signal_id}"),
        types.InlineKeyboardButton("❌ DENY", callback_data=f"deny_{signal_id}")
    )
    markup.add(types.InlineKeyboardButton("📊 Details", callback_data=f"details_{signal_id}"))
    tg_bot.send_message(chat_id, signal_msg, parse_mode="Markdown", reply_markup=markup)
    threading.Thread(target=expire_signal, args=(signal_id,), daemon=True).start()
    return signal_id

def expire_signal(signal_id):
    time.sleep(signal_timeout)
    if signal_id in pending_signals and pending_signals[signal_id]['status'] == 'pending':
        pending_signals[signal_id]['status'] = 'expired'
        asset = pending_signals[signal_id]['asset']
        tg_bot.send_message(chat_id, f"⏰ *Signal Expired*\n\nAsset: `{asset}`", parse_mode="Markdown")

@tg_bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    try:
        tg_bot.answer_callback_query(call.id)
        if call.data.startswith("accept_"):
            signal_id = call.data.split("_")[1]
            success, result = execute_trade(signal_id)
            if success:
                signal_data = pending_signals[signal_id]
                msg = f"✅ *TRADE EXECUTED*\n\n*Asset:* `{signal_data['asset']}`\n*Signal:* {signal_data['signal']}\n*Expiry:* 2 MIN"
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Menu", callback_data="main_menu"))
                tg_bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=markup)
            else:
                tg_bot.edit_message_text(f"❌ *Trade Failed*\n\nReason: {result}", call.message.chat.id, call.message.message_id, parse_mode="Markdown")
        elif call.data.startswith("deny_"):
            signal_id = call.data.split("_")[1]
            if signal_id in pending_signals:
                pending_signals[signal_id]['status'] = 'denied'
                asset = pending_signals[signal_id]['asset']
                tg_bot.edit_message_text(f"❌ *SIGNAL DENIED*\n\nAsset: `{asset}`", call.message.chat.id, call.message.message_id, parse_mode="Markdown")
        elif call.data == "main_menu":
            show_main_menu(call.message)
        elif call.data == "status":
            show_status(call.message)
        elif call.data == "stop":
            bot_running.clear()
            tg_bot.edit_message_text("⏸️ *Stopped*", call.message.chat.id, call.message.message_id, parse_mode="Markdown")
        elif call.data == "start_trading":
            bot_running.set()
            tg_bot.edit_message_text("✅ *Started*", call.message.chat.id, call.message.message_id, parse_mode="Markdown")
        elif call.data == "retest_ssid":
            test_ssid_command(call.message)
    except:
        pass

def get_main_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📊 Status", callback_data="status"),
        types.InlineKeyboardButton("🔍 Test SSID", callback_data="retest_ssid")
    )
    markup.add(
        types.InlineKeyboardButton("▶️ Start", callback_data="start_trading"),
        types.InlineKeyboardButton("⏸️ Stop", callback_data="stop")
    )
    return markup

@tg_bot.message_handler(commands=['start', 'menu'])
def send_welcome(message):
    pending_count = len([s for s in pending_signals.values() if s['status'] == 'pending'])
    cooldown_count = len(spread_rejected)
    
    msg = f"""
🤖 *Trading Bot*

📊 *Status:* {"✅ Running" if bot_running.is_set() else "⏸️ Stopped"}
📨 *Pending:* {pending_count}
⏰ *Cooldowns:* {cooldown_count}

*Commands:*
/test_ssid - Check if SSID is valid
/menu - Show control panel
    """
    tg_bot.send_message(message.chat.id, msg, parse_mode="Markdown", reply_markup=get_main_keyboard())

def show_main_menu(message):
    pending_count = len([s for s in pending_signals.values() if s['status'] == 'pending'])
    msg = f"🤖 *Trading Bot*\n\n📊 *Status:* {'✅ Running' if bot_running.is_set() else '⏸️ Stopped'}\n📨 *Pending:* {pending_count}"
    tg_bot.edit_message_text(msg, message.chat.id, message.message_id, parse_mode="Markdown", reply_markup=get_main_keyboard())

def show_status(message):
    text = f"📊 *Status*\n\nRunning: {'✅' if bot_running.is_set() else '⏸️'}\nPending: {len([s for s in pending_signals.values() if s['status'] == 'pending'])}\nCooldowns: {len(spread_rejected)}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="main_menu"))
    tg_bot.edit_message_text(text, message.chat.id, message.message_id, parse_mode="Markdown", reply_markup=markup)

def run_trading_engine():
    # Test SSID on startup
    print("\n🔍 Testing SSID on startup...")
    results = api.test_ssid()
    passed = sum(1 for t in results["tests"].values() if t.get("success"))
    
    if passed == 0:
        msg = "❌ *SSID INVALID!*\n\nBot cannot connect to Pocket Option.\nUse /test_ssid to check again."
        tg_bot.send_message(chat_id, msg, parse_mode="Markdown")
        return
    
    if api.connect():
        tg_bot.send_message(chat_id, "🚀 *Bot Started!*\n\nUse /test_ssid to verify connection.\nUse /menu for controls.")
        
        while True:
            if bot_running.is_set():
                current_time = time.localtime()
                if current_time.tm_min % 2 == 0 and current_time.tm_sec < 5:
                    try:
                        profits = api.get_all_profit()
                        assets = [a for a, p in profits.items() if p >= 90 and "_otc" in a.lower()]
                        for asset in assets[:10]:
                            analysis = analyze_market(asset)
                            if analysis and analysis.get('valid'):
                                signal_id = send_trade_signal(asset, analysis)
                                print(f"🚨 SIGNAL: {signal_id[:8]} - {asset} - {analysis['signal']}")
                        time.sleep(60)
                    except Exception as e:
                        print(f"❌ Engine Error: {e}")
            time.sleep(1)
    else:
        tg_bot.send_message(chat_id, "❌ Failed to connect to Pocket Option API")

@app.route('/')
def home():
    return "Trading Bot is Running"

@app.route('/health')
def health():
    return {"status": "running" if bot_running.is_set() else "stopped"}

if __name__ == "__main__":
    print("Starting Trading Bot...")
    threading.Thread(target=run_trading_engine, daemon=True).start()
    threading.Thread(target=tg_bot.infinity_polling, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
