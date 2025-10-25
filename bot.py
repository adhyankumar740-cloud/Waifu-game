import logging
import random
import time
import requests
import os
from dotenv import load_dotenv
import psycopg2
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

# --- CONFIGURATION (LOAD FROM .ENV) ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_USER_IDS = [int(i.strip()) for i in os.getenv("ADMIN_USER_IDS", "").split(',') if i.strip()]

# Webhook configuration (Render ke liye zaroori)
PORT = int(os.environ.get('PORT', '8443')) 
WEBHOOK_URL = os.getenv("WEBHOOK_URL") 

SPAWN_THRESHOLD = 100 
current_spawns = {} 

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- DATABASE FUNCTIONS ---

def execute_query(query, params=None, fetch=False):
    """Database se connect aur query execute karta hai."""
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        cur = conn.cursor()
        cur.execute(query, params)
        if fetch:
            result = cur.fetchall()
            conn.commit()
            cur.close()
            return result
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Database Error: {e}")
        # Initialization logic for robustness
        if "does not exist" in str(e) or "relation" in str(e):
             initialize_database()
             if conn: conn.close()
             # Retry the query after initialization
             return execute_query(query, params, fetch)
        
    finally:
        if conn:
            conn.close()

def initialize_database():
    """Zaroori tables banata hai agar woh exist nahi karte."""
    logger.info("Initializing database schema...")
    queries = [
        "CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT);",
        "CREATE TABLE IF NOT EXISTS characters (char_id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL);",
        "CREATE TABLE IF NOT EXISTS user_collection (user_id BIGINT REFERENCES users(user_id), char_id INT REFERENCES characters(char_id), grab_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (user_id, char_id));",
        "CREATE TABLE IF NOT EXISTS user_profiles (user_id BIGINT PRIMARY KEY REFERENCES users(user_id), trades_done INT DEFAULT 0, gifts_sent INT DEFAULT 0, gifts_received INT DEFAULT 0, hmode_text TEXT DEFAULT 'Harem Collection', imode_text TEXT DEFAULT 'Inline Waifus');",
        "CREATE TABLE IF NOT EXISTS pending_trades (trade_id TEXT PRIMARY KEY, from_user_id BIGINT REFERENCES users(user_id), to_user_id BIGINT REFERENCES users(user_id), from_char_name TEXT NOT NULL, to_char_name TEXT NOT NULL, status TEXT DEFAULT 'PENDING', created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP);"
    ]
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        cur = conn.cursor()
        for query in queries:
            cur.execute(query)
        conn.commit()
        cur.close()
        logger.info("Database schema initialized successfully.")
    except Exception as e:
        logger.error(f"Database Initialization Failed: {e}")
    finally:
        if conn:
            conn.close()

# --- HELPER FUNCTIONS ---

def register_user(user):
    """User ko DB mein register/update karta hai."""
    execute_query(
        "INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, first_name = EXCLUDED.first_name;",
        (user.id, user.username, user.first_name)
    )
    execute_query(
        "INSERT INTO user_profiles (user_id) VALUES (%s) ON CONFLICT DO NOTHING;",
        (user.id,)
    )

async def get_random_waifu():
    """Multiple free APIs se random waifu fetch karta hai."""
    try:
        response = requests.get("https://api.waifu.im/search?is_nsfw=false&tags=waifu")
        response.raise_for_status()
        data = response.json()
        image_url = data['images'][0]['url']
        character_name = "Unknown Waifu"
        tags = data['images'][0].get('tags', [])
        for tag in tags:
            if tag.get('is_character', False):
                character_name = tag['name']
                break
        if character_name == "Unknown Waifu":
             character_name = f"Waifu #{int(time.time() * 1000)}" 
        return character_name.strip(), image_url
    except Exception as e:
        logger.error(f"API Error: {e}")
        return None, None 

async def spawn_waifu(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Chat mein ek nayi waifu spawn karta hai."""
    global current_spawns
    
    if chat_id in current_spawns and not current_spawns[chat_id].get('claimed', True):
        return

    name, image = await get_random_waifu()
    
    if name and image:
        current_spawns[chat_id] = {'name': name, 'image': image, 'claimed': False}
        
        keyboard = [[InlineKeyboardButton("💖 GRAB 💖", callback_data="grab_waifu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_photo(
            chat_id=chat_id,
            photo=image,
            caption=f"✨ Ek wild **{name}** prakat hui hai! ✨\n\nUse apna banane ke liye 'GRAB' button dabayein!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )

# --- COMMAND HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start: Welcome message aur user registration."""
    user = update.effective_user
    register_user(user)
    
    await update.message.reply_html(
        rf"Salaam, {user.mention_html()}! Main **Grab Your Waifu Bot** hoon. 😼"
        f"\n\n**Available Commands:**"
        f"\n/start - Bot shuru karein."
        f"\n/help - FAQ/Madad dekhein."
        f"\n/grab - Spawned waifu ko claim karein."
        f"\n/harem - Apni collection dekhein."
        f"\n/search [waifu_name] - Waifu ko khojein."
        f"\n/changetime [seconds] - Spawn time badlein (Admin)."
        f"\n/top (or /gtop) - Global Leaderboard dekhein."
        f"\n/trade @user [my_char] for [their_char] - Trade request karein."
        f"\n/gift @user [char_name] - Waifu gift karein."
        f"\n/status - Apne Harem Stats dekhein."
        f"\n/hmode [text] - Harem appearance badlein."
        f"\n/imode [text] - Inline result appearance badlein (WIP)."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help: FAQ/Madad."""
    await update.message.reply_text(
        "**FAQ/Madad:**\n\n"
        "1. **Waifu kaise collect karein?**\n"
        "   Har 100 messages ke baad ek waifu spawn hogi. Use **💖 GRAB 💖** button ya **/grab** command se claim karein.\n"
        "2. **Trade kaise karein?**\n"
        "   **/trade @username [Aapki Waifu] for [Dusre ki Waifu]** format use karein.\n"
        "3. **Apne stats kaise dekhein?**\n"
        "   **/status** use karein."
    )

async def harem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/harem: User ka collection dikhata hai."""
    user_id = update.effective_user.id
    
    hmode_result = execute_query("SELECT hmode_text FROM user_profiles WHERE user_id = %s;", (user_id,), fetch=True)
    hmode_text = hmode_result[0][0] if hmode_result and hmode_result[0][0] else "Harem Collection"

    query = """
    SELECT c.name FROM user_collection uc
    JOIN characters c ON uc.char_id = c.char_id
    WHERE uc.user_id = %s
    ORDER BY c.name;
    """
    results = execute_query(query, (user_id,), fetch=True)
    
    if results:
        harem_list = "\n".join(f"{i}. {char[0]}" for i, char in enumerate(results, 1))
        await update.message.reply_text(
            f"**{update.effective_user.first_name} ka {hmode_text} ({len(results)}):**\n\n{harem_list}",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text("Aapka harem abhi khaali hai. Characters grab karein!")

async def grab_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/grab: Spawned waifu ko claim karta hai (Fallback to button)."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if chat_id not in current_spawns or current_spawns[chat_id].get('claimed', True):
        await update.message.reply_text("Abhi koi waifu spawn nahi hui hai, ya woh pehle hi claim ho chuki hai.")
        return
        
    spawned_waifu = current_spawns[chat_id]
    
    check_query = """SELECT c.name FROM user_collection uc JOIN characters c ON uc.char_id = c.char_id WHERE uc.user_id = %s AND c.name = %s;"""
    if execute_query(check_query, (user_id, spawned_waifu['name']), fetch=True):
         await update.message.reply_text(f"**{update.effective_user.first_name}** ne **{spawned_waifu['name']}** ko grab karne ki koshish ki, lekin unke paas yeh pehle se hai!", parse_mode=ParseMode.MARKDOWN)
         current_spawns[chat_id]['claimed'] = True 
         return

    execute_query("INSERT INTO characters (name) VALUES (%s) ON CONFLICT DO NOTHING;", (spawned_waifu['name'],))
    char_id_result = execute_query("SELECT char_id FROM characters WHERE name = %s;", (spawned_waifu['name'],), fetch=True)
    char_id = char_id_result[0][0]
    execute_query("INSERT INTO user_collection (user_id, char_id) VALUES (%s, %s);", (user_id, char_id))
    current_spawns[chat_id]['claimed'] = True

    await update.message.reply_text(
        f"🎉 Badhaai ho, **{update.effective_user.first_name}**! Aapne **{spawned_waifu['name']}** ko apne harem mein shaamil kar liya hai!",
        parse_mode=ParseMode.MARKDOWN
    )

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/search: Waifu ko collection mein khojein."""
    if not context.args:
        await update.message.reply_text("Kripya search karne ke liye waifu ka naam dein. Format: /search [waifu_name]")
        return
        
    search_name = " ".join(context.args)
    query = "SELECT name FROM characters WHERE name ILIKE %s LIMIT 10;" 
    results = execute_query(query, (f"%{search_name}%",), fetch=True)
    
    if results:
        found_list = "\n".join(f"- {row[0]}" for row in results)
        await update.message.reply_text(
            f"**Waifu Search Results (Database):**\n\n{found_list}",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(f"'{search_name}' naam ki koi waifu hamare database mein nahi mili.")

async def changetime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/changetime: Spawn time badlein (Admin Only)."""
    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("Yeh command sirf admin ke liye hai.")
        return
    
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Format: /changetime [number_of_messages]")
        return
        
    global SPAWN_THRESHOLD
    new_time = int(context.args[0])
    SPAWN_THRESHOLD = new_time
    
    await update.message.reply_text(f"Naya spawn time set ho gaya hai: Har **{new_time}** messages ke baad waifu spawn hogi.", parse_mode=ParseMode.MARKDOWN)

async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/top: Current chat ke top collectors dekhein (Redirects to /gtop)."""
    await update.message.reply_text("Local chat leaderboard abhi vikas mein hai. Global leaderboard ke liye **/gtop** istemaal karein.")
    await leaderboard_command(update, context) 

async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/trade: Trade request karta hai aur DB mein save karta hai."""
    try:
        args = context.args
        if len(args) < 3 or 'for' not in args:
            await update.message.reply_text("Format galat hai. Sahi format: /trade @username [My Character] for [Their Character]")
            return

        target_username_mention = args[0]
        if not target_username_mention.startswith('@'):
            await update.message.reply_text("Pehla argument @username hona chahiye.")
            return
        
        target_username = target_username_mention[1:]
        from_user_id = update.effective_user.id
        from_user_name = update.effective_user.first_name

        full_args = " ".join(args[1:])
        if ' for ' not in full_args:
             await update.message.reply_text("Format: /trade @username [My Character] for [Their Character]")
             return
        
        my_char_name, their_char_name = full_args.split(' for ', 1)
        my_char_name = my_char_name.strip()
        their_char_name = their_char_name.strip()
        
        target_result = execute_query("SELECT user_id, first_name FROM users WHERE username = %s;", (target_username,), fetch=True)
        
        if not target_result:
            await update.message.reply_text(f"User @{target_username} nahi mila. Unhe bot ko /start karne ko kahein.")
            return

        target_user_id, target_user_name = target_result[0]
        
        if target_user_id == from_user_id:
            await update.message.reply_text("Aap khud se trade nahi kar sakte!")
            return

        giver_has_char = execute_query(
            "SELECT 1 FROM user_collection uc JOIN characters c ON uc.char_id = c.char_id WHERE uc.user_id = %s AND c.name = %s;",
            (from_user_id, my_char_name), fetch=True
        )
        if not giver_has_char:
            await update.message.reply_text(f"Aapke paas '{my_char_name}' naam ka character nahi hai.")
            return
        
        receiver_has_char = execute_query(
            "SELECT 1 FROM user_collection uc JOIN characters c ON uc.char_id = c.char_id WHERE uc.user_id = %s AND c.name = %s;",
            (target_user_id, their_char_name), fetch=True
        )
        if not receiver_has_char:
            await update.message.reply_text(f"@{target_username} ke paas '{their_char_name}' nahi hai.")
            return
        
        trade_id = f"trade_{int(time.time())}_{from_user_id}" 
        execute_query(
            "INSERT INTO pending_trades (trade_id, from_user_id, to_user_id, from_char_name, to_char_name) VALUES (%s, %s, %s, %s, %s);",
            (trade_id, from_user_id, target_user_id, my_char_name, their_char_name)
        )
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Accept", callback_data=f"trade_accept_{trade_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"trade_reject_{trade_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"<b>Trade Request!</b>\n\n"
                     f"{from_user_name} (@{update.effective_user.username}) "
                     f"aapko apna '<b>{my_char_name}</b>' dekar aapse aapka '<b>{their_char_name}</b>' lena chahte hain."
                     f"\n\nKya aapko yeh trade manzoor hai?",
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
            await update.message.reply_text(f"Trade request @{target_username} ko bhej di gayi hai.")
        except Exception as e:
            logger.warning(f"Trade DM failed: {e}")
            execute_query("DELETE FROM pending_trades WHERE trade_id = %s;", (trade_id,)) 
            await update.message.reply_text(f"Trade request nahi bhej paya. @{target_username} ko bot ko DM karne ko kahein.")

    except Exception as e:
        logger.error(f"Trade command error: {e}")
        await update.message.reply_text("Trade request mein kuch gadbad hui.")

async def gift_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/gift @username [char_name] - Waifu gift karta hai."""
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("Format galat hai. Sahi format: /gift @username [Character Name]")
            return

        target_username_mention = args[0]
        if not target_username_mention.startswith('@'):
            await update.message.reply_text("Pehla argument @username hona chahiye.")
            return
        
        target_username = target_username_mention[1:]
        gifter_user_id = update.effective_user.id
        gifter_user_name = update.effective_user.first_name
        character_name = " ".join(args[1:])

        target_result = execute_query("SELECT user_id, first_name FROM users WHERE username = %s;", (target_username,), fetch=True)
        
        if not target_result:
            await update.message.reply_text(f"User @{target_username} nahi mila. Unhe bot ko /start karne ko kahein.")
            return

        target_user_id, target_user_name = target_result[0]
        
        if target_user_id == gifter_user_id:
            await update.message.reply_text("Aap khud ko gift nahi de sakte!")
            return

        # 1. Check karein ki gifter ke paas character hai ya nahi
        char_id_result = execute_query(
            "SELECT c.char_id FROM user_collection uc JOIN characters c ON uc.char_id = c.char_id WHERE uc.user_id = %s AND c.name = %s;",
            (gifter_user_id, character_name), fetch=True
        )
        if not char_id_result:
            await update.message.reply_text(f"Aapke paas '{character_name}' naam ka character nahi hai.")
            return
        
        char_id = char_id_result[0][0]

        # 2. Gifter se remove karein (aur collection se uski entry delete karein)
        execute_query("DELETE FROM user_collection WHERE user_id = %s AND char_id = %s;", (gifter_user_id, char_id))
        
        # 3. Receiver ko add karein (collection mein entry banayein)
        execute_query("INSERT INTO user_collection (user_id, char_id) VALUES (%s, %s);", (target_user_id, char_id))
        
        # 4. Stats update karein
        execute_query("UPDATE user_profiles SET gifts_sent = gifts_sent + 1 WHERE user_id = %s;", (gifter_user_id,))
        execute_query("UPDATE user_profiles SET gifts_received = gifts_received + 1 WHERE user_id = %s;", (target_user_id,))
        
        await update.message.reply_text(
            f"Success! Aapne '{character_name}' ko @{target_username} ko gift kar diya hai."
        )
        
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"Tohfa! {gifter_user_name} ne aapko **{character_name}** gift kiya hai! 🎉"
            )
        except Exception as e:
            logger.warning(f"Gift DM failed: {e}")

    except Exception as e:
        logger.error(f"Gift command error: {e}")
        await update.message.reply_text("Gift bhejte waqt kuch gadbad hui.")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/gtop (or /top) command - Global Leaderboard (dynamic inline filter ke saath)"""
    await update.message.reply_text("🏆 Global Leaderboard Load Ho Raha Hai...", reply_markup=get_leaderboard_markup('global'))

def get_leaderboard_markup(time_period='global'):
    """Leaderboard inline keyboard markup generate karta hai."""
    keyboard = [
        [
            InlineKeyboardButton("🌐 Global", callback_data="lb_global"),
            InlineKeyboardButton("📅 Monthly (WIP)", callback_data="lb_monthly"),
            InlineKeyboardButton("☀️ Today", callback_data="lb_today"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def fetch_leaderboard_data(time_period='global'):
    """Database se leaderboard data fetch karta hai."""
    time_filter = ""
    if time_period == 'today':
        time_filter = "AND uc.grab_time >= CURRENT_DATE"
    elif time_period == 'monthly':
        time_filter = "AND uc.grab_time >= date_trunc('month', CURRENT_DATE)"
    
    query = f"""
    SELECT u.first_name, COUNT(uc.char_id) AS collection_count
    FROM users u
    JOIN user_collection uc ON u.user_id = uc.user_id
    WHERE 1=1 {time_filter}
    GROUP BY u.user_id, u.first_name
    ORDER BY collection_count DESC
    LIMIT 10;
    """
    results = execute_query(query, fetch=True)
    if not results:
        return "Abhi tak koi data nahi mila."
        
    leaderboard_text = f"**🏆 Leaderboard: {time_period.capitalize()} 🏆**\n\n"
    leaderboard_text += "\n".join(f"**{i}.** {row[0]} - {row[1]} waifus" for i, row in enumerate(results, 1))
    
    return leaderboard_text

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status: User stats dekhein."""
    user_id = update.effective_user.id
    
    query = """
    SELECT up.trades_done, up.gifts_sent, up.gifts_received, up.hmode_text, COUNT(uc.char_id) 
    FROM user_profiles up
    LEFT JOIN user_collection uc ON up.user_id = uc.user_id
    WHERE up.user_id = %s
    GROUP BY up.user_id, up.trades_done, up.gifts_sent, up.gifts_received, up.hmode_text;
    """
    
    result = execute_query(query, (user_id,), fetch=True)
    
    if result:
        trades, sent, received, hmode, collection_count = result[0]
        await update.message.reply_html(
            f"<b>{update.effective_user.first_name} ka Harem Status:</b>\n"
            f"-----------------------------------\n"
            f"<b>Characters Collected:</b> {collection_count}\n"
            f"<b>Harem Mode:</b> {hmode}\n"
            f"<b>Trades Kiye:</b> {trades}\n"
            f"<b>Gifts Bheje/Mile:</b> {sent}/{received}\n"
        )
    else:
        await update.message.reply_text("Aapka status abhi tak register nahi hua hai. /start karein.")

async def hmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/hmode: Harem appearance badlein."""
    if not context.args:
        await update.message.reply_text("Format: /hmode [Naya Harem Naam]\nExample: /hmode My Waifu Empire")
        return
        
    new_text = " ".join(context.args)[:50] # Limit to 50 chars
    user_id = update.effective_user.id
    
    execute_query("UPDATE user_profiles SET hmode_text = %s WHERE user_id = %s;", (new_text, user_id))
    await update.message.reply_text(f"Harem ka naam badal diya gaya hai: **{new_text}**", parse_mode=ParseMode.MARKDOWN)

async def imode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/imode: Inline result appearance badlein (Future feature)."""
    await update.message.reply_text("Yeh command abhi vikas mein hai (Inline Query Result Customization).")
    

# --- CORE LOGIC (CALLBACKS AND COUNTER) ---

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buttons (Grab, Leaderboard, Trade) ko handle karta hai."""
    query = update.callback_query
    await query.answer() 
    
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    
    # --- GRAB LOGIC ---
    if query.data == "grab_waifu":
        if chat_id not in current_spawns or current_spawns[chat_id].get('claimed', True):
            await query.edit_message_caption(caption="Bahut der kardi! 🥺")
            return
            
        spawned_waifu = current_spawns[chat_id]
        check_query = """SELECT c.name FROM user_collection uc JOIN characters c ON uc.char_id = c.char_id WHERE uc.user_id = %s AND c.name = %s;"""
        if execute_query(check_query, (user_id, spawned_waifu['name']), fetch=True):
             await query.message.reply_text(f"**{query.from_user.first_name}** ne **{spawned_waifu['name']}** ko grab karne ki koshish ki, lekin unke paas yeh pehle se hai!", parse_mode=ParseMode.MARKDOWN)
             current_spawns[chat_id]['claimed'] = True
             return

        execute_query("INSERT INTO characters (name) VALUES (%s) ON CONFLICT DO NOTHING;", (spawned_waifu['name'],))
        char_id_result = execute_query("SELECT char_id FROM characters WHERE name = %s;", (spawned_waifu['name'],), fetch=True)
        char_id = char_id_result[0][0]
        execute_query("INSERT INTO user_collection (user_id, char_id) VALUES (%s, %s);", (user_id, char_id))
        current_spawns[chat_id]['claimed'] = True
        
        await query.edit_message_caption(
            caption=f"✨ **{spawned_waifu['name']}** ✨\n\n💖 **Grabbed by: {query.from_user.first_name}** 💖",
            parse_mode=ParseMode.MARKDOWN
        )
        await query.message.reply_text(
            f"🎉 Badhaai ho, **{query.from_user.first_name}**! Aapne **{spawned_waifu['name']}** ko apne harem mein shaamil kar liya hai!",
            parse_mode=ParseMode.MARKDOWN
        )

    # --- LEADERBOARD LOGIC ---
    elif query.data.startswith("lb_"):
        time_period = query.data.split('_')[1]
        leaderboard_text = fetch_leaderboard_data(time_period)
        
        try:
            await query.edit_message_text(
                leaderboard_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_leaderboard_markup(time_period)
            )
        except BadRequest:
             pass
             
    # --- TRADE LOGIC (ACCEPT/REJECT) ---
    elif query.data.startswith(("trade_accept_", "trade_reject_")):
        action, trade_id = query.data.split('_', 2)[1:]
        
        trade_data = execute_query(
            "SELECT from_user_id, to_user_id, from_char_name, to_char_name, status FROM pending_trades WHERE trade_id = %s;",
            (trade_id,), fetch=True
        )
        
        if not trade_data:
            await query.edit_message_text("Yeh trade request expire ho chuki hai ya pehle hi process ho chuki hai.")
            return

        from_id, to_id, from_char, to_char, status = trade_data[0]
        
        if user_id != to_id:
            await query.answer("Yeh trade request aapke liye nahi hai!", show_alert=True)
            return
            
        if status != 'PENDING':
             await query.edit_message_text(f"Yeh trade pehle hi {status.lower()} ho chuka hai.")
             return
             
        giver_name_result = execute_query("SELECT first_name FROM users WHERE user_id = %s;", (from_id,), fetch=True)
        giver_name = giver_name_result[0][0] if giver_name_result else "Original User"

        if action == "accept":
            # Trade process: Characters swap karein
            from_char_id_result = execute_query("SELECT char_id FROM characters WHERE name = %s;", (from_char,), fetch=True)
            to_char_id_result = execute_query("SELECT char_id FROM characters WHERE name = %s;", (to_char,), fetch=True)
            
            if not from_char_id_result or not to_char_id_result:
                 await query.edit_message_text("Trade fail: Character ID nahi mila.")
                 return
                 
            from_char_id = from_char_id_result[0][0]
            to_char_id = to_char_id_result[0][0]

            # 1. Receiver (to_id) se to_char hatao, Giver (from_id) ko do
            execute_query("DELETE FROM user_collection WHERE user_id = %s AND char_id = %s;", (to_id, to_char_id))
            execute_query("INSERT INTO user_collection (user_id, char_id) VALUES (%s, %s);", (from_id, to_char_id))
            
            # 2. Giver (from_id) se from_char hatao, Receiver (to_id) ko do
            execute_query("DELETE FROM user_collection WHERE user_id = %s AND char_id = %s;", (from_id, from_char_id))
            execute_query("INSERT INTO user_collection (user_id, char_id) VALUES (%s, %s);", (to_id, from_char_id))
            
            # Stats update karein
            execute_query("UPDATE user_profiles SET trades_done = trades_done + 1 WHERE user_id IN (%s, %s);", (from_id, to_id))
            
            # Trade status update karein
            execute_query("UPDATE pending_trades SET status = 'ACCEPTED' WHERE trade_id = %s;", (trade_id,))
            
            await query.edit_message_text(f"✅ Trade Accepted! Aapne '{to_char}' dekar '{from_char}' le liya hai.")
            
            try:
                await context.bot.send_message(
                    chat_id=from_id,
                    text=f"✅ **Trade Accepted!** {query.from_user.first_name} ne aapka trade request accept kar liya hai. Aapko **{to_char}** mil gaya hai!"
                )
            except Exception:
                pass
                
        elif action == "reject":
            execute_query("UPDATE pending_trades SET status = 'REJECTED' WHERE trade_id = %s;", (trade_id,))
            
            await query.edit_message_text("❌ Trade Rejected.")
            
            try:
                await context.bot.send_message(
                    chat_id=from_id,
                    text=f"❌ **Trade Rejected!** {query.from_user.first_name} ne aapka trade request reject kar diya hai."
                )
            except Exception:
                pass


async def message_counter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Har message ko count karta hai aur threshold par spawn trigger karta hai."""
    chat_id = update.effective_chat.id
    
    if update.message.chat.type == "private":
        return

    if update.effective_user:
        register_user(update.effective_user)

    if 'message_count' not in context.chat_data:
        context.chat_data['message_count'] = 0
        
    context.chat_data['message_count'] += 1
    
    if context.chat_data['message_count'] >= SPAWN_THRESHOLD:
        await spawn_waifu(context, chat_id)
        context.chat_data['message_count'] = 0 

# --- WEBHOOK MAIN FUNCTION ---

def main():
    """Bot ko Webhook mode mein start karta hai."""
    initialize_database()

    if not TELEGRAM_TOKEN or not DATABASE_URL or not WEBHOOK_URL:
        logger.error("Required environment variables are not set.")
        return
        
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("grab", grab_command))
    application.add_handler(CommandHandler("harem", harem_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("changetime", changetime_command))
    application.add_handler(CommandHandler("top", top_command)) 
    application.add_handler(CommandHandler("trade", trade_command))
    application.add_handler(CommandHandler("gift", gift_command))
    application.add_handler(CommandHandler("gtop", leaderboard_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("hmode", hmode_command))
    application.add_handler(CommandHandler("imode", imode_command))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_counter))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Run in Webhook mode for Render Web Service
    print(f"Setting webhook to {WEBHOOK_URL} on port {PORT}...")
    
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TELEGRAM_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}"
    )

if __name__ == "__main__":
    main()
