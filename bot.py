import logging
import random
import time
import requests
import os
import uuid
from dotenv import load_dotenv
import psycopg2
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultPhoto, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
    InlineQueryHandler 
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

# --- CONFIGURATION (LOAD FROM .ENV) ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_USER_IDS = [int(i.strip()) for i in os.getenv("ADMIN_USER_IDS", "").split(',') if i.strip()]

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
        if "does not exist" in str(e) or "relation" in str(e) or "column" in str(e):
             # Try initialization/migration if schema error occurs
             initialize_database()
             if conn: conn.close()
             return execute_query(query, params, fetch)
        
    finally:
        if conn:
            conn.close()

def initialize_database():
    """Zaroori tables banata hai aur existing mein missing columns add karta hai."""
    logger.info("Initializing and migrating database schema...")
    queries = [
        # --- CREATE TABLES (IF NOT EXISTS) ---
        "CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT);",
        "CREATE TABLE IF NOT EXISTS characters (char_id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, image_url TEXT, rarity TEXT DEFAULT 'Common', anime TEXT DEFAULT 'Unknown');", # Added image_url, rarity, anime
        "CREATE TABLE IF NOT EXISTS user_collection (user_id BIGINT REFERENCES users(user_id), char_id INT REFERENCES characters(char_id), grab_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (user_id, char_id));",
        "CREATE TABLE IF NOT EXISTS user_profiles (user_id BIGINT PRIMARY KEY REFERENCES users(user_id), trades_done INT DEFAULT 0, gifts_sent INT DEFAULT 0, gifts_received INT DEFAULT 0, hmode_text TEXT DEFAULT 'Harem Collection', imode_text TEXT DEFAULT 'Inline Waifus');",
        "CREATE TABLE IF NOT EXISTS pending_trades (trade_id TEXT PRIMARY KEY, from_user_id BIGINT REFERENCES users(user_id), to_user_id BIGINT REFERENCES users(user_id), from_char_name TEXT NOT NULL, to_char_name TEXT NOT NULL, status TEXT DEFAULT 'PENDING', created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP);",
        
        # --- MIGRATION: ADD MISSING COLUMNS (Data safety ke liye zaroori) ---
        "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS hmode_text TEXT DEFAULT 'Harem Collection';",
        "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS imode_text TEXT DEFAULT 'Inline Waifus';",
        "ALTER TABLE characters ADD COLUMN IF NOT EXISTS image_url TEXT;",
        "ALTER TABLE characters ADD COLUMN IF NOT EXISTS rarity TEXT DEFAULT 'Common';",
        "ALTER TABLE characters ADD COLUMN IF NOT EXISTS anime TEXT DEFAULT 'Unknown';",
    ]
    
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        cur = conn.cursor()
        for query in queries:
            try:
                cur.execute(query)
                conn.commit()
            except psycopg2.ProgrammingError as pe:
                 if 'already exists' in str(pe):
                      logger.info(f"Column already exists, ignoring: {query.strip()}")
                      conn.rollback() 
                 else:
                      raise 
            except Exception as e:
                logger.error(f"Error executing query: {e} in {query.strip()}")
                conn.rollback() 

        cur.close()
        logger.info("Database schema initialized and migrated successfully.")
    except Exception as e:
        logger.error(f"Database Initialization/Migration Failed: {e}")
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
    """Waifu.im se random waifu fetch karta hai aur uski details nikalta hai."""
    try:
        response = requests.get("https://api.waifu.im/search?is_nsfw=false&tags=waifu")
        response.raise_for_status()
        data = response.json()
        
        image_url = data['images'][0]['url']
        
        # Character Name (Tags se nikalna)
        character_name = "Unknown Waifu"
        anime_name = "Unknown Anime"
        tags = data['images'][0].get('tags', [])
        for tag in tags:
            if tag.get('is_character', False):
                character_name = tag['name']
            if tag.get('is_character', False) == False and tag.get('is_nsfw', False) == False and tag.get('is_meta', False) == False:
                 anime_name = tag['name'] # Simple assumption: First non-meta/nsfw tag is the anime/source
        
        if character_name == "Unknown Waifu":
             character_name = f"Waifu #{int(time.time() * 1000)}" 
             
        # Simple Rarity Logic (Can be improved)
        rarity = random.choice(["Common", "Rare", "Epic", "Legendary"])

        return character_name.strip(), image_url, rarity, anime_name.strip()
    except Exception as e:
        logger.error(f"API Error: {e}")
        return None, None, None, None

# --- CORE LOGIC (SPAWN AND COUNTER) ---

async def spawn_waifu(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Chat mein ek nayi waifu spawn karta hai."""
    global current_spawns
    
    if chat_id in current_spawns and not current_spawns[chat_id].get('claimed', True):
        return

    name, image, rarity, anime = await get_random_waifu()
    
    if name and image:
        current_spawns[chat_id] = {'name': name, 'image': image, 'claimed': False, 'rarity': rarity, 'anime': anime}
        
        keyboard = [[InlineKeyboardButton("💖 GRAB 💖", callback_data="grab_waifu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Waifu details ko DB mein save karein (ya update karein)
        # Yeh step zaroori hai taki inline search mein photo dikh sake
        execute_query(
            "INSERT INTO characters (name, image_url, rarity, anime) VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO UPDATE SET image_url = EXCLUDED.image_url, rarity = EXCLUDED.rarity, anime = EXCLUDED.anime;",
            (name, image, rarity, anime)
        )

        await context.bot.send_photo(
            chat_id=chat_id,
            photo=image,
            caption=f"✨ Ek wild **{name}** ({rarity}) prakat hui hai! ✨\n\n**Anime:** {anime}\n\nUse apna banane ke liye 'GRAB' button dabayein!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )

# --- INLINE QUERY HANDLER (THE NEW FEATURE) ---

async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline query ko handle karta hai aur scrollable results deta hai (Screenshot feature)."""
    query = update.inline_query.query
    
    # Agar query empty hai, toh random popular characters dikhayein (ya blank)
    if not query:
        # Default/Popular characters dikhane ke liye logic yahan dal sakte hain
        results_data = execute_query(
             "SELECT name, image_url, char_id, rarity, anime FROM characters ORDER BY char_id DESC LIMIT 20;",
             fetch=True
        )
    else:
        # Search query ke anusaar characters khojein
        results_data = execute_query(
            "SELECT name, image_url, char_id, rarity, anime FROM characters WHERE name ILIKE %s LIMIT 20;",
            (f"%{query}%",), fetch=True
        )

    results = []
    
    for name, image_url, char_id, rarity, anime in results_data:
        
        # Detailed message content (Jab user click karega toh yeh chat mein bheja jayega)
        message_content = f"✨ **{name}** ✨\n" \
                          f"**Rarity:** {rarity}\n" \
                          f"**Anime:** {anime}\n" \
                          f"**ID:** {char_id}"
                          
        # Photo result: Scrollable gallery mein photo thumbnail dikhega
        results.append(
            InlineQueryResultPhoto(
                id=str(uuid.uuid4()), # Unique ID har result ke liye
                photo_url=image_url,
                thumbnail_url=image_url,
                caption=f"**{name}**\nRarity: {rarity}",
                parse_mode=ParseMode.MARKDOWN,
                
                # InputMessageContent - Yeh jab user gallery se image select karke bhejega, toh uske sath kya text jayega
                input_message_content=InputTextMessageContent(
                    message_content, 
                    parse_mode=ParseMode.MARKDOWN
                )
            )
        )
        
    await update.inline_query.answer(results, cache_time=5)


# --- TRADE AND GIFT COMMANDS (Full Implementation) ---

async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/trade: Trade request karta hai aur DB mein save karta hai (Same as previous, full logic)."""
    # ... (Same /trade logic from previous code) ...
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
    """/gift @username [char_name] - Waifu gift karta hai (Same as previous, full logic)."""
    # ... (Same /gift logic from previous code) ...
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

        char_id_result = execute_query(
            "SELECT c.char_id FROM user_collection uc JOIN characters c ON uc.char_id = c.char_id WHERE uc.user_id = %s AND c.name = %s;",
            (gifter_user_id, character_name), fetch=True
        )
        if not char_id_result:
            await update.message.reply_text(f"Aapke paas '{character_name}' naam ka character nahi hai.")
            return
        
        char_id = char_id_result[0][0]

        execute_query("DELETE FROM user_collection WHERE user_id = %s AND char_id = %s;", (gifter_user_id, char_id))
        execute_query("INSERT INTO user_collection (user_id, char_id) VALUES (%s, %s);", (target_user_id, char_id))
        
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

# --- OTHER COMMANDS (Same as previous) ---

# ... (start_command, help_command, harem_command, grab_command, changetime_command, 
# top_command, leaderboard_command, get_leaderboard_markup, fetch_leaderboard_data, 
# status_command, hmode_command, imode_command are all the same as the final code) ...

# --- GRAB/LEADERBOARD/TRADE CALLBACKS (Same as previous) ---

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buttons (Grab, Leaderboard, Trade) ko handle karta hai."""
    query = update.callback_query
    await query.answer() 
    
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    
    # --- GRAB LOGIC (Same as previous) ---
    if query.data == "grab_waifu":
        # ... (Same grab logic from previous code) ...
        if chat_id not in current_spawns or current_spawns[chat_id].get('claimed', True):
            await query.edit_message_caption(caption="Bahut der kardi! 🥺")
            return
            
        spawned_waifu = current_spawns[chat_id]
        check_query = """SELECT c.name FROM user_collection uc JOIN characters c ON uc.char_id = c.char_id WHERE uc.user_id = %s AND c.name = %s;"""
        if execute_query(check_query, (user_id, spawned_waifu['name']), fetch=True):
             await query.message.reply_text(f"**{query.from_user.first_name}** ne **{spawned_waifu['name']}** ko grab karne ki koshish ki, lekin unke paas yeh pehle se hai!", parse_mode=ParseMode.MARKDOWN)
             current_spawns[chat_id]['claimed'] = True
             return

        # Ensure all character details are updated/inserted for inline search
        execute_query(
            "INSERT INTO characters (name, image_url, rarity, anime) VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO UPDATE SET image_url = EXCLUDED.image_url, rarity = EXCLUDED.rarity, anime = EXCLUDED.anime;",
            (spawned_waifu['name'], spawned_waifu['image'], spawned_waifu['rarity'], spawned_waifu['anime'])
        )
        
        char_id_result = execute_query("SELECT char_id FROM characters WHERE name = %s;", (spawned_waifu['name'],), fetch=True)
        char_id = char_id_result[0][0]
        execute_query("INSERT INTO user_collection (user_id, char_id) VALUES (%s, %s);", (user_id, char_id))
        current_spawns[chat_id]['claimed'] = True
        
        await query.edit_message_caption(
            caption=f"✨ **{spawned_waifu['name']}** ({spawned_waifu['rarity']}) ✨\n\n💖 **Grabbed by: {query.from_user.first_name}** 💖",
            parse_mode=ParseMode.MARKDOWN
        )
        await query.message.reply_text(
            f"🎉 Badhaai ho, **{query.from_user.first_name}**! Aapne **{spawned_waifu['name']}** ko apne harem mein shaamil kar liya hai!",
            parse_mode=ParseMode.MARKDOWN
        )

    # --- LEADERBOARD LOGIC (Same as previous) ---
    elif query.data.startswith("lb_"):
        # ... (Same leaderboard logic from previous code) ...
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
             
    # --- TRADE LOGIC (ACCEPT/REJECT) (Same as previous) ---
    elif query.data.startswith(("trade_accept_", "trade_reject_")):
        # ... (Same trade accept/reject logic from previous code) ...
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
            from_char_id_result = execute_query("SELECT char_id FROM characters WHERE name = %s;", (from_char,), fetch=True)
            to_char_id_result = execute_query("SELECT char_id FROM characters WHERE name = %s;", (to_char,), fetch=True)
            
            if not from_char_id_result or not to_char_id_result:
                 await query.edit_message_text("Trade fail: Character ID nahi mila.")
                 return
                 
            from_char_id = from_char_id_result[0][0]
            to_char_id = to_char_id_result[0][0]

            execute_query("DELETE FROM user_collection WHERE user_id = %s AND char_id = %s;", (to_id, to_char_id))
            execute_query("INSERT INTO user_collection (user_id, char_id) VALUES (%s, %s);", (from_id, to_char_id))
            
            execute_query("DELETE FROM user_collection WHERE user_id = %s AND char_id = %s;", (from_id, from_char_id))
            execute_query("INSERT INTO user_collection (user_id, char_id) VALUES (%s, %s);", (to_id, from_char_id))
            
            execute_query("UPDATE user_profiles SET trades_done = trades_done + 1 WHERE user_id IN (%s, %s);", (from_id, to_id))
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
    """Har message ko count karta hai aur threshold par spawn trigger karta hai (Same as previous)."""
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
    initialize_database() # DB initialization/migration
    
    # Check config
    if not TELEGRAM_TOKEN or not DATABASE_URL or not WEBHOOK_URL:
        logger.error("Required environment variables are not set.")
        return
        
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("grab", grab_command))
    application.add_handler(CommandHandler("harem", harem_command))
    application.add_handler(CommandHandler("search", search_command)) # Command for info, main search is inline
    application.add_handler(CommandHandler("changetime", changetime_command))
    application.add_handler(CommandHandler("top", top_command)) 
    application.add_handler(CommandHandler("trade", trade_command))
    application.add_handler(CommandHandler("gift", gift_command))
    application.add_handler(CommandHandler("gtop", leaderboard_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("hmode", hmode_command))
    application.add_handler(CommandHandler("imode", imode_command))
    
    # Core Handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_counter))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # --- NEW: INLINE QUERY HANDLER ---
    application.add_handler(InlineQueryHandler(inline_search))
    
    # Run in Webhook mode for Render Web Service
    print(f"Setting webhook to {WEBHOOK_URL} on port {PORT}...")
    
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TELEGRAM_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}"
    )

# Placeholder commands (You need to include the actual functions from previous responses)
# For brevity, the full code for commands like start_command, harem_command etc. 
# is assumed to be included in the final bot.py file you are preparing.

# DUMMY implementation for placeholder functions (REMOVE these if you have the real ones)
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    register_user(update.effective_user)
    await update.message.reply_text("Bot started with all commands. Use @botname [search_term] for inline search.")
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    await update.message.reply_text("Help: Use @botname [search] for waifu gallery.")
async def harem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Harem command active.")
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Type `@botname [waifu name]` in any chat for inline search gallery!")
async def changetime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Changetime command active (Admin only).")
async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/top redirects to /gtop.")
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Leaderboard command active.")
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Status command active.")
async def hmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Harem mode command active.")
async def imode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Inline mode command active.")
def get_leaderboard_markup(time_period): return None
def fetch_leaderboard_data(time_period): return "Leaderboard data"


if __name__ == "__main__":
    main()
