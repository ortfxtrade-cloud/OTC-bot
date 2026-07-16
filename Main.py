import os
import time
import threading
from flask import Flask
import pandas as pd
import telebot
from telebot import types
from pocketoptionapi_async import AsyncPocketOptionClient
import ta
import uuid

# --- CONFIG ---
app = Flask(__name__)
bot_running = threading.Event()
bot_running.set() 

ssid = os.environ.get("PO_SSID")
telegram_token = os.environ.get("TELEGRAM_TOKEN")
chat_id = os.environ.get("CHAT_ID")
tg_bot = telebot.TeleBot(telegram_token)

# Initialize API client
api = AsyncPocketOptionClient(ssid)

# Store pending signals waiting for approval
pending_signals = {}
signal_timeout = 30  # seconds before signal expires

# --- CORE LOGIC ---
def get_market_data(asset, timeframe_seconds):
    """Get market data for specified timeframe"""
    candles = api.get_candles(asset, timeframe_seconds, 100, time.time())
    if not candles: 
        return None, None
    
    df = pd.DataFrame(candles)
    
    if len(df) < 50:
        return None, None
    
    # Calculate indicators
    macd = ta.macd(df['close'])
    rsi = ta.rsi(df['close'], length=14).iloc[-1]
    ema_200 = ta.ema(df['close'], length=200).iloc[-1] if len(df) >= 200 else None
    
    return df, {
        "macd": macd, 
        "rsi": rsi, 
        "ema_200": ema_200,
        "price": df['close'].iloc[-1],
        "close": df['close']
    }

def check_macd_cross(macd_df):
    """Check if MACD just crossed (current candle)"""
    if len(macd_df) < 3:
        return None
    
    macd_line = macd_df['MACD_12_26_9']
    signal_line = macd_df['MACDs_12_26_9']
    
    # Current values
    current_macd = macd_line.iloc[-1]
    current_signal = signal_line.iloc[-1]
    
    # Previous values
    prev_macd = macd_line.iloc[-2]
    prev_signal = signal_line.iloc[-2]
    
    # Check for bullish cross (MACD crosses above signal)
    if prev_macd <= prev_signal and current_macd > current_signal:
        return "BULLISH_CROSS"
    
    # Check for bearish cross (MACD crosses below signal)
    if prev_macd >= prev_signal and current_macd < current_signal:
        return "BEARISH_CROSS"
    
    return None

def check_15min_trend(asset):
    """Check 15-minute trend - Price must be above/below EMA200"""
    df_15m, ind_15m = get_market_data(asset, 900)  # 900 seconds = 15 minutes
    
    if df_15m is None or ind_15m is None or ind_15m['ema_200'] is None:
        return {
            "valid": False,
            "trend": "Unknown",
            "reason": "Unable to fetch 15-minute data or insufficient data for EMA200",
            "indicators": ind_15m
        }
    
    price_15m = ind_15m['price']
    ema_200_15m = ind_15m['ema_200']
    
    # Determine trend
    if price_15m > ema_200_15m:
        price_vs_ema = ((price_15m - ema_200_15m) / ema_200_15m) * 100
        return {
            "valid": True,
            "trend": "BULLISH 📈",
            "direction": "BUY",
            "reason": f"Price ({price_15m:.5f}) is ABOVE EMA200 ({ema_200_15m:.5f}) by {price_vs_ema:.2f}%",
            "indicators": ind_15m,
            "price_vs_ema_percent": price_vs_ema
        }
    elif price_15m < ema_200_15m:
        price_vs_ema = ((ema_200_15m - price_15m) / ema_200_15m) * 100
        return {
            "valid": True,
            "trend": "BEARISH 📉",
            "direction": "SELL",
            "reason": f"Price ({price_15m:.5f}) is BELOW EMA200 ({ema_200_15m:.5f}) by {price_vs_ema:.2f}%",
            "indicators": ind_15m,
            "price_vs_ema_percent": price_vs_ema
        }
    else:
        return {
            "valid": False,
            "trend": "NEUTRAL",
            "direction": "NONE",
            "reason": f"Price ({price_15m:.5f}) is AT EMA200 ({ema_200_15m:.5f})",
            "indicators": ind_15m,
            "price_vs_ema_percent": 0
        }

def check_2min_macd_cross(asset, expected_cross_direction):
    """Check 2-minute timeframe for MACD cross"""
    df, ind = get_market_data(asset, 120)  # 120 seconds = 2 minutes
    
    if df is None or ind is None:
        return {
            "cross_detected": False,
            "reason": "Unable to fetch 2-minute data"
        }
    
    cross = check_macd_cross(ind['macd'])
    
    if cross is None:
        return {
            "cross_detected": False,
            "reason": "No MACD cross on 2min timeframe",
            "indicators": ind
        }
    
    if cross == expected_cross_direction:
        rsi = ind['rsi']
        macd_diff = abs(ind['macd']['MACD_12_26_9'].iloc[-1] - ind['macd']['MACDs_12_26_9'].iloc[-1])
        
        return {
            "cross_detected": True,
            "cross_type": cross,
            "rsi": rsi,
            "macd_diff": macd_diff,
            "indicators": ind,
            "reason": f"✅ {expected_cross_direction} on 2min timeframe"
        }
    else:
        return {
            "cross_detected": False,
            "cross_type": cross,
            "reason": f"❌ Wrong cross direction on 2min (got {cross}, expected {expected_cross_direction})",
            "indicators": ind
        }

def check_rsi_conditions(rsi_value, trade_direction):
    """Check if RSI is in correct range"""
    if trade_direction == "CALL":
        if 30 <= rsi_value <= 45:
            return True, f"✅ RSI ({rsi_value:.2f}) is in BUY zone (30-45)"
        else:
            return False, f"❌ RSI ({rsi_value:.2f}) is NOT in BUY zone (30-45)"
    else:
        if 55 <= rsi_value <= 70:
            return True, f"✅ RSI ({rsi_value:.2f}) is in SELL zone (55-70)"
        else:
            return False, f"❌ RSI ({rsi_value:.2f}) is NOT in SELL zone (55-70)"

def analyze_market(asset):
    """Main analysis following the strategy"""
    
    trend_15m = check_15min_trend(asset)
    
    if not trend_15m['valid']:
        return None
    
    trade_direction = trend_15m['direction']
    
    if trade_direction == "BUY":
        signal_type = "CALL"
        expected_cross = "BULLISH_CROSS"
    else:
        signal_type = "PUT"
        expected_cross = "BEARISH_CROSS"
    
    cross_result = check_2min_macd_cross(asset, expected_cross)
    
    if not cross_result['cross_detected']:
        return None
    
    rsi_valid, rsi_msg = check_rsi_conditions(cross_result['rsi'], signal_type)
    
    if not rsi_valid:
        return None
    
    return {
        "signal": signal_type,
        "reason": f"All conditions met for {signal_type}",
        "trend_15m": trend_15m,
        "cross_result": cross_result,
        "rsi_result": {"valid": True, "message": rsi_msg, "rsi": cross_result['rsi']},
        "valid": True
    }

def execute_trade(signal_id):
    """Execute a confirmed trade with 2-minute expiry"""
    if signal_id not in pending_signals:
        return False, "Signal expired or not found"
    
    signal_data = pending_signals[signal_id]
    
    if time.time() - signal_data['timestamp'] > signal_timeout:
        del pending_signals[signal_id]
        return False, "Signal timeout expired"
    
    try:
        asset = signal_data['asset']
        signal = signal_data['signal']
        
        buy_info = api.buy(1, asset, signal.lower(), 120)
        trade_id = buy_info.get("id")
        
        if trade_id:
            threading.Thread(
                target=monitor_trade_result,
                args=(trade_id, asset, signal_id),
                daemon=True
            ).start()
            
            signal_data['status'] = 'executed'
            signal_data['trade_id'] = trade_id
            
            return True, trade_id
        else:
            return False, "Failed to place order"
            
    except Exception as e:
        return False, str(e)

def monitor_trade_result(trade_id, asset, signal_id):
    """Waits for the trade to finish and reports result."""
    time.sleep(125)
    
    try:
        result = api.check_win(trade_id)
        status = "WON 💰" if result > 0 else "LOST 📉"
        
        trade_msg = f"""
📊 *Trade Result*

*Asset:* `{asset}`
*Signal ID:* `{signal_id[:8]}...`
*Status:* {status}
*Profit:* {result:.2f}
*Expiry:* 2 Minutes

Use /menu for more options
        """
        
        markup = types.InlineKeyboardMarkup(row_width=2)
        btn_history = types.InlineKeyboardButton("📈 History", callback_data="history")
        btn_menu = types.InlineKeyboardButton("🔙 Menu", callback_data="main_menu")
        markup.add(btn_history, btn_menu)
        
        tg_bot.send_message(chat_id, trade_msg, parse_mode="Markdown", reply_markup=markup)
        
        if signal_id in pending_signals:
            pending_signals[signal_id]['result'] = {
                'status': status,
                'profit': result
            }
            
    except Exception as e:
        error_msg = f"❌ Error checking trade result: {str(e)}"
        tg_bot.send_message(chat_id, error_msg)

def send_trade_signal(asset, analysis):
    """Send trade signal for approval"""
    signal_id = str(uuid.uuid4())
    
    pending_signals[signal_id] = {
        'asset': asset,
        'signal': analysis['signal'],
        'analysis': analysis,
        'timestamp': time.time(),
        'status': 'pending',
        'trade_id': None,
        'result': None
    }
    
    trend_15m = analysis['trend_15m']
    signal_type = analysis['signal']
    cross_result = analysis['cross_result']
    rsi_result = analysis['rsi_result']
    
    expiry_time = time.strftime("%H:%M:%S", time.localtime(time.time() + 120))
    
    signal_msg = f"""
🚨 *NEW TRADE SIGNAL*

*Asset:* `{asset}`
*Signal:* {signal_type} {'🟢' if signal_type == 'CALL' else '🔴'}

📈 *Step 1: 15-Minute Trend*
• {trend_15m['reason']}
• Price: {trend_15m['indicators']['price']:.5f}
• EMA200: {trend_15m['indicators']['ema_200']:.5f}

⏱️ *Step 2: 2-Minute MACD Cross*
• Status: ✅ Cross Detected
• Type: {cross_result['cross_type']}
• MACD Diff: {cross_result['macd_diff']:.6f}

📊 *Step 3: RSI Confirmation*
• {rsi_result['message']}

🎯 *Signal:* ✅ VALID
⏱️ *Expiry:* 2 MIN ({expiry_time})
🕒 *Signal ID:* `{signal_id[:8]}...`

⚠️ *This signal will expire in {signal_timeout} seconds*
    """
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    btn_accept = types.InlineKeyboardButton("✅ ACCEPT TRADE", callback_data=f"accept_{signal_id}")
    btn_deny = types.InlineKeyboardButton("❌ DENY", callback_data=f"deny_{signal_id}")
    btn_details = types.InlineKeyboardButton("📊 Details", callback_data=f"details_{signal_id}")
    btn_trend = types.InlineKeyboardButton("📈 15m Chart", callback_data=f"trend_{signal_id}")
    
    markup.add(btn_accept, btn_deny)
    markup.add(btn_details, btn_trend)
    
    tg_bot.send_message(chat_id, signal_msg, parse_mode="Markdown", reply_markup=markup)
    
    threading.Thread(target=expire_signal, args=(signal_id,), daemon=True).start()
    
    return signal_id

def expire_signal(signal_id):
    """Auto-expire signal after timeout"""
    time.sleep(signal_timeout)
    
    if signal_id in pending_signals and pending_signals[signal_id]['status'] == 'pending':
        pending_signals[signal_id]['status'] = 'expired'
        
        asset = pending_signals[signal_id]['asset']
        expire_msg = f"⏰ *Signal Expired*\n\nAsset: `{asset}`\nSignal ID: `{signal_id[:8]}...`\n\nThis signal has expired."
        
        tg_bot.send_message(chat_id, expire_msg, parse_mode="Markdown")

@tg_bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    """Handle all callback queries"""
    try:
        tg_bot.answer_callback_query(call.id)
        
        if call.data.startswith("accept_"):
            signal_id = call.data.split("_")[1]
            handle_accept_signal(call, signal_id)
            
        elif call.data.startswith("deny_"):
            signal_id = call.data.split("_")[1]
            handle_deny_signal(call, signal_id)
            
        elif call.data.startswith("details_"):
            signal_id = call.data.split("_")[1]
            show_signal_details(call.message, signal_id)
            
        elif call.data.startswith("trend_"):
            signal_id = call.data.split("_")[1]
            show_trend_details(call.message, signal_id)
            
        elif call.data == "main_menu":
            show_main_menu(call.message)
        elif call.data == "status":
            show_status(call.message)
        elif call.data == "profit":
            show_profit(call.message)
        elif call.data == "stop":
            show_confirmation(call.message, "stop")
        elif call.data == "start_trading":
            show_confirmation(call.message, "start")
        elif call.data == "history":
            show_trade_history(call.message)
        elif call.data == "confirm_stop":
            execute_stop(call.message)
        elif call.data == "confirm_start":
            execute_start(call.message)
        elif call.data == "cancel":
            show_main_menu(call.message)
        elif call.data == "close":
            tg_bot.delete_message(call.message.chat.id, call.message.message_id)
        elif call.data == "refresh":
            refresh_current_view(call.message)
            
    except Exception as e:
        tg_bot.answer_callback_query(call.id, f"Error: {str(e)}", show_alert=True)

def handle_accept_signal(call, signal_id):
    """Handle signal acceptance"""
    success, result = execute_trade(signal_id)
    
    if success:
        signal_data = pending_signals[signal_id]
        
        accept_msg = f"""
✅ *TRADE EXECUTED*

*Asset:* `{signal_data['asset']}`
*Signal:* {signal_data['signal']} {'🟢' if signal_data['signal'] == 'CALL' else '🔴'}
*Expiry:* 2 MINUTES
*Trade ID:* `{result}`

Trade placed. Results in 2 minutes.
        """
        
        markup = types.InlineKeyboardMarkup()
        btn_menu = types.InlineKeyboardButton("🔙 Menu", callback_data="main_menu")
        markup.add(btn_menu)
        
        tg_bot.edit_message_text(
            accept_msg,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=markup
        )
        
    else:
        error_msg = f"❌ *Trade Failed*\n\nReason: {result}"
        
        markup = types.InlineKeyboardMarkup()
        btn_refresh = types.InlineKeyboardButton("🔄 Refresh", callback_data="refresh")
        markup.add(btn_refresh)
        
        tg_bot.edit_message_text(
            error_msg,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=markup
        )

def handle_deny_signal(call, signal_id):
    """Handle signal denial"""
    if signal_id in pending_signals:
        pending_signals[signal_id]['status'] = 'denied'
        asset = pending_signals[signal_id]['asset']
        
        deny_msg = f"""
❌ *SIGNAL DENIED*

*Asset:* `{asset}`
*Action:* Trade skipped
        """
        
        markup = types.InlineKeyboardMarkup()
        btn_back = types.InlineKeyboardButton("🔙 Menu", callback_data="main_menu")
        markup.add(btn_back)
        
        tg_bot.edit_message_text(
            deny_msg,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=markup
        )

def show_signal_details(message, signal_id):
    """Show signal details"""
    if signal_id not in pending_signals:
        tg_bot.answer_callback_query(message.chat.id, "Signal expired or not found", show_alert=True)
        return
    
    signal_data = pending_signals[signal_id]
    analysis = signal_data['analysis']
    trend_15m = analysis['trend_15m']
    cross_result = analysis['cross_result']
    ind = cross_result['indicators']
    
    details_msg = f"""
📊 *SIGNAL DETAILS*

*Asset:* `{signal_data['asset']}`
*Signal:* {signal_data['signal']} {'🟢' if signal_data['signal'] == 'CALL' else '🔴'}
*Expiry:* 2 MINUTES

📈 *15-Minute Trend:*
• Price: {trend_15m['indicators']['price']:.5f}
• EMA200: {trend_15m['indicators']['ema_200']:.5f}
• RSI: {trend_15m['indicators']['rsi']:.2f}

⏱️ *2-Minute MACD Cross:*
• MACD Line: {ind['macd']['MACD_12_26_9'].iloc[-1]:.6f}
• Signal Line: {ind['macd']['MACDs_12_26_9'].iloc[-1]:.6f}
• Cross Type: {cross_result['cross_type']}
• MACD Diff: {cross_result['macd_diff']:.6f}

📊 *RSI (2min):*
• Value: {cross_result['rsi']:.2f}
• Status: {'✅ Valid' if analysis['rsi_result']['valid'] else '❌ Invalid'}
"""
    
    markup = types.InlineKeyboardMarkup()
    btn_back = types.InlineKeyboardButton("🔙 Back to Signal", callback_data=f"back_to_signal_{signal_id}")
    markup.add(btn_back)
    
    tg_bot.edit_message_text(
        details_msg,
        message.chat.id,
        message.message_id,
        parse_mode="Markdown",
        reply_markup=markup
    )

def show_trend_details(message, signal_id):
    """Show 15-minute trend details"""
    if signal_id not in pending_signals:
        tg_bot.answer_callback_query(message.chat.id, "Signal expired or not found", show_alert=True)
        return
    
    signal_data = pending_signals[signal_id]
    trend_15m = signal_data['analysis']['trend_15m']
    ind = trend_15m['indicators']
    
    trend_msg = f"""
📈 *15-MINUTE TREND*

*Asset:* `{signal_data['asset']}`
*Trend:* {trend_15m['trend']}
*Direction:* {trend_15m['direction']}

*Indicators:*
• Price: {ind['price']:.5f}
• EMA200: {ind['ema_200']:.5f}
• RSI (14): {ind['rsi']:.2f}

*Analysis:*
{trend_15m['reason']}
"""
    
    markup = types.InlineKeyboardMarkup()
    btn_back = types.InlineKeyboardButton("🔙 Back", callback_data=f"back_to_signal_{signal_id}")
    markup.add(btn_back)
    
    tg_bot.edit_message_text(
        trend_msg,
        message.chat.id,
        message.message_id,
        parse_mode="Markdown",
        reply_markup=markup
    )

def get_main_keyboard():
    """Generate main control keyboard"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    btn_status = types.InlineKeyboardButton("📊 Status", callback_data="status")
    btn_profit = types.InlineKeyboardButton("💰 Profit", callback_data="profit")
    btn_start = types.InlineKeyboardButton("▶️ Start", callback_data="start_trading")
    btn_stop = types.InlineKeyboardButton("⏸️ Stop", callback_data="stop")
    btn_history = types.InlineKeyboardButton("📈 History", callback_data="history")
    btn_help = types.InlineKeyboardButton("❓ Help", callback_data="help")
    
    markup.add(btn_status, btn_profit)
    markup.add(btn_start, btn_stop)
    markup.add(btn_history, btn_help)
    
    return markup

@tg_bot.message_handler(commands=['start', 'menu'])
def send_welcome(message):
    """Send welcome message with main keyboard"""
    pending_count = len([s for s in pending_signals.values() if s['status'] == 'pending'])
    
    welcome_text = f"""
🤖 *Trading Bot Control Panel*

📊 *Status:* {"✅ Running" if bot_running.is_set() else "⏸️ Stopped"}
📨 *Pending Signals:* {pending_count}

*Strategy:*
1. 15min Trend (Price vs EMA200)
2. 2min MACD Cross
3. RSI Confirmation (30-45 BUY / 55-70 SELL)
4. Expiry: 2 MINUTES

Select an option:
    """
    
    markup = get_main_keyboard()
    tg_bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=markup)

def show_main_menu(message):
    """Show main menu"""
    pending_count = len([s for s in pending_signals.values() if s['status'] == 'pending'])
    
    welcome_text = f"""
🤖 *Trading Bot Control Panel*

📊 *Status:* {"✅ Running" if bot_running.is_set() else "⏸️ Stopped"}
📨 *Pending Signals:* {pending_count}

Select an option:
    """
    
    markup = get_main_keyboard()
    tg_bot.edit_message_text(
        welcome_text,
        message.chat.id,
        message.message_id,
        parse_mode="Markdown",
        reply_markup=markup
    )

def show_status(message):
    """Show bot status"""
    active_signals = len([s for s in pending_signals.values() if s['status'] == 'pending'])
    executed_today = len([s for s in pending_signals.values() if s['status'] == 'executed'])
    
    status_text = f"""
📊 *Bot Status*

*Status:* {"✅ Running" if bot_running.is_set() else "⏸️ Stopped"}
*Strategy:* 15m Trend + 2min MACD + RSI
*Expiry:* 2 MINUTES
*Pending Signals:* {active_signals}
*Trades Today:* {executed_today}
    """
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_refresh = types.InlineKeyboardButton("🔄 Refresh", callback_data="refresh")
    btn_back = types.InlineKeyboardButton("🔙 Back", callback_data="main_menu")
    markup.add(btn_refresh, btn_back)
    
    tg_bot.edit_message_text(
        status_text,
        message.chat.id,
        message.message_id,
        parse_mode="Markdown",
        reply_markup=markup
    )

def show_profit(message):
    """Show profit information"""
    try:
        profits = api.get_all_profit()
        profit_text = "💰 *Current Profits*\n\n"
        
        for asset, profit in profits.items():
            if profit >= 90:
                emoji = "🟢" if profit > 95 else "🟡" if profit > 90 else "🔴"
                profit_text += f"{emoji} `{asset}`: {profit:.2f}%\n"
        
    except Exception as e:
        profit_text = f"❌ Error fetching profits: {str(e)}"
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_refresh = types.InlineKeyboardButton("🔄 Refresh", callback_data="refresh")
    btn_back = types.InlineKeyboardButton("🔙 Back", callback_data="main_menu")
    markup.add(btn_refresh, btn_back)
    
    tg_bot.edit_message_text(
        profit_text,
        message.chat.id,
        message.message_id,
        parse_mode="Markdown",
        reply_markup=markup
    )

def show_trade_history(message):
    """Show trade history"""
    executed_trades = [s for s in pending_signals.values() if s['status'] in ['executed', 'completed']]
    
    if not executed_trades:
        history_text = "📈 *Trade History*\n\nNo trades executed yet."
    else:
        history_text = "📈 *Recent Trades*\n\n"
        for i, trade in enumerate(executed_trades[-5:], 1):
            result = trade.get('result', {})
            status_emoji = "✅" if result.get('status') == "WON 💰" else "❌"
            history_text += f"{i}. {status_emoji} `{trade['asset']}` - {trade['signal']}\n"
    
    markup = types.InlineKeyboardMarkup()
    btn_back = types.InlineKeyboardButton("🔙 Menu", callback_data="main_menu")
    markup.add(btn_back)
    
    tg_bot.edit_message_text(
        history_text,
        message.chat.id,
        message.message_id,
        parse_mode="Markdown",
        reply_markup=markup
    )

def show_confirmation(message, action):
    """Show confirmation dialog"""
    action_text = "stop trading" if action == "stop" else "start trading"
    confirm_text = f"⚠️ *Confirm Action*\n\nAre you sure you want to {action_text}?"
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_yes = types.InlineKeyboardButton("✅ Yes", callback_data=f"confirm_{action}")
    btn_no = types.InlineKeyboardButton("❌ No", callback_data="cancel")
    markup.add(btn_yes, btn_no)
    
    tg_bot.edit_message_text(
        confirm_text,
        message.chat.id,
        message.message_id,
        parse_mode="Markdown",
        reply_markup=markup
    )

def execute_stop(message):
    """Execute stop trading"""
    bot_running.clear()
    
    stop_text = "⏸️ *Trading Stopped*\n\nBot has been paused."
    
    markup = types.InlineKeyboardMarkup()
    btn_start = types.InlineKeyboardButton("▶️ Start", callback_data="start_trading")
    btn_back = types.InlineKeyboardButton("🔙 Menu", callback_data="main_menu")
    markup.add(btn_start, btn_back)
    
    tg_bot.edit_message_text(
        stop_text,
        message.chat.id,
        message.message_id,
        parse_mode="Markdown",
        reply_markup=markup
    )

def execute_start(message):
    """Execute start trading"""
    bot_running.set()
    
    start_text = "✅ *Trading Started*\n\nBot is now active and monitoring."
    
    markup = types.InlineKeyboardMarkup()
    btn_stop = types.InlineKeyboardButton("⏸️ Stop", callback_data="stop")
    btn_back = types.InlineKeyboardButton("🔙 Menu", callback_data="main_menu")
    markup.add(btn_stop, btn_back)
    
    tg_bot.edit_message_text(
        start_text,
        message.chat.id,
        message.message_id,
        parse_mode="Markdown",
        reply_markup=markup
    )

def refresh_current_view(message):
    """Refresh current view"""
    if "Status" in message.text:
        show_status(message)
    elif "Profit" in message.text:
        show_profit(message)
    elif "History" in message.text:
        show_trade_history(message)
    else:
        show_main_menu(message)

def run_trading_engine():
    """Main trading engine"""
    if api.connect():
        startup_msg = "🚀 *Trading Bot Started!*\n\nStrategy: 15m Trend + 2min MACD + RSI\nExpiry: 2 MINUTES\n\nUse /menu for controls."
        tg_bot.send_message(chat_id, startup_msg, parse_mode="Markdown")
        
        while True:
            if bot_running.is_set():
                current_time = time.localtime()
                if current_time.tm_min % 2 == 0 and current_time.tm_sec < 5:
                    try:
                        profits = api.get_all_profit()
                        assets = [a for a, p in profits.items() if p >= 90 and "_otc" in a.lower()]
                        
                        for asset in assets:
                            analysis = analyze_market(asset)
                            if analysis and analysis.get('valid'):
                                signal_id = send_trade_signal(asset, analysis)
                                print(f"Signal sent: {signal_id[:8]} for {asset} - {analysis['signal']}")
                        
                        time.sleep(60)
                        
                    except Exception as e:
                        print(f"Error: {e}")
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
    threading.Thread(target=run_trading_engine, daemon=True).start()
    threading.Thread(target=tg_bot.infinity_polling, daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
