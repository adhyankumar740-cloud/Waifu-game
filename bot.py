import logging
import random
import time
import requests
import os
import uuid
import json # JSON module for parsing tags in get_random_waifu

from dotenv import load_dotenv
import psycopg2
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultPhoto, InputTextMessageContent
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
    result = None
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        cur = conn.cursor()
        cur.execute(query, params)
        if fetch:
            result = cur.fetchall()
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Database Error: {e} executing: {query.split(';')[0].strip()}")
        # Schema migration/initialization is handled during main() startup, 
        # so here we just log and return None if an operational error occurs.
    finally:
        if conn:
            conn.close()
    return result

def initialize_database():
    """Zaroori tables banata hai aur existing mein missing columns add karta hai."""
    logger.info("Initializing and migrating database schema...")
    queries = [
        # --- CREATE TABLES (IF NOT EXISTS) ---
        "CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT);",
        "CREATE TABLE IF NOT EXISTS characters (char_id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, image_url TEXT, rarity TEXT DEFAULT 'Common', anime TEXT DEFAULT 'Unknown');", 
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
                 # Agar 'already exists' type ka error aaye toh ignore karo
                 if 'already exists' in str(pe):
                      conn.rollback() 
                 else:
                      raise 
            except Exception as e:
                logger.error(f"Error executing query: {e} in {query.split(';')[0].strip()}")
                conn.rollback() 

        cur.close()
        logger.info("Database schema initialized and migrated successfully.")
    except Exception as e:
        logger.error(f"Database Initialization/Migration Failed: {e}")
        # Agar yahan fail ho jaye, toh bot aage nahi badhega, jo sahi hai.
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
        # SFW Waifu images ko target karna
        response = requests.get("https://api.waifu.im/search?is_nsfw=false&tags=waifu")
        response.raise_for_status()
        data = response.json()
        
        image_url = data['images'][0]['url']
        
        character_name = "Unknown Waifu"
        anime_name = "Unknown Anime"
        tags = data['images'][0].get('tags', [])
        
        # Tags se character aur anime name nikalna
        for tag in tags:
            if tag.get('is_character', False):
                character_name = tag['name']
            elif tag.get('is_meta', False) == False and tag.get('is_nsfw', False) == False:
                 # First non-meta/nsfw tag is a good candidate for the source/anime
                 if anime_name == "Unknown Anime":
                      anime_name = tag['name'] 
        
        if character_name == "Unknown Waifu":
             # Agar naam nahi mila toh timestamp se unique naam banao
             character_name = f"Waifu #{str(uuid.uuid4())[:8]}" 
             
        # Simple Rarity Logic (Can be improved)
        rarity = random.choice(["Common", "Rare", "Epic", "Legendary"])

        return character_name.strip(), image_url, rarity, anime_name.strip()
    except Exception as e:
        logger.error(f"API Error fetching waifu: {e}")
        return None, None, None, None

# --- CORE LOGIC (SPAWN AND COUNTER) ---

async def spawn_waifu(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Chat mein ek nayi waifu spawn karta hai."""
    global current_spawns
    
    # Check if a waifu is already spawned and unclaimed in this chat
    if chat_id in current_spawns and not current_spawns[chat_id].get('claimed', True):
        return

    name, image, rarity, anime = await get_random_waifu()
    
    if name and image:
        current_spawns[chat_id] = {'name': name, 'image': image, 'claimed': False, 'rarity': rarity, 'anime': anime}
        
        keyboard = [[InlineKeyboardButton("💖 GRAB 💖", callback_data="grab_waifu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Waifu details ko DB mein save karein (ya update karein)
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

# --- COMMAND HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start: Welcome message aur user registration."""
    user = update.effective_user
    register_user(user)
    profile_data = execute_query("SELECT hmode_text FROM user_profiles WHERE user_id = %s;", (user.id,), fetch=True)
    hmode_text = profile_data[0][0] if profile_data else "Harem Collection"
    
    await update.message.reply_html(
        rf"Salaam, {user.mention_html()}! Main **Grab Your Waifu Bot** hoon. 😼"
        f"\n\nHar {SPAWN_THRESHOLD} messages ke baad ek nayi waifu spawn hogi, jise aap **GRAB** kar sakte hain!"
        f"\n\n**Main Commands:**"
        f"\n/grab - Spawned waifu ko claim karein."
        f"\n/harem - Apni {hmode_text} dekhein."
        f"\n/status - Apne Harem Stats dekhein."
        f"\n/trade & /gift - Waifus ka aadaan-pradaan karein."
        f"\n\n**Gallery Search:**"
        f"\nKisi bhi chat mein type karein: <b>@botname [waifu name]</b> - Gallery mein search karne ke liye!"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help: FAQ/Madad."""
    await update.message.reply_text(
        "**FAQ/Madad:**\n"
        "1. **Spawn:** Har {SPAWN_THRESHOLD} messages ke baad ek waifu spawn hogi. Use /grab ya button se claim karein.\n"
        "2. **Collection:** /harem se aapki collection dekhein.\n"
        "3. **Trade/Gift:** /trade @user [Aapka Char] for [Unka Char] or /gift @user [Char Name].\n"
        "4. **Search:** Chat mein **@botname [waifu name]** type karein gallery search ke liye."
        , parse_mode=ParseMode.MARKDOWN
    )

async def grab_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/grab: Spawned waifu ko claim karta hai (Text fallback)."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if chat_id not in current_spawns or current_spawns[chat_id].get('claimed', True):
        await update.message.reply_text("Abhi koi waifu spawned nahi hai. Agli spawn ka intezaar karein!")
        return

    spawned_waifu = current_spawns[chat_id]
    
    # Check if user already has the character
    check_query = """SELECT c.name FROM user_collection uc JOIN characters c ON uc.char_id = c.char_id WHERE uc.user_id = %s AND c.name = %s;"""
    if execute_query(check_query, (user_id, spawned_waifu['name']), fetch=True):
         await update.message.reply_text(f"**{update.effective_user.first_name}** ke paas **{spawned_waifu['name']}** pehle se hai!", parse_mode=ParseMode.MARKDOWN)
         return

    # Claim the character
    execute_query(
        "INSERT INTO characters (name, image_url, rarity, anime) VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO UPDATE SET image_url = EXCLUDED.image_url, rarity = EXCLUDED.rarity, anime = EXCLUDED.anime;",
        (spawned_waifu['name'], spawned_waifu['image'], spawned_waifu['rarity'], spawned_waifu['anime'])
    )
    
    char_id_result = execute_query("SELECT char_id FROM characters WHERE name = %s;", (spawned_waifu['name'],), fetch=True)
    if char_id_result:
        char_id = char_id_result[0][0]
        execute_query("INSERT INTO user_collection (user_id, char_id) VALUES (%s, %s);", (user_id, char_id))
        current_spawns[chat_id]['claimed'] = True
        
        # Edit the original message (if possible)
        try:
             await context.bot.edit_message_caption(
                 chat_id=chat_id,
                 message_id=update.message.message_id - 1, # Thoda mushkil, agar button se na ho to ignore
                 caption=f"✨ **{spawned_waifu['name']}** ({spawned_waifu['rarity']}) ✨\n\n💖 **Grabbed by: {update.effective_user.first_name}** 💖",
                 parse_mode=ParseMode.MARKDOWN,
                 reply_markup=None
             )
        except Exception:
             pass # Agar edit fail ho toh ignore karo
             
        await update.message.reply_text(
            f"🎉 Badhaai ho, **{update.effective_user.first_name}**! Aapne **{spawned_waifu['name']}** ko apne harem mein shaamil kar liya hai!",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text("Grab failed due to DB error. Please try again.")


async def harem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/harem: User ka collection dikhata hai."""
    user_id = update.effective_user.id
    
    # Get user's preferred collection name
    profile_data = execute_query("SELECT hmode_text FROM user_profiles WHERE user_id = %s;", (user_id,), fetch=True)
    hmode_text = profile_data[0][0] if profile_data else "Harem Collection"

    collection_data = execute_query(
        "SELECT c.name, c.rarity, c.anime FROM user_collection uc JOIN characters c ON uc.char_id = c.char_id WHERE uc.user_id = %s ORDER BY c.rarity, c.name;",
        (user_id,), fetch=True
    )

    if not collection_data:
        await update.message.reply_text(f"Aapki **{hmode_text}** abhi khaali hai. Spawn hone wali waifus ko grab karein!", parse_mode=ParseMode.MARKDOWN)
        return

    harem_list = [f"• {name} ({rarity}) - *{anime[:20]}...*" for name, rarity, anime in collection_data]
    harem_text = f"💖 **{update.effective_user.first_name}**'s {hmode_text} ({len(harem_list)} Waifus) 💖\n\n" + "\n".join(harem_list)
    
    # Agar list bahut badi ho toh split karein
    if len(harem_text) > 4096:
         # Simplified split for Telegram message limit
         harem_text = harem_text[:4000] + "\n\n...(Aur bhi hain, lekin Telegram limit ke kaaran poori list nahi dikh rahi)."

    await update.message.reply_text(harem_text, parse_mode=ParseMode.MARKDOWN)

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/search: Inline search ke bare mein batata hai."""
    profile_data = execute_query("SELECT imode_text FROM user_profiles WHERE user_id = %s;", (update.effective_user.id,), fetch=True)
    imode_text = profile_data[0][0] if profile_data else "Inline Waifus"
    await update.message.reply_text(
        f"**{imode_text}** Gallery Search:\n\n"
        f"Kisi bhi chat mein type karein: **@botname [waifu name]**"
        f"\n\nExample: `@botname Rem`"
        , parse_mode=ParseMode.MARKDOWN
    )

# Trading and Gifting functions (Same as previous response, included for completeness)
async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (Full /trade logic) ...
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
            "SELECT 1 FROM user_collection uc JOIN characters c ON uc.char_id = c.char_id WHERE uc.user_id = %s AND c.name ILIKE %s;",
            (from_user_id, my_char_name), fetch=True
        )
        if not giver_has_char:
            await update.message.reply_text(f"Aapke paas '{my_char_name}' naam ka character nahi hai.")
            return
        
        receiver_has_char = execute_query(
            "SELECT 1 FROM user_collection uc JOIN characters c ON uc.char_id = c.char_id WHERE uc.user_id = %s AND c.name ILIKE %s;",
            (target_user_id, their_char_name), fetch=True
        )
        if not receiver_has_char:
            await update.message.reply_text(f"@{target_username} ke paas '{their_char_name}' nahi hai.")
            return
        
        # Use exact names found if multiple matches exist, but for now we assume ILIKE is sufficient
        
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
    # ... (Full /gift logic) ...
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
            "SELECT c.char_id FROM user_collection uc JOIN characters c ON uc.char_id = c.char_id WHERE uc.user_id = %s AND c.name ILIKE %s;",
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

# --- INLINE QUERY HANDLER (Search Gallery) ---

async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline query ko handle karta hai aur scrollable results deta hai."""
    query = update.inline_query.query
    
    # User ke preferred inline text ko fetch karna
    profile_data = execute_query("SELECT imode_text FROM user_profiles WHERE user_id = %s;", (update.inline_query.from_user.id,), fetch=True)
    imode_text = profile_data[0][0] if profile_data else "Inline Waifus"
    
    # Agar query empty hai, toh recently added characters dikhayein
    if not query:
        results_data = execute_query(
             "SELECT name, image_url, char_id, rarity, anime FROM characters ORDER BY char_id DESC LIMIT 30;",
             fetch=True
        )
    else:
        # Search query ke anusaar characters khojein (case-insensitive search)
        results_data = execute_query(
            "SELECT name, image_url, char_id, rarity, anime FROM characters WHERE name ILIKE %s LIMIT 30;",
            (f"%{query}%",), fetch=True
        )

    results = []
    
    for name, image_url, char_id, rarity, anime in results_data:
        
        message_content = f"✨ **{name}** ✨\n" \
                          f"**Rarity:** {rarity}\n" \
                          f"**Anime:** {anime}\n" \
                          f"**(DB ID: {char_id})**"
                          
        # InlineQueryResultPhoto for the image gallery look
        results.append(
            InlineQueryResultPhoto(
                id=str(uuid.uuid4()), 
                photo_url=image_url,
                thumbnail_url=image_url,
                title=f"{imode_text}: {name}",
                caption=f"**{name}**\nRarity: {rarity}",
                parse_mode=ParseMode.MARKDOWN,
                
                # InputMessageContent - Yeh jab user gallery se image select karke bhejega
                input_message_content=InputTextMessageContent(
                    message_content, 
                    parse_mode=ParseMode.MARKDOWN
                )
            )
        )
        
    await update.inline_query.answer(results, cache_time=5)


# --- CALLBACKS & OTHER HANDLERS ---

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buttons (Grab, Leaderboard, Trade) ko handle karta hai."""
    query = update.callback_query
    await query.answer() 
    
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    
    # --- GRAB LOGIC (Same as grab_command) ---
    if query.data == "grab_waifu":
        if chat_id not in current_spawns or current_spawns[chat_id].get('claimed', True):
            await query.edit_message_caption(caption="Bahut der kardi! 🥺")
            return
            
        spawned_waifu = current_spawns[chat_id]
        
        check_query = """SELECT c.name FROM user_collection uc JOIN characters c ON uc.char_id = c.char_id WHERE uc.user_id = %s AND c.name = %s;"""
        if execute_query(check_query, (user_id, spawned_waifu['name']), fetch=True):
             await query.message.reply_text(f"**{query.from_user.first_name}** ne **{spawned_waifu['name']}** ko grab karne ki koshish ki, lekin unke paas yeh pehle se hai!", parse_mode=ParseMode.MARKDOWN)
             current_spawns[chat_id]['claimed'] = True
             await query.edit_message_reply_markup(reply_markup=None) # Remove button
             return

        execute_query(
            "INSERT INTO characters (name, image_url, rarity, anime) VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO UPDATE SET image_url = EXCLUDED.image_url, rarity = EXCLUDED.rarity, anime = EXCLUDED.anime;",
            (spawned_waifu['name'], spawned_waifu['image'], spawned_waifu['rarity'], spawned_waifu['anime'])
        )
        
        char_id_result = execute_query("SELECT char_id FROM characters WHERE name = %s;", (spawned_waifu['name'],), fetch=True)
        
        if char_id_result:
            char_id = char_id_result[0][0]
            execute_query("INSERT INTO user_collection (user_id, char_id) VALUES (%s, %s);", (user_id, char_id))
            current_spawns[chat_id]['claimed'] = True
            
            await query.edit_message_caption(
                caption=f"✨ **{spawned_waifu['name']}** ({spawned_waifu['rarity']}) ✨\n\n💖 **Grabbed by: {query.from_user.first_name}** 💖",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=None # Remove the button
            )
            
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🎉 Badhaai ho, **{query.from_user.first_name}**! Aapne **{spawned_waifu['name']}** ko apne harem mein shaamil kar liya hai!",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.edit_message_caption(caption="Grab failed due to DB error. Please try again.")

    # --- LEADERBOARD LOGIC (Simplified placeholders) ---
    elif query.data.startswith("lb_"):
        # Yahan par aapka leaderboard data fetch/display logic ayega
        await query.edit_message_text("Leaderboard data updated! (Implementation pending)")

             
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
        receiver_name = query.from_user.first_name # The one who accepted/rejected

        if action == "accept":
            # Char IDs ko dobara fetch karna for safety, using ILIKE for flexibility
            from_char_id_result = execute_query("SELECT char_id FROM characters WHERE name ILIKE %s;", (from_char,), fetch=True)
            to_char_id_result = execute_query("SELECT char_id FROM characters WHERE name ILIKE %s;", (to_char,), fetch=True)
            
            if not from_char_id_result or not to_char_id_result:
                 await query.edit_message_text("Trade fail: Character ID nahi mila (ya naam match nahi hua).")
                 return
                 
            from_char_id = from_char_id_result[0][0]
            to_char_id = to_char_id_result[0][0]

            # 1. Receiver ka char (to_char) Giver ko dena
            execute_query("DELETE FROM user_collection WHERE user_id = %s AND char_id = %s;", (to_id, to_char_id))
            execute_query("INSERT INTO user_collection (user_id, char_id) VALUES (%s, %s);", (from_id, to_char_id))
            
            # 2. Giver ka char (from_char) Receiver ko dena
            execute_query("DELETE FROM user_collection WHERE user_id = %s AND char_id = %s;", (from_id, from_char_id))
            execute_query("INSERT INTO user_collection (user_id, char_id) VALUES (%s, %s);", (to_id, from_char_id))
            
            # 3. Trade stats update karna
            execute_query("UPDATE user_profiles SET trades_done = trades_done + 1 WHERE user_id IN (%s, %s);", (from_id, to_id))
            execute_query("UPDATE pending_trades SET status = 'ACCEPTED' WHERE trade_id = %s;", (trade_id,))
            
            await query.edit_message_text(f"✅ Trade Accepted! Aapne '{to_char}' dekar '{from_char}' le liya hai.")
            
            try:
                await context.bot.send_message(
                    chat_id=from_id,
                    text=f"✅ **Trade Accepted!** {receiver_name} ne aapka trade request accept kar liya hai. Aapko **{to_char}** mil gaya hai!"
                )
            except Exception:
                pass
                
        elif action == "reject":
            execute_query("UPDATE pending_trades SET status = 'REJECTED' WHERE trade_id = %s;", (trade_id,))
            await query.edit_message_text("❌ Trade Rejected.")
            
            try:
                await context.bot.send_message(
                    chat_id=from_id,
                    text=f"❌ **Trade Rejected!** {receiver_name} ne aapka trade request reject kar diya hai."
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
        
# --- PLACEHOLDER FUNCTIONS (Minimal working versions) ---

async def changetime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to change spawn time/threshold."""
    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("Aap yeh command use nahi kar sakte.")
        return
    await update.message.reply_text("Spawn time changed (Implementation pending).")

async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/top redirects to /gtop (leaderboard)."""
    await leaderboard_command(update, context)

def fetch_leaderboard_data(time_period='global'):
    """DB se leaderboard data fetch karta hai."""
    # Simplified query for demonstration
    results = execute_query(
        """
        SELECT u.first_name, COUNT(uc.char_id) as count
        FROM users u 
        JOIN user_collection uc ON u.user_id = uc.user_id 
        GROUP BY u.first_name 
        ORDER BY count DESC 
        LIMIT 10;
        """, fetch=True
    )
    if not results:
        return "Abhi koi data nahi hai. Pehli waifu grab karein!"
        
    text = "👑 **Global Top 10 Waifu Collectors** 👑\n\n"
    for i, (name, count) in enumerate(results):
        text += f"{i+1}. {name}: **{count}** waifus\n"
    return text

def get_leaderboard_markup(time_period):
    """Leaderboard buttons create karta hai."""
    keyboard = [
        [
            InlineKeyboardButton("Global", callback_data="lb_global"),
            InlineKeyboardButton("Weekly (P)", callback_data="lb_weekly"), # (P) Placeholder
            InlineKeyboardButton("Daily (P)", callback_data="lb_daily"),   # (P) Placeholder
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/gtop: Leaderboard dikhata hai."""
    leaderboard_text = fetch_leaderboard_data()
    reply_markup = get_leaderboard_markup('global')
    
    await update.message.reply_text(
        leaderboard_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status: User ka profile aur stats dikhata hai."""
    user_id = update.effective_user.id
    
    # 1. Stats from user_profiles
    profile_data = execute_query(
        "SELECT trades_done, gifts_sent, gifts_received, hmode_text, imode_text FROM user_profiles WHERE user_id = %s;", 
        (user_id,), 
        fetch=True
    )
    # 2. Total collection count
    count_data = execute_query(
        "SELECT COUNT(char_id) FROM user_collection WHERE user_id = %s;", 
        (user_id,), 
        fetch=True
    )
    
    trades_done, gifts_sent, gifts_received, hmode_text, imode_text = profile_data[0] if profile_data else (0, 0, 0, "Harem Collection", "Inline Waifus")
    total_waifus = count_data[0][0] if count_data else 0

    status_text = (
        f"👤 **{update.effective_user.first_name}'s Profile Status** 📊\n\n"
        f"💖 **Total Waifus:** {total_waifus}\n"
        f"🔄 **Trades Done:** {trades_done}\n"
        f"🎁 **Gifts Sent:** {gifts_sent}\n"
        f"🧧 **Gifts Received:** {gifts_received}\n\n"
        f"🔧 **Current Settings:**\n"
        f"  • /harem Mode: `{hmode_text}`\n"
        f"  • /search Mode: `{imode_text}`"
    )
    await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)

async def hmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/hmode [text]: Customizes the name of the /harem list."""
    if not context.args:
        await update.message.reply_text("Kripya naya naam enter karein. Example: `/hmode Waifu Paradise`")
        return
        
    new_text = " ".join(context.args).strip()[:30] # Limit to 30 chars
    user_id = update.effective_user.id
    
    execute_query("UPDATE user_profiles SET hmode_text = %s WHERE user_id = %s;", (new_text, user_id))
    await update.message.reply_text(f"✅ Success! Aapki **/harem** list ab **'{new_text}'** ke naam se jaani jayegi.")

async def imode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/imode [text]: Customizes the title of the Inline Search."""
    if not context.args:
        await update.message.reply_text("Kripya naya naam enter karein. Example: `/imode My Waifu Gallery`")
        return
        
    new_text = " ".join(context.args).strip()[:30]
    user_id = update.effective_user.id
    
    execute_query("UPDATE user_profiles SET imode_text = %s WHERE user_id = %s;", (new_text, user_id))
    await update.message.reply_text(f"✅ Success! Aapki Inline Search Gallery ab **'{new_text}'** ke title se dikhegi.")

# --- WEBHOOK MAIN FUNCTION ---

def main():
    """Bot ko Webhook mode mein start karta hai."""
    
    # Check config
    if not TELEGRAM_TOKEN or not DATABASE_URL or not WEBHOOK_URL:
        logger.error("Required environment variables (TELEGRAM_TOKEN, DATABASE_URL, WEBHOOK_URL) are not set.")
        return
        
    initialize_database() # DB initialization/migration

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("grab", grab_command))
    application.add_handler(CommandHandler("harem", harem_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("changetime", changetime_command))
    application.add_handler(CommandHandler("top", top_command)) # Alias for /gtop
    application.add_handler(CommandHandler("trade", trade_command))
    application.add_handler(CommandHandler("gift", gift_command))
    application.add_handler(CommandHandler("gtop", leaderboard_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("hmode", hmode_command))
    application.add_handler(CommandHandler("imode", imode_command))
    
    # Core Handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_counter))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # INLINE QUERY HANDLER (For the gallery search)
    application.add_handler(InlineQueryHandler(inline_search))
    
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
