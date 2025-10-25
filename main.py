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

# .env file se variables load karein
load_dotenv()

# --- CONFIGURATION (LOAD FROM .ENV) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
# ADMIN_USER_IDS ko string se list of integers mein convert karein
ADMIN_USER_IDS = [int(i.strip()) for i in os.getenv("ADMIN_USER_IDS", "").split(',') if i.strip()]

SPAWN_THRESHOLD = 100 
# ---------------------------------------

# Global variable for current spawn in each chat
current_spawns = {}
# Set to track all active users (for broadcast)
active_users = set()

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- DATABASE FUNCTIONS (POSTGRESQL / NEON DB) ---

def execute_query(query, params=None, fetch=False):
    """Database se connect aur query execute karta hai."""
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require') # Neon DB requires sslmode='require'
        cur = conn.cursor()
        cur.execute(query, params)
        if fetch:
            result = cur.fetchall()
            cur.close()
            conn.commit()
            return result
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Database Error: {e}")
        # Agar connection error ho to database ko initialise karein
        if "does not exist" in str(e) or "relation" in str(e):
             # Initialise database agar table nahi mili
             initialize_database()
             # Retry the query after initialization
             if conn: conn.close()
             return execute_query(query, params, fetch)
        
    finally:
        if conn:
            conn.close()

def initialize_database():
    """Zaroori tables banata hai agar woh exist nahi karte."""
    logger.info("Initializing database schema...")
    # USER table mein saare user data store honge
    # COLLECTION table mein kaunsa user kaunsa character rakhta hai uska record hoga
    # PROFILES table mein user ke stats store honge
    
    queries = [
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS characters (
            char_id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS user_collection (
            user_id BIGINT REFERENCES users(user_id),
            char_id INT REFERENCES characters(char_id),
            grab_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, char_id)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
            trades_done INT DEFAULT 0,
            gifts_sent INT DEFAULT 0,
            gifts_received INT DEFAULT 0
        );
        """
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


# --- API HELPER FUNCTION ---
async def get_random_waifu():
    """Multiple free APIs se random waifu fetch karta hai."""
    # Waifu.im se character name milne ke chances zyada hain
    api_choice = 'waifu.im' 
    
    try:
        if api_choice == 'waifu.im':
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
                 character_name = f"Waifu #{int(time.time() * 1000)}" # Fallback name
                 
            return character_name.strip(), image_url

    except Exception as e:
        logger.error(f"API Error ({api_choice}): {e}")
        return None, None 

# --- CORE SPAWN FUNCTION ---
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


# --- CORE COMMANDS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start command - Welcome and user registration."""
    user = update.effective_user
    
    # User ko DB mein register/update karein
    execute_query(
        "INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, first_name = EXCLUDED.first_name;",
        (user.id, user.username, user.first_name)
    )
    
    # Profile table mein entry banayein agar nahi hai
    execute_query(
        "INSERT INTO user_profiles (user_id) VALUES (%s) ON CONFLICT DO NOTHING;",
        (user.id,)
    )
    
    await update.message.reply_html(
        rf"Salaam, {user.mention_html()}! Main taiyaar hoon. 😼"
        f"\n\n<b>Commands:</b>"
        f"\n/harem - Collection dekhein."
        f"\n/leaderboard - Top collectors dekhein (inline filter ke saath)."
        f"\n/trade, /gift, /profile, /ping"
    )

async def harem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/harem command - User ka collection dikhata hai."""
    user_id = update.effective_user.id
    
    query = """
    SELECT c.name FROM user_collection uc
    JOIN characters c ON uc.char_id = c.char_id
    WHERE uc.user_id = %s;
    """
    
    results = execute_query(query, (user_id,), fetch=True)
    
    if results:
        harem_list = "\n".join(f"{i}. {char[0]}" for i, char in enumerate(results, 1))
        await update.message.reply_text(
            f"<b>{update.effective_user.first_name} ka Harem ({len(results)}):</b>\n\n{harem_list}",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text("Aapka harem abhi khaali hai. Characters grab karein!")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/leaderboard command - Initial leaderboard message."""
    await update.message.reply_text("🏆 Leaderboard Load Ho Raha Hai...", reply_markup=get_leaderboard_markup('global'))

def get_leaderboard_markup(time_period='global'):
    """Leaderboard inline keyboard markup generate karta hai."""
    
    # 3 Buttons: Global (Sabhi waqt ka), Monthly (Aap yeh logic bana sakte hain), Today (Aaj ka)
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
    # Time filter ke liye complex SQL ki zaroorat padegi
    time_filter = ""
    if time_period == 'today':
        time_filter = "WHERE uc.grab_time >= CURRENT_DATE"
    elif time_period == 'monthly':
        time_filter = "WHERE uc.grab_time >= date_trunc('month', CURRENT_DATE)"
    
    query = f"""
    SELECT u.first_name, COUNT(uc.char_id) AS collection_count
    FROM users u
    JOIN user_collection uc ON u.user_id = uc.user_id
    {time_filter}
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


# --- CALLBACKS (BUTTON LOGIC) ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buttons (Grab, Leaderboard Filters) ko handle karta hai."""
    query = update.callback_query
    await query.answer() 
    
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    
    # --- GRAB LOGIC ---
    if query.data == "grab_waifu":
        global current_spawns
        
        if chat_id not in current_spawns or current_spawns[chat_id].get('claimed', True):
            await query.edit_message_caption(caption="Bahut der kardi! 🥺")
            return
            
        spawned_waifu = current_spawns[chat_id]
        
        # Check karein ki user ne pehle hi collect toh nahi kiya
        check_query = """
        SELECT c.name FROM user_collection uc
        JOIN characters c ON uc.char_id = c.char_id
        WHERE uc.user_id = %s AND c.name = %s;
        """
        if execute_query(check_query, (user_id, spawned_waifu['name']), fetch=True):
             await query.message.reply_text(f"<b>{query.from_user.first_name}</b> ne <b>{spawned_waifu['name']}</b> ko grab karne ki koshish ki, lekin unke paas yeh pehle se hai!", parse_mode=ParseMode.HTML)
             await query.edit_message_caption(caption=f"✨ **{spawned_waifu['name']}** ✨\n(Pehle hi {query.from_user.first_name} ke paas hai)")
             return

        # 1. Character ko DB mein daalein (agar pehle se nahi hai)
        execute_query("INSERT INTO characters (name) VALUES (%s) ON CONFLICT DO NOTHING;", (spawned_waifu['name'],))
        
        # 2. Character ID nikaalein
        char_id_result = execute_query("SELECT char_id FROM characters WHERE name = %s;", (spawned_waifu['name'],), fetch=True)
        char_id = char_id_result[0][0]
        
        # 3. User collection mein entry karein
        execute_query(
            "INSERT INTO user_collection (user_id, char_id) VALUES (%s, %s);",
            (user_id, char_id)
        )
        
        current_spawns[chat_id]['claimed'] = True
        
        # Original message ko edit karein
        await query.edit_message_caption(
            caption=f"✨ **{spawned_waifu['name']}** ✨\n\n💖 **Grabbed by: {query.from_user.first_name}** 💖",
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Chat mein confirmation message bhejein
        await query.message.reply_text(
            f"🎉 Badhaai ho, <b>{query.from_user.first_name}</b>! Aapne <b>{spawned_waifu['name']}</b> ko apne harem mein shaamil kar liya hai!",
            parse_mode=ParseMode.HTML
        )

    # --- LEADERBOARD LOGIC ---
    elif query.data.startswith("lb_"):
        time_period = query.data.split('_')[1]
        
        # Leaderboard data fetch karein
        leaderboard_text = fetch_leaderboard_data(time_period)
        
        # Message ko edit karein
        await query.edit_message_text(
            leaderboard_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_leaderboard_markup(time_period)
        )

# --- AUTO-SPAWN HANDLER ---
async def message_counter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Har message ko count karta hai aur threshold par spawn trigger karta hai."""
    chat_id = update.effective_chat.id
    
    if update.message.chat.type == "private":
        return

    # User ko DB mein register/update karein
    if update.effective_user:
        user = update.effective_user
        execute_query(
            "INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, first_name = EXCLUDED.first_name;",
            (user.id, user.username, user.first_name)
        )

    if 'message_count' not in context.chat_data:
        context.chat_data['message_count'] = 0
        
    context.chat_data['message_count'] += 1
    
    if context.chat_data['message_count'] >= SPAWN_THRESHOLD:
        await spawn_waifu(context, chat_id)
        context.chat_data['message_count'] = 0 

# --- MAIN FUNCTION ---
def main():
    """Bot ko start karta hai."""
    # Database initialization ko sabse pehle call karein
    initialize_database() 

    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN is not set. Please check your .env or Render Environment Variables.")
        return
        
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("harem", harem_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    # Note: Trade, Gift, Broadcast, Ping commands ko aap khud upar wale code se integrate kar sakte hain.

    # Message Handler (Auto-spawn ke liye)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_counter))
    
    # Callback Query Handler (Buttons ke liye)
    application.add_handler(CallbackQueryHandler(button_callback))

    print("Bot chalu ho gaya hai. Render par logs dekhein.")
    application.run_polling()


if __name__ == "__main__":
    main()
