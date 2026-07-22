hereimport os
import time
import threading
import json
import re
import uuid
import logging
from flask import Flask, jsonify
import pandas as pd
import telebot
from telebot import types
import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- CONFIG ---
app = Flask(__name__)
bot_running = threading.Event()
bot_running.set()

# Get environment variables
ssid = os.environ.get("PO_SSID")
telegram_token ="8018417452:AAGMj_om_H-Ah18T11YBI2FoZcpcTiK6sAs"
chat_id ="8701685996"

if not telegram_token or not chat_id:
    logger.error("Missing TELEGRAM_TOKEN or CHAT_ID environment variables")
    exit(1)

tg_bot = telebot.TeleBot(telegram_token)

# Store pending signals, user sessions, login states
pending_signals = {}
signal_timeout = 30
user_sessions = {}
pending_login = {}
login_states = {}

# Spread filter settings
MAX_SPREAD_PERCENT = 0.05
MAX_SPREAD_PIPS = 5
SPREAD_COOLDOWN_MINUTES = 60
spread_rejected = {}

# --- POCKET OPTION API CLIENT ---
class PocketOptionClient:
    """Main Pocket Option API Client"""
    
    BASE_URL = "https://pocketoption.com"
    API_URL = "https://pocketoption.com/api"
    
    def __init__(self):
        self.session = None
        self.ssid = None
        self._init_session()
    
    def _init_session(self):
        """Initialize requests session with proper headers"""
        self.session = requests.Session()
        
        # Configure retry strategy
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        # Set headers
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Content-Type': 'application/json',
            'Origin': 'https://pocketoption.com',
            'Referer': 'https://pocketoption.com/',
            'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'Connection': 'keep-alive'
        })
    
    def set_ssid(self, ssid):
        """Set SSID and update headers"""
        self.ssid = ssid
        if ssid:
            self.session.headers.update({
                'Cookie': f'ssid={ssid}'
            })
            # Also set in session cookies
            self.session.cookies.set('ssid', ssid, domain='.pocketoption.com', path='/')
    
    def get_csrf_token(self):
        """Extract CSRF token from page"""
        try:
            response = self.session.get(self.BASE_URL, timeout=15)
            
            # Check cookies
            if 'csrf_token' in response.cookies:
                return response.cookies.get('csrf_token')
            
            # Check meta tags
            csrf_patterns = [
                r'csrf_token["\']?\s*[:=]\s*["\']([^"\']+)["\']',
                r'name="csrf-token" content="([^"]+)"',
                r'data-csrf="([^"]+)"',
                r'<meta[^>]+csrf[^>]+content="([^"]+)"'
            ]
            
            for pattern in csrf_patterns:
                match = re.search(pattern, response.text)
                if match:
                    return match.group(1)
            
            return None
        except Exception as e:
            logger.error(f"Error getting CSRF token: {e}")
            return None
    
    def login(self, email, password):
        """Login to Pocket Option"""
        result = {"success": False, "ssid": None, "account": {}, "error": None}
        
        try:
            # Reset session for fresh login
            self._init_session()
            
            # Get initial page to establish session
            logger.info("Fetching main page...")
            self.session.get(self.BASE_URL, timeout=15)
            
            # Get CSRF token
            csrf_token = self.get_csrf_token()
            logger.info(f"CSRF token: {csrf_token}")
            
            # Prepare login data
            login_data = {
                "email": email,
                "password": password,
                "remember": True
            }
            
            if csrf_token:
                login_data["csrf_token"] = csrf_token
                self.session.headers.update({'X-CSRF-TOKEN': csrf_token})
            
            # Try different login endpoints
            endpoints = [
                f"{self.API_URL}/auth/login",
                f"{self.API_URL}/v2/auth/login",
                f"{self.BASE_URL}/api/auth/login",
                f"{self.API_URL}/login"
            ]
            
            for endpoint in endpoints:
                try:
                    logger.info(f"Attempting login at: {endpoint}")
                    response = self.session.post(endpoint, json=login_data, timeout=15)
                    
                    logger.info(f"Response status: {response.status_code}")
                    
                    if response.status_code == 200:
                        data = response.json()
                        logger.info(f"Login response: {data}")
                        
                        # Extract SSID from various sources
                        ssid = self._extract_ssid(response, data)
                        
                        if ssid:
                            result["ssid"] = ssid
                            self.set_ssid(ssid)
                            
                            # Get profile
                            profile = self.get_profile()
                            if profile:
                                result["account"] = profile
                            
                            result["success"] = True
                            logger.info("Login successful!")
                            return result
                            
                    elif response.status_code == 401:
                        result["error"] = "Invalid email or password"
                        logger.warning("Login failed: Invalid credentials")
                        return result
                        
                except Exception as e:
                    logger.warning(f"Endpoint {endpoint} failed: {e}")
                    continue
            
            # Try alternative login method
            logger.info("Trying alternative login method...")
            result = self._alternative_login(email, password)
            
        except Exception as e:
            result["error"] = f"Login error: {str(e)[:100]}"
            logger.error(f"Login exception: {e}")
        
        return result
    
    def _extract_ssid(self, response, data):
        """Extract SSID from response"""
        # From cookies
        if 'ssid' in response.cookies:
            return response.cookies.get('ssid')
        
        # From response data
        if 'ssid' in data:
            return data.get('ssid')
        elif 'token' in data:
            return data.get('token')
        elif 'session' in data:
            return data.get('session')
        elif 'data' in data and isinstance(data['data'], dict):
            return data['data'].get('ssid') or data['data'].get('token')
        elif 'result' in data and isinstance(data['result'], dict):
            return data['result'].get('ssid') or data['result'].get('token')
        
        # From session cookies
        if 'ssid' in self.session.cookies:
            return self.session.cookies.get('ssid')
        
        return None
    
    def _alternative_login(self, email, password):
        """Alternative login using form data"""
        result = {"success": False, "ssid": None, "account": {}, "error": None}
        
        try:
            # Reset session
            self._init_session()
            
            # Get login page
            self.session.get(self.BASE_URL, timeout=15)
            
            # Prepare form data
            login_data = {
                'email': email,
                'password': password,
                'remember': 'on'
            }
            
            # Change content type for form data
            self.session.headers['Content-Type'] = 'application/x-www-form-urlencoded'
            
            response = self.session.post(
                f"{self.BASE_URL}/login",
                data=login_data,
                timeout=15,
                allow_redirects=True
            )
            
            logger.info(f"Alternative login status: {response.status_code}")
            
            # Check for SSID in cookies
            if 'ssid' in self.session.cookies:
                ssid = self.session.cookies.get('ssid')
                result["ssid"] = ssid
                self.set_ssid(ssid)
                
                # Get profile
                profile = self.get_profile()
                if profile:
                    result["account"] = profile
                
                result["success"] = True
                logger.info("Alternative login successful!")
                return result
            
            result["error"] = "Alternative login failed - no SSID found"
            
        except Exception as e:
            result["error"] = f"Alternative login error: {str(e)[:100]}"
            logger.error(f"Alternative login exception: {e}")
        
        return result
    
    def get_profile(self):
        """Get user profile information"""
        try:
            endpoints = [
                f"{self.API_URL}/profile",
                f"{self.API_URL}/v2/profile",
                f"{self.BASE_URL}/api/profile",
                f"{self.API_URL}/user/profile"
            ]
            
            for endpoint in endpoints:
                try:
                    response = self.session.get(endpoint, timeout=10)
                    if response.status_code == 200:
                        data = response.json()
                        logger.info(f"Profile response: {data}")
                        
                        # Extract profile data
                        if 'id' in data:
                            return {
                                "id": data.get('id'),
                                "balance": data.get('balance'),
                                "demo_balance": data.get('demo_balance'),
                                "currency": data.get('currency', 'USD')
                            }
                        elif 'data' in data and isinstance(data['data'], dict):
                            profile_data = data['data']
                            return {
                                "id": profile_data.get('id'),
                                "balance": profile_data.get('balance'),
                                "demo_balance": profile_data.get('demo_balance'),
                                "currency": profile_data.get('currency', 'USD')
                            }
                        elif 'result' in data and isinstance(data['result'], dict):
                            profile_data = data['result']
                            return {
                                "id": profile_data.get('id'),
                                "balance": profile_data.get('balance'),
                                "demo_balance": profile_data.get('demo_balance'),
                                "currency": profile_data.get('currency', 'USD')
                            }
                except Exception as e:
                    continue
            
            return {}
            
        except Exception as e:
            logger.error(f"Profile error: {e}")
            return {}
    
    def verify_ssid(self, ssid):
        """Verify if SSID is valid"""
        try:
            temp_session = requests.Session()
            temp_session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json',
                'Cookie': f'ssid={ssid}'
            })
            
            response = temp_session.get(f"{self.API_URL}/profile", timeout=10)
            return response.status_code == 200
            
        except Exception as e:
            logger.error(f"SSID verification error: {e}")
            return False
    
    def get_candles(self, asset, timeframe, count, timestamp):
        """Get candle data"""
        try:
            params = {
                'asset': asset,
                'timeframe': timeframe,
                'count': count,
                'timestamp': timestamp
            }
            
            endpoints = [
                f"{self.API_URL}/candles",
                f"{self.API_URL}/v2/candles"
            ]
            
            for endpoint in endpoints:
                try:
                    response = self.session.get(endpoint, params=params, timeout=10)
                    if response.status_code == 200:
                        data = response.json()
                        if 'candles' in data:
                            return data['candles']
                        elif 'data' in data and 'candles' in data['data']:
                            return data['data']['candles']
                        elif isinstance(data, list):
                            return data
                except:
                    continue
            
            return []
            
        except Exception as e:
            logger.error(f"Get candles error: {e}")
            return []
    
    def get_all_profit(self):
        """Get assets profit information"""
        try:
            endpoints = [
                f"{self.API_URL}/assets/profit",
                f"{self.API_URL}/v2/assets/profit"
            ]
            
            for endpoint in endpoints:
                try:
                    response = self.session.get(endpoint, timeout=10)
                    if response.status_code == 200:
                        data = response.json()
                        if 'profit' in data:
                            return data['profit']
                        elif 'data' in data and 'profit' in data['data']:
                            return data['data']['profit']
                        elif isinstance(data, dict):
                            return data
                except:
                    continue
            
            return {}
            
        except Exception as e:
            logger.error(f"Get profit error: {e}")
            return {}
    
    def buy(self, amount, asset, direction, expiry):
        """Place a trade"""
        try:
            trade_data = {
                'amount': amount,
                'asset': asset,
                'direction': direction,
                'expiry': expiry
            }
            
            endpoints = [
                f"{self.API_URL}/trade/open",
                f"{self.API_URL}/v2/trade/open"
            ]
            
            for endpoint in endpoints:
                try:
                    response = self.session.post(endpoint, json=trade_data, timeout=10)
                    if response.status_code == 200:
                        data = response.json()
                        trade_id = None
                        
                        if 'trade_id' in data:
                            trade_id = data['trade_id']
                        elif 'id' in data:
                            trade_id = data['id']
                        elif 'data' in data and 'trade_id' in data['data']:
                            trade_id = data['data']['trade_id']
                        
                        if trade_id:
                            return {'id': str(trade_id), 'success': True}
                except:
                    continue
            
            return {'success': False, 'error': 'Failed to place trade'}
            
        except Exception as e:
            logger.error(f"Buy error: {e}")
            return {'success': False, 'error': str(e)}
    
    def check_win(self, trade_id):
        """Check trade result"""
        try:
            endpoints = [
                f"{self.API_URL}/trade/result/{trade_id}",
                f"{self.API_URL}/v2/trade/result/{trade_id}"
            ]
            
            for endpoint in endpoints:
                try:
                    response = self.session.get(endpoint, timeout=10)
                    if response.status_code == 200:
                        data = response.json()
                        if 'profit' in data:
                            return data['profit']
                        elif 'data' in data and 'profit' in data['data']:
                            return data['data']['profit']
                except:
                    continue
            
            return 0
            
        except Exception as e:
            logger.error(f"Check win error: {e}")
            return 0
    
    def connect(self):
        """Check if connected"""
        try:
            response = self.session.get(f"{self.API_URL}/profile", timeout=10)
            return response.status_code == 200
        except:
            return False

# Initialize clients
pocket_client = PocketOptionClient()
if ssid:
    pocket_client.set_ssid(ssid)

# --- TECHNICAL INDICATORS ---
def calculate_ema(data, period):
    return data.ewm(span=period, adjust=False).mean()

def calculate_macd(data, fast=12, slow=26, signal=9):
    ema_fast = calculate_ema(data, fast)
    ema_slow = calculate_ema(data, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calculate_ema(macd_line, signal)
    return pd.DataFrame({
        'MACD_12_26_9': macd_line,
        'MACDs_12_26_9': signal_line,
        'MACDh_12_26_9': macd_line - signal_line
    })

def calculate_rsi(data, period=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

# --- SIGNAL DETECTION ---
def detect_macd_cross(macd_line, signal_line):
    """Detect MACD cross/touch"""
    if len(macd_line) < 2:
        return "NONE"
    
    prev_diff = macd_line.iloc[-2] - signal_line.iloc[-2]
    curr_diff = macd_line.iloc[-1] - signal_line.iloc[-1]
    
    is_touch = abs(curr_diff) < 0.00001
    was_apart = abs(prev_diff) > 0.00001
    
    if is_touch and was_apart:
        return "TOUCH"
    elif prev_diff < 0 and curr_diff > 0:
        return "BULL_CROSS"
    elif prev_diff > 0 and curr_diff < 0:
        return "BEAR_CROSS"
    
    return "NONE"

def detect_macd_diff_change(macd_line, signal_line):
    """Detect MACD diff direction change"""
    if len(macd_line) < 3:
        return "NONE"
    
    diffs = [macd_line.iloc[i] - signal_line.iloc[i] for i in range(-3, 0)]
    curr_diff, prev_diff, prev_prev_diff = diffs[-1], diffs[-2], diffs[-3]
    
    # BULLISH: Was going down, now going up (while still negative)
    was_decreasing = prev_diff < prev_prev_diff
    now_increasing = curr_diff > prev_diff
    if was_decreasing and now_increasing and curr_diff < 0:
        return "BULLISH_TURN"
    
    # BEARISH: Was going up, now going down (while still positive)
    was_increasing = prev_diff > prev_prev_diff
    now_decreasing = curr_diff < prev_diff
    if was_increasing and now_decreasing and curr_diff > 0:
        return "BEARISH_TURN"
    
    return "NONE"

# --- SPREAD FILTER ---
def calculate_spread(bid, ask):
    if not bid or not ask:
        return None
    spread = ask - bid
    pips = spread * 10000 if bid > 1 else spread * 100
    return {
        'spread_pips': pips,
        'spread_percent': (spread / bid) * 100
    }

def check_spread_filter(asset):
    """Check if spread is acceptable"""
    if asset in spread_rejected:
        if time.time() - spread_rejected[asset] < SPREAD_COOLDOWN_MINUTES * 60:
            return False
        del spread_rejected[asset]
    
    try:
        candles = pocket_client.get_candles(asset, 60, 2, time.time())
        if not candles:
            return True
        
        c = candles[-1]
        info = calculate_spread(c.get('low'), c.get('high'))
        
        if info and (info['spread_percent'] > MAX_SPREAD_PERCENT or info['spread_pips'] > MAX_SPREAD_PIPS):
            spread_rejected[asset] = time.time()
            return False
    except Exception as e:
        logger.error(f"Spread check error: {e}")
    
    return True

# --- MARKET ANALYSIS ---
def get_market_data(asset, tf):
    """Get market data and indicators"""
    try:
        candles = pocket_client.get_candles(asset, tf, 100, time.time())
        if not candles or len(candles) < 50:
            return None, None
        
        df = pd.DataFrame(candles)
        macd = calculate_macd(df['close'])
        rsi = calculate_rsi(df['close']).iloc[-1]
        ema200 = calculate_ema(df['close'], 200).iloc[-1] if len(df) >= 200 else None
        
        return df, {
            "macd": macd,
            "rsi": rsi,
            "ema_200": ema200,
            "price": df['close'].iloc[-1]
        }
    except Exception as e:
        logger.error(f"Market data error: {e}")
        return None, None

def check_15min(asset):
    """Check 15-minute trend"""
    df, ind = get_market_data(asset, 900)
    if not df or not ind or not ind['ema_200']:
        return None
    
    p, e = ind['price'], ind['ema_200']
    if p > e:
        return {"valid": True, "trend": "BULLISH 📈", "direction": "BUY", "price": p, "ema_200": e}
    elif p < e:
        return {"valid": True, "trend": "BEARISH 📉", "direction": "SELL", "price": p, "ema_200": e}
    
    return None

def analyze(asset):
    """Analyze market for signals"""
    if not check_spread_filter(asset):
        return None
    
    trend = check_15min(asset)
    if not trend:
        return None
    
    df, ind = get_market_data(asset, 120)
    if not df:
        return None
    
    macd_line = ind['macd']['MACD_12_26_9']
    signal_line = ind['macd']['MACDs_12_26_9']
    rsi = ind['rsi']
    histogram = ind['macd']['MACDh_12_26_9']
    
    curr_diff = macd_line.iloc[-1] - signal_line.iloc[-1]
    prev_diff = macd_line.iloc[-2] - signal_line.iloc[-2]
    
    # Check signals
    macd_signal = detect_macd_cross(macd_line, signal_line)
    diff_change = detect_macd_diff_change(macd_line, signal_line)
    
    trade_direction = trend['direction']
    
    # Build diffs for display
    diffs = []
    for i in range(-3, 0):
        if i >= -len(macd_line):
            diffs.append(macd_line.iloc[i] - signal_line.iloc[i])
    
    base_data = {
        "trend_15m": trend,
        "rsi": rsi,
        "macd_line": macd_line.iloc[-1],
        "signal_line": signal_line.iloc[-1],
        "curr_diff": curr_diff,
        "prev_diff": prev_diff,
        "histogram": histogram.iloc[-1],
        "prev_histogram": histogram.iloc[-2],
        "diffs": diffs,
        "price": ind['price']
    }
    
    # Pre-Alert: Diff direction change
    if trade_direction == "BUY" and diff_change == "BULLISH_TURN" and 30 <= rsi <= 45:
        return {
            **base_data,
            "type": "PRE_ALERT",
            "signal": "CALL",
            "diff_change": "BULLISH_TURN",
            "reason": "MACD diff turning UP (was falling, now rising)"
        }
    
    if trade_direction == "SELL" and diff_change == "BEARISH_TURN" and 55 <= rsi <= 70:
        return {
            **base_data,
            "type": "PRE_ALERT",
            "signal": "PUT",
            "diff_change": "BEARISH_TURN",
            "reason": "MACD diff turning DOWN (was rising, now falling)"
        }
    
    # Signal: Cross confirmed
    if trade_direction == "BUY" and macd_signal in ["BULL_CROSS", "TOUCH"] and 30 <= rsi <= 45:
        return {
            **base_data,
            "type": "SIGNAL",
            "signal": "CALL",
            "macd_signal": macd_signal,
            "valid": True
        }
    
    if trade_direction == "SELL" and macd_signal in ["BEAR_CROSS", "TOUCH"] and 55 <= rsi <= 70:
        return {
            **base_data,
            "type": "SIGNAL",
            "signal": "PUT",
            "macd_signal": macd_signal,
            "valid": True
        }
    
    return None

# --- TRADE EXECUTION ---
def execute_trade(signal_id):
    """Execute a trade"""
    if signal_id not in pending_signals:
        return False, "Signal not found"
    
    sd = pending_signals[signal_id]
    if sd['status'] != 'approved':
        return False, "Not approved"
    
    try:
        result = pocket_client.buy(1, sd['asset'], sd['signal'].lower(), 120)
        
        if result.get('success') and result.get('id'):
            tid = result['id']
            sd['status'] = 'executed'
            sd['trade_id'] = tid
            threading.Thread(target=monitor_trade_result, args=(tid, sd['asset'], signal_id), daemon=True).start()
            return True, tid
        
        return False, result.get('error', 'Order failed')
    except Exception as e:
        return False, str(e)

def monitor_trade_result(trade_id, asset, signal_id):
    """Monitor trade result"""
    time.sleep(125)
    try:
        result = pocket_client.check_win(trade_id)
        status = "WON 💰" if result > 0 else "LOST 📉"
        
        msg = f"📊 *Trade Result*\n\n*Asset:* `{asset}`\n*Status:* {status}\n*Profit:* {result:.2f}"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu"))
        tg_bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)
        
        if signal_id in pending_signals:
            pending_signals[signal_id]['result'] = {'status': status, 'profit': result}
    except Exception as e:
        logger.error(f"Monitor trade error: {e}")

# --- SIGNAL SENDING ---
def send_pre_alert(asset, analysis):
    """Send pre-alert notification"""
    alert_id = str(uuid.uuid4())
    
    diffs = analysis.get('diffs', [])
    diff_progression = " → ".join([f"`{d:.6f}`" for d in diffs if d is not None])
    
    if analysis['signal'] == "CALL":
        direction_emoji = "🟢"
        change_text = "NEGATIVE → turning UP ⚡"
        expected = "BULLISH CROSS expected"
    else:
        direction_emoji = "🔴"
        change_text = "POSITIVE → turning DOWN ⚡"
        expected = "BEARISH CROSS expected"
    
    msg = f"""
⚠️ *PRE-ALERT: MACD Direction Change!*

*Asset:* `{asset}`
*Expected:* {analysis['signal']} {direction_emoji}

📈 *15-Min Trend ✅* ({analysis['trend_15m']['trend']})
• Price: {analysis.get('price', analysis['trend_15m']['price']):.5f}
• EMA200: {analysis['trend_15m']['ema_200']:.5f}

⏱️ *MACD Diff Turning! ⚡*
• {change_text}
• MACD: {analysis['macd_line']:.6f}
• Signal: {analysis['signal_line']:.6f}
• Current Diff: {analysis['curr_diff']:.6f}
• Previous Diff: {analysis['prev_diff']:.6f}

📉 *Diff Progression:*
{diff_progression}

📊 *RSI:* {analysis['rsi']:.2f} ✅

🔔 *First sign of reversal!*
⏳ *{expected}*
"""
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("👀 Watching for Cross", callback_data=f"watching_{alert_id}"))
    
    tg_bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)

def send_signal_for_approval(asset, analysis):
    """Send signal for manual approval"""
    signal_id = str(uuid.uuid4())
    pending_signals[signal_id] = {
        'asset': asset,
        'signal': analysis['signal'],
        'analysis': analysis,
        'timestamp': time.time(),
        'status': 'pending'
    }
    
    expiry_time = time.strftime("%H:%M:%S", time.localtime(time.time() + 120))
    
    msg = f"""
🚨 *TRADE SIGNAL - CONFIRMED*

*Asset:* `{asset}`
*Signal:* {analysis['signal']} {'🟢 CALL' if analysis['signal'] == 'CALL' else '🔴 PUT'}

📈 *15-Min Trend ✅* ({analysis['trend_15m']['trend']})
• Price: {analysis.get('price', analysis['trend_15m']['price']):.5f}
• EMA200: {analysis['trend_15m']['ema_200']:.5f}

⏱️ *MACD Cross ✅* ({analysis['macd_signal']})
• MACD: {analysis['macd_line']:.6f}
• Signal: {analysis['signal_line']:.6f}

📊 *RSI:* {analysis['rsi']:.2f} ✅

🎯 *ALL CONDITIONS MET!*
⏱️ *Expiry:* 2 MIN ({expiry_time})
⚠️ *Expires in {signal_timeout}s*
"""
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ APPROVE", callback_data=f"approve_{signal_id}"),
        types.InlineKeyboardButton("❌ DENY", callback_data=f"deny_{signal_id}")
    )
    markup.add(types.InlineKeyboardButton("📊 Details", callback_data=f"details_{signal_id}"))
    
    tg_bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)
    threading.Thread(target=expire_signal, args=(signal_id,), daemon=True).start()
    
    return signal_id

def expire_signal(signal_id):
    """Expire pending signal"""
    time.sleep(signal_timeout)
    if signal_id in pending_signals and pending_signals[signal_id]['status'] == 'pending':
        pending_signals[signal_id]['status'] = 'expired'
        try:
            tg_bot.send_message(
                chat_id,
                f"⏰ *Signal Expired*\n\nAsset: `{pending_signals[signal_id]['asset']}`",
                parse_mode="Markdown"
            )
        except:
            pass

# --- TELEGRAM CALLBACK HANDLER ---
@tg_bot.callback_query_handler(func=lambda call: True)
def handle_all_callbacks(call):
    try:
        data = call.data
        
        # Signal approval
        if data.startswith("approve_"):
            sid = data.split("_")[1]
            if sid in pending_signals and pending_signals[sid]['status'] == 'pending':
                pending_signals[sid]['status'] = 'approved'
                tg_bot.answer_callback_query(call.id, "✅ Approved! Executing...", show_alert=True)
                tg_bot.edit_message_text(
                    f"✅ *APPROVED!*\n\nExecuting trade for `{pending_signals[sid]['asset']}`...",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode="Markdown"
                )
                success, result = execute_trade(sid)
                if success:
                    tg_bot.send_message(
                        call.message.chat.id,
                        f"✅ *TRADE EXECUTED!*\n\n*Asset:* `{pending_signals[sid]['asset']}`\n*Signal:* {pending_signals[sid]['signal']}\n*ID:* `{result}`\n*Expiry:* 2 MIN",
                        parse_mode="Markdown"
                    )
                else:
                    tg_bot.send_message(
                        call.message.chat.id,
                        f"❌ *Failed:* {result}",
                        parse_mode="Markdown"
                    )
            else:
                tg_bot.answer_callback_query(call.id, "Signal expired!", show_alert=True)
        
        elif data.startswith("deny_"):
            sid = data.split("_")[1]
            if sid in pending_signals:
                pending_signals[sid]['status'] = 'denied'
                tg_bot.answer_callback_query(call.id, "❌ Denied", show_alert=True)
                tg_bot.edit_message_text(
                    f"❌ *SIGNAL DENIED*\n\nAsset: `{pending_signals[sid]['asset']}`",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode="Markdown"
                )
        
        elif data.startswith("details_"):
            sid = data.split("_")[1]
            if sid in pending_signals:
                a = pending_signals[sid]['analysis']
                msg = f"📊 *Details*\n\nAsset: `{pending_signals[sid]['asset']}`\nSignal: {pending_signals[sid]['signal']}\n\n📈 Price: {a.get('price', a['trend_15m']['price']):.5f}\n📈 EMA200: {a['trend_15m']['ema_200']:.5f}\n⏱️ MACD: {a.get('macd_signal', 'N/A')}\n📊 RSI: {a['rsi']:.2f}\n📊 Diff: {a['curr_diff']:.6f}"
                tg_bot.answer_callback_query(call.id, "Details shown", show_alert=True)
                tg_bot.send_message(call.message.chat.id, msg, parse_mode="Markdown")
        
        # Main menu
        elif data == "main_menu":
            show_main_menu(call.message)
        
        # Login flow
        elif data == "start_login":
            login_states[call.message.chat.id] = {"step": "waiting_email"}
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_login"))
            tg_bot.edit_message_text(
                "📧 *Enter your Pocket Option email:*",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=markup
            )
        
        elif data == "cancel_login":
            login_states.pop(call.message.chat.id, None)
            pending_login.pop(call.message.chat.id, None)
            tg_bot.edit_message_text(
                "❌ Login cancelled.",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown"
            )
            show_main_menu_after(call.message)
        
        # Status
        elif data == "view_status":
            show_status(call.message)
        
        elif data == "view_session":
            if call.message.chat.id in user_sessions:
                s = user_sessions[call.message.chat.id]
                valid = pocket_client.verify_ssid(s["ssid"])
                acc = s.get("account", {})
                pending = len([x for x in pending_signals.values() if x['status'] == 'pending'])
                msg = f"📊 *Session*\n\n*Email:* `{s.get('email', 'N/A')}`\n*SSID:* {'✅ Valid' if valid else '❌ Expired'}\n*Account:* `{acc.get('id', 'N/A')}`\n*Balance:* ${acc.get('balance', 'N/A')}\n*Pending:* {pending}"
            else:
                msg = "❌ *No active session*"
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu"))
            tg_bot.edit_message_text(
                msg,
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=markup
            )
        
        # Test SSID
        elif data == "test_ssid":
            tg_bot.answer_callback_query(call.id, "Testing...", show_alert=False)
            if pocket_client.ssid:
                valid = pocket_client.verify_ssid(pocket_client.ssid)
                if valid:
                    profile = pocket_client.get_profile()
                    msg = f"✅ *SSID VALID!*\n\nAccount: `{profile.get('id')}`\nBalance: `${profile.get('balance')}`"
                else:
                    msg = "❌ *SSID Invalid!*"
            else:
                msg = "❌ *No SSID set!*"
            
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu"),
                types.InlineKeyboardButton("🔑 Login", callback_data="start_login")
            )
            tg_bot.edit_message_text(
                msg,
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=markup
            )
        
        # Trading controls
        elif data == "start_trading":
            bot_running.set()
            tg_bot.answer_callback_query(call.id, "✅ Signals Started!", show_alert=True)
            msg = "✅ *Trading Signals ACTIVE*\n\n⚠️ Pre-Alert: MACD diff change\n🚨 Signal: MACD cross + RSI\n\nYou must APPROVE each trade!"
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("⏸️ Stop", callback_data="stop_trading"),
                types.InlineKeyboardButton("🏠 Menu", callback_data="main_menu")
            )
            tg_bot.edit_message_text(
                msg,
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=markup
            )
        
        elif data == "stop_trading":
            bot_running.clear()
            tg_bot.answer_callback_query(call.id, "⏸️ Stopped", show_alert=True)
            msg = "⏸️ *Signals STOPPED*"
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("▶️ Start", callback_data="start_trading"),
                types.InlineKeyboardButton("🏠 Menu", callback_data="main_menu")
            )
            tg_bot.edit_message_text(
                msg,
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=markup
            )
        
        # Logout
        elif data == "logout":
            user_sessions.pop(call.message.chat.id, None)
            pocket_client.set_ssid(None)
            tg_bot.answer_callback_query(call.id, "✅ Logged out", show_alert=True)
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("🔑 Login", callback_data="start_login"),
                types.InlineKeyboardButton("🏠 Menu", callback_data="main_menu")
            )
            tg_bot.edit_message_text(
                "✅ *Logged out*",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=markup
            )
        
        # Spread info
        elif data == "spread_info":
            try:
                profits = pocket_client.get_all_profit()
                assets = [a for a, p in profits.items() if p >= 90 and "_otc" in a.lower()]
                msg = "💰 *Spread Info*\n\n"
                for asset in assets[:5]:
                    candles = pocket_client.get_candles(asset, 60, 1, time.time())
                    if candles:
                        info = calculate_spread(candles[-1].get('low'), candles[-1].get('high'))
                        if info:
                            msg += f"• `{asset}`: {info['spread_pips']:.1f} pips\n"
                msg += f"\n⏰ Cooldowns: {len(spread_rejected)}"
            except Exception as e:
                msg = f"❌ Error: {e}"
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🏠 Menu", callback_data="main_menu"))
            tg_bot.edit_message_text(
                msg,
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=markup
            )
        
        # Cooldowns
        elif data == "cooldowns":
            if not spread_rejected:
                msg = "⏰ *No cooldowns!*"
            else:
                msg = "⏰ *Cooldowns*\n\n"
                for asset, rt in spread_rejected.items():
                    remaining = (SPREAD_COOLDOWN_MINUTES * 60) - (time.time() - rt)
                    msg += f"• `{asset}`: {max(0, remaining/60):.0f}min\n"
            
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("🔄 Clear", callback_data="clear_cooldowns"),
                types.InlineKeyboardButton("🏠 Menu", callback_data="main_menu")
            )
            tg_bot.edit_message_text(
                msg,
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=markup
            )
        
        elif data == "clear_cooldowns":
            spread_rejected.clear()
            tg_bot.answer_callback_query(call.id, "✅ Cleared!", show_alert=True)
            show_main_menu(call.message)
        
        # Help
        elif data == "help":
            msg = """
🤖 *How it works:*

⚡ *Pre-Alert:* MACD diff changes direction
🚨 *Signal:* MACD crosses + RSI confirmed

*Strategy:*
• 15min Trend (Price vs EMA200)
• 2min MACD Cross/Touch
• RSI (30-45 BUY / 55-70 SELL)
• Expiry: 2 MINUTES

*You control every trade!*
"""
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu"))
            tg_bot.edit_message_text(
                msg,
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=markup
            )
        
        # Watching for cross
        elif data.startswith("watching_"):
            tg_bot.answer_callback_query(call.id, "👀 Watching for confirmation...", show_alert=False)
        
    except Exception as e:
        logger.error(f"Callback error: {e}")
        tg_bot.answer_callback_query(call.id, f"Error: {str(e)[:50]}", show_alert=True)

# --- TELEGRAM MESSAGE HANDLER ---
@tg_bot.message_handler(func=lambda message: True)
def handle_text_messages(message):
    cid = message.chat.id
    text = message.text.strip()
    
    if cid in login_states:
        state = login_states[cid]
        
        if state["step"] == "waiting_email" and "@" in text:
            pending_login[cid] = {"email": text}
            state["step"] = "waiting_password"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_login"))
            tg_bot.send_message(
                cid,
                f"📧 *Email:* `{text}`\n\n🔐 Send your password:",
                parse_mode="Markdown",
                reply_markup=markup
            )
            return
        
        elif state["step"] == "waiting_password":
            try:
                tg_bot.delete_message(cid, message.message_id)
            except:
                pass
            
            password = text
            email = pending_login[cid]["email"]
            
            status_msg = tg_bot.send_message(cid, "🔑 *Logging in...*", parse_mode="Markdown")
            
            result = pocket_client.login(email, password)
            
            if result["success"] and result["ssid"]:
                user_sessions[cid] = {
                    "ssid": result["ssid"],
                    "email": email,
                    "account": result["account"]
                }
                pocket_client.set_ssid(result["ssid"])
                
                acc = result["account"]
                msg = f"✅ *LOGIN SUCCESSFUL!*\n\n*Account:* `{acc.get('id')}`\n*Balance:* ${acc.get('balance')}\n*Demo:* ${acc.get('demo_balance')}"
                markup = types.InlineKeyboardMarkup(row_width=2)
                markup.add(
                    types.InlineKeyboardButton("▶️ Start Signals", callback_data="start_trading"),
                    types.InlineKeyboardButton("🏠 Menu", callback_data="main_menu")
                )
                tg_bot.send_message(cid, msg, parse_mode="Markdown", reply_markup=markup)
            else:
                msg = f"❌ *Login Failed*\n\n{result.get('error', 'Unknown error')}"
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔑 Try Again", callback_data="start_login"))
                tg_bot.send_message(cid, msg, parse_mode="Markdown", reply_markup=markup)
            
            pending_login.pop(cid, None)
            login_states.pop(cid, None)
            return
    
    send_welcome(message)

# --- MAIN MENU FUNCTIONS ---
def get_main_keyboard():
    running = bot_running.is_set()
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🔑 Login", callback_data="start_login"),
        types.InlineKeyboardButton("📊 Session", callback_data="view_session")
    )
    markup.add(
        types.InlineKeyboardButton("⏸️ Stop" if running else "▶️ Start Signals", callback_data="stop_trading" if running else "start_trading"),
        types.InlineKeyboardButton("🔍 Test SSID", callback_data="test_ssid")
    )
    markup.add(
        types.InlineKeyboardButton("💰 Spread", callback_data="spread_info"),
        types.InlineKeyboardButton("⏰ Cooldowns", callback_data="cooldowns")
    )
    markup.add(
        types.InlineKeyboardButton("🚪 Logout", callback_data="logout"),
        types.InlineKeyboardButton("❓ Help", callback_data="help")
    )
    markup.add(types.InlineKeyboardButton("📊 Bot Status", callback_data="view_status"))
    return markup

def send_welcome(message):
    pending = len([s for s in pending_signals.values() if s['status'] == 'pending'])
    has_session = message.chat.id in user_sessions or bool(pocket_client.ssid)
    
    msg = f"""
🤖 *TRADING BOT*

📊 Running: {'✅' if bot_running.is_set() else '⏸️'}
🔑 Logged In: {'✅' if has_session else '❌'}
📨 Pending: {pending}

⚡ *Pre-Alert:* MACD diff change
🚨 *Signal:* MACD cross + RSI
⚠️ *Manual approval required!*
"""
    tg_bot.send_message(
        message.chat.id,
        msg,
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

def show_main_menu(message):
    send_welcome(message)

def show_main_menu_after(message):
    send_welcome(message)

def show_status(message):
    running = bot_running.is_set()
    pending = len([s for s in pending_signals.values() if s['status'] == 'pending'])
    approved = len([s for s in pending_signals.values() if s['status'] == 'approved'])
    executed = len([s for s in pending_signals.values() if s['status'] == 'executed'])
    
    msg = f"""
📊 *STATUS*

Running: {'✅' if running else '⏸️'}
📨 Pending: {pending}
✅ Approved: {approved}
💼 Executed: {executed}
⏰ Cooldowns: {len(spread_rejected)}
"""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu"))
    tg_bot.edit_message_text(
        msg,
        message.chat.id,
        message.message_id,
        parse_mode="Markdown",
        reply_markup=markup
    )

# --- MAIN ENGINE ---
def run_trading_engine():
    """Main trading engine loop"""
    try:
        if pocket_client.ssid and pocket_client.connect():
            logger.info("Connected with existing SSID")
            tg_bot.send_message(
                chat_id,
                "🚀 *Bot Started!*\n\n⚡ *Pre-Alert:* MACD diff turns\n🚨 *Signal:* MACD crosses + RSI valid\n⚠️ *You must APPROVE each trade!*\n\nSend /start for menu.",
                parse_mode="Markdown"
            )
        else:
            logger.warning("No valid SSID found. Waiting for login...")
        
        while True:
            if bot_running.is_set() and pocket_client.ssid:
                t = time.localtime()
                # Run every 2 minutes at :00, :02, :04, etc. (even minutes)
                if t.tm_min % 2 == 0 and t.tm_sec < 5:
                    try:
                        logger.info("Scanning for signals...")
                        profits = pocket_client.get_all_profit()
                        
                        if profits:
                            # Filter assets with profit >= 90 and OTC
                            assets = [a for a, p in profits.items() if p >= 90 and "_otc" in a.lower()]
                            logger.info(f"Found {len(assets)} OTC assets")
                            
                            for asset in assets[:10]:
                                analysis = analyze(asset)
                                if analysis:
                                    if analysis['type'] == 'PRE_ALERT':
                                        send_pre_alert(asset, analysis)
                                        logger.info(f"⚡ Pre-Alert: {asset} - {analysis['signal']}")
                                    elif analysis['type'] == 'SIGNAL' and analysis.get('valid'):
                                        send_signal_for_approval(asset, analysis)
                                        logger.info(f"🚨 Signal: {asset} - {analysis['signal']}")
                            
                            time.sleep(60)  # Wait 1 minute after scanning
                        else:
                            logger.warning("No profit data received")
                            
                    except Exception as e:
                        logger.error(f"Scan error: {e}")
                
                time.sleep(1)
            else:
                # Wait if bot is stopped or no SSID
                if not pocket_client.ssid:
                    logger.info("No SSID - waiting for login...")
                time.sleep(5)
                
    except Exception as e:
        logger.error(f"Engine error: {e}")

# --- FLASK ROUTES ---
@app.route('/')
def home():
    return jsonify({
        "status": "running",
        "bot_running": bot_running.is_set(),
        "ssid_set": bool(pocket_client.ssid),
        "pending_signals": len(pending_signals)
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

@app.route('/status')
def status():
    return jsonify({
        "running": bot_running.is_set(),
        "ssid_valid": pocket_client.verify_ssid(pocket_client.ssid) if pocket_client.ssid else False,
        "pending_signals": len(pending_signals),
        "cooldowns": len(spread_rejected),
        "sessions": len(user_sessions)
    })

# --- MAIN ---
if __name__ == "__main__":
    try:
        logger.info("Starting Trading Bot...")
        
        # Start bot threads
        bot_thread = threading.Thread(target=tg_bot.infinity_polling, daemon=True)
        bot_thread.start()
        logger.info("Telegram bot started")
        
        engine_thread = threading.Thread(target=run_trading_engine, daemon=True)
        engine_thread.start()
        logger.info("Trading engine started")
        
        # Start Flask
        port = int(os.environ.get("PORT", 8080))
        logger.info(f"Starting Flask server on port {port}")
        app.run(host="0.0.0.0", port=port, debug=False)
        
    except Exception as e:
        logger.error(f"Main error: {e}")
