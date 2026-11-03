import asyncio
import logging
import os
import time
import resource
import re
import aiohttp

from dotenv import load_dotenv
load_dotenv(override=True) 

import asyncpg
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command, CommandObject, BaseFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    InlineQueryResultArticle, InputTextMessageContent
)
from aiogram.utils.deep_linking import create_start_link
from aiogram.enums import ChatType
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", "0"))
PUBLIC_CHANNEL_ID = int(os.getenv("PUBLIC_CHANNEL_ID", "0"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")

if not BOT_TOKEN or not DATABASE_URL:
    raise SystemExit("❌ Faltan BOT_TOKEN o DATABASE_URL en las variables de entorno.")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())
db_pool: asyncpg.Pool = None

# ================= ESTADOS (FSM) =================
class AdminStates(StatesGroup):
    waiting_for_welcome = State()
    waiting_for_sticker = State()
    waiting_for_tmdb = State()

# ================= FILTROS =================
class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id == ADMIN_ID

_media_groups: dict[str, float] = {}

def _is_first_in_group(group_id: str | None) -> bool:
    if not group_id: return True
    now = time.time()
    for k in [k for k, v in _media_groups.items() if now - v > 300]: del _media_groups[k]
    if group_id in _media_groups: return False
    _media_groups[group_id] = now
    return True

# ================= AUTO-BORRADO =================
async def delete_later(chat_id: int, message_id: int, delay: int = 600):
    await asyncio.sleep(delay)
    try: await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception: pass

# ================= TMDB =================
async def fetch_tmdb_data(tipo: str, titulo: str):
    if not TMDB_API_KEY: return "SIN_API_KEY"
    url = f"https://api.themoviedb.org/3/search/{tipo}?api_key={TMDB_API_KEY}&language=es-ES&query={titulo}"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200: return None
                data = await resp.json()
                if not data.get("results"): return None
                item = data["results"][0]
                
                detail_url = f"https://api.themoviedb.org/3/{tipo}/{item['id']}?api_key={TMDB_API_KEY}&language=es-ES"
                async with session.get(detail_url) as detail_resp:
                    return await detail_resp.json()
    except Exception as e:
        logging.error(f"Error TMDB: {e}")
        return None

# ================= CALLBACKS =================
class SearchCB(CallbackData, prefix="srch"): code: str
class FavToggleCB(CallbackData, prefix="fav"): code: str
class SeasonCB(CallbackData, prefix="season"): code: str
class AdminCB(CallbackData, prefix="adm"): section: str
class PublishCB(CallbackData, prefix="pub"): code: str
class CheckSubCB(CallbackData, prefix="chksub"): payload: str = ""
class TmdbSaveCB(CallbackData, prefix="tmdb"): tipo: str; tmdb_id: int

# ================= VERIFICACIÓN DE SUSCRIPCIÓN =================
async def check_sub(user_id: int) -> bool:
    if PUBLIC_CHANNEL_ID == 0: return True
    try:
        member = await bot.get_chat_member(chat_id=PUBLIC_CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logging.error(f"Error verificando suscripción: {e}")
        return False

async def send_join_message(event, payload: str = ""):
    try:
        chat = await bot.get_chat(PUBLIC_CHANNEL_ID)
        if chat.invite_link: link = chat.invite_link
        elif chat.username: link = f"https://t.me/{chat.username}"
        else: link = "https://t.me/TuCanalPrincipal"
    except: link = "https://t.me/TuCanalPrincipal"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Unirse al Canal", url=link)],
        [InlineKeyboardButton(text="✅ Ya me uní", callback_data=CheckSubCB(payload=payload).pack())]
    ])
    text = ("🔒 <b>ACCESO RESTRINGIDO</b>\n\nPara poder ver las películas y series, primero debes estar suscrito a nuestro canal principal.\n\n👉 Únete al canal y luego pulsa el botón <b>✅ Ya me uní</b>.")
    
    if isinstance(event, types.CallbackQuery):
        await event.message.answer(text, reply_markup=kb)
        await event.answer("Debes suscribirte al canal primero.", show_alert=True)
    else:
        await event.answer(text, reply_markup=kb)

@dp.callback_query(CheckSubCB.filter())
async def on_check_sub(query: types.CallbackQuery, callback_data: CheckSubCB):
    if await check_sub(query.from_user.id):
        await query.message.delete()
        await query.message.answer("✅ <b>¡Gracias por unirte!</b> Ya tienes acceso completo al bot.\n\nEscribe /start para comenzar.")
        if callback_data.payload: await deliver_by_code(query.from_user.id, callback_data.payload)
    else:
        await query.answer("❌ Aún no estás suscrito. Por favor, únete al canal primero.", show_alert=True)

# ================= DB =================
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2, ssl="require")
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS content (
                code TEXT PRIMARY KEY, title TEXT NOT NULL, description_text TEXT,
                description_photo_id TEXT, tags TEXT DEFAULT '', content_type TEXT DEFAULT 'movie',
                file_id TEXT, media_type TEXT, status TEXT DEFAULT 'pending',
                public_message_id BIGINT, tmdb_id INT, created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS seasons (
                code TEXT PRIMARY KEY, content_code TEXT NOT NULL REFERENCES content(code) ON DELETE CASCADE,
                season_number INTEGER NOT NULL, UNIQUE(content_code, season_number)
            );
            CREATE TABLE IF NOT EXISTS episodes (
                id SERIAL PRIMARY KEY, season_code TEXT NOT NULL REFERENCES seasons(code) ON DELETE CASCADE,
                episode_number INTEGER NOT NULL, file_id TEXT NOT NULL, media_type TEXT NOT NULL, title TEXT,
                UNIQUE(season_code, episode_number)
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT, joined_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS favorites (
                user_id BIGINT, code TEXT, added_at TIMESTAMPTZ DEFAULT NOW(), PRIMARY KEY (user_id, code)
            );
            CREATE TABLE IF NOT EXISTS message_map (
                bot_message_id BIGINT PRIMARY KEY, target_code TEXT NOT NULL, target_type TEXT NOT NULL,
                used BOOLEAN DEFAULT FALSE, created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            ALTER TABLE content ADD COLUMN IF NOT EXISTS public_message_id BIGINT;
            ALTER TABLE content ADD COLUMN IF NOT EXISTS tmdb_id INT;
            ALTER TABLE message_map ADD COLUMN IF NOT EXISTS used BOOLEAN DEFAULT FALSE;
            ALTER TABLE message_map ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

            UPDATE content SET status = 'ready' WHERE file_id IS NOT NULL AND status != 'ready';
            UPDATE content SET status = 'ready' WHERE content_type = 'series' AND code IN (SELECT content_code FROM seasons) AND status != 'ready';
        """)

async def register_user(user: types.User):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id, username, first_name) VALUES ($1,$2,$3) ON CONFLICT (user_id) DO NOTHING", user.id, user.username, user.first_name)

# ================= HELPERS DE LINKING =================
async def get_target_by_msg_id(bot_msg_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT target_code, target_type FROM message_map WHERE bot_message_id=$1", bot_msg_id)

async def get_recent_pending():
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT target_code, target_type FROM message_map WHERE used = FALSE AND created_at > NOW() - INTERVAL '10 minutes' ORDER BY created_at DESC LIMIT 1")

async def mark_used(target_code: str, target_type: str):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE message_map SET used=TRUE WHERE target_code=$1 AND target_type=$2", target_code, target_type)

async def save_confirmation(bot_msg_id: int, target_code: str, target_type: str):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO message_map (bot_message_id, target_code, target_type) VALUES ($1,$2,$3)", bot_msg_id, target_code, target_type)

# ================= GENERADOR DE POSTS =================
async def generate_post_kb(code: str):
    async with db_pool.acquire() as conn:
        content = await conn.fetchrow("SELECT content_type FROM content WHERE code=$1", code)
        if not content: return None
        buttons = []
        if content["content_type"] == "movie":
            link = await create_start_link(bot, payload=code, encode=False)
            buttons.append([InlineKeyboardButton(text="🎬 Ver Película", url=link)])
        else:
            seasons = await conn.fetch("SELECT code, season_number FROM seasons WHERE content_code=$1 ORDER BY season_number", code)
            for s in seasons:
                link = await create_start_link(bot, payload=s["code"], encode=False)
                buttons.append([InlineKeyboardButton(text=f"🎬 Temporada {s['season_number']}", url=link)])
        return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

async def send_formatted_post(chat_id: int, code: str):
    async with db_pool.acquire() as conn:
        content = await conn.fetchrow("SELECT * FROM content WHERE code=$1", code)
    if not content: return False

    kb = await generate_post_kb(code)
    text = content["description_text"] or ""
    photo = content["description_photo_id"]

    try:
        if photo: msg = await bot.send_photo(chat_id, photo=photo, caption=text, reply_markup=kb)
        elif text: msg = await bot.send_message(chat_id, text=text, reply_markup=kb)
        
        if chat_id == PUBLIC_CHANNEL_ID and msg:
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE content SET public_message_id=$1 WHERE code=$2", msg.message_id, code)
                
            async with db_pool.acquire() as conn:
                sticker_row = await conn.fetchrow("SELECT value FROM settings WHERE key='post_sticker'")
            if sticker_row and sticker_row["value"]:
                try: await bot.send_sticker(chat_id, sticker=sticker_row["value"])
                except Exception as e: logging.warning(f"Error sticker: {e}")
        return True
    except Exception as e:
        logging.error(f"Error enviando post {code}: {e}")
        return False

# ================= LÓGICA TMDB =================
async def process_tmdb_query(message: types.Message, query_text: str):
    parts = query_text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("⚠️ Debes indicar el tipo (peli/serie/anime) y el título.\nEjemplo: <code>peli Inception</code>")
    
    tipo_raw, titulo = parts[0].lower(), parts[1]
    tipo_tmdb = "movie" if tipo_raw in ["peli", "pelicula", "película", "movie"] else "tv"
    
    status_msg = await message.answer("⏳ Buscando en TMDB...")
    
    data = await fetch_tmdb_data(tipo_tmdb, titulo)
    
    if data == "SIN_API_KEY":
        return await status_msg.edit_text("❌ Falta configurar `TMDB_API_KEY` en las variables de entorno de BotKeep.")
    if not data:
        return await status_msg.edit_text("❌ No se encontró el contenido en TMDB o hubo un error de conexión. Intenta de nuevo.")
        
    nombre = data.get("title") or data.get("name")
    sinopsis = data.get("overview") or "Sin sinopsis disponible."
    año = (data.get("release_date") or data.get("first_air_date") or "N/A")[:4]
    
    # 🚨 MODIFICACIÓN AQUÍ: Agregamos el # y quitamos espacios dentro del nombre del género
    generos = ", ".join([f"#{g['name'].replace(' ', '')}" for g in data.get("genres", [])])
    
    poster_path = data.get("poster_path")
    tmdb_id = data["id"]
    
    temporadas_str = ""
    capitulos_str = ""
    
    if tipo_tmdb == "tv":
        num_temp = data.get("number_of_seasons", 0)
        num_eps = data.get("number_of_episodes", 0)
        temporadas_str = f"🍀Temporadas: {num_temp}\n"
        capitulos_str = f"🍀Capítulos: {num_eps}\n"
        encabezado = "🧃Anime:" if tipo_raw == "anime" else "🧃Serie:"
    else:
        encabezado = "🧃Película:"

    texto = (
        f"{encabezado} {nombre}\n"
        f"🍀Géneros: {generos}\n"
        f"{temporadas_str}"
        f"{capitulos_str}"
        f"🍀Año: {año}\n\n"
        f"🔋Sinopsis:\n{sinopsis}\n\n"
        f"▬ι𓆃__ ▬ι𓆃__ ▬ι𓆃 ____ ▬ι𓆃"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📤 Enviar al Canal de Almacenamiento", callback_data=TmdbSaveCB(tipo=tipo_tmdb, tmdb_id=tmdb_id).pack())
    ]])

    await status_msg.delete()
    
    if poster_path:
        photo_url = f"https://image.tmdb.org/t/p/w500{poster_path}"
        try:
            await message.answer_photo(photo=photo_url, caption=texto, reply_markup=kb)
            return
        except Exception as e:
            logging.error(f"Error poster: {e}")
            
    await message.answer(texto, reply_markup=kb)

@dp.message(Command("tmdb"), IsAdmin())
async def cmd_tmdb(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("🎬 Uso: <code>/tmdb peli Inception</code> o <code>/tmdb serie Breaking Bad</code>")
    await process_tmdb_query(message, command.args)

@dp.message(AdminStates.waiting_for_tmdb, IsAdmin())
async def process_tmdb_state(message: types.Message, state: FSMContext):
    if message.text == "/cancelar":
        await state.clear()
        return await message.answer("❌ Búsqueda cancelada.", reply_markup=admin_panel_kb())
    await state.clear()
    await process_tmdb_query(message, message.text)

@dp.callback_query(TmdbSaveCB.filter(), IsAdmin())
async def on_tmdb_save(query: types.CallbackQuery, callback_data: TmdbSaveCB):
    await query.answer("Enviando al canal...")
    
    try:
        msg = await query.message.copy_to(chat_id=STORAGE_CHANNEL_ID)
    except Exception as e:
        return await query.message.edit_text(f"❌ Error al enviar al canal: {e}")

    code = f"peli_{msg.message_id}"
    texto_completo = query.message.caption or query.message.text
    title = texto_completo.split("\n")[0].replace("🧃Película:", "").replace("🧃Serie:", "").replace("🧃Anime:", "").strip()
    photo_id = msg.photo[-1].file_id if msg.photo else None

    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO content (code, title, description_text, description_photo_id, status, tmdb_id, content_type)
               VALUES ($1,$2,$3,$4,'pending',$5,$6)
               ON CONFLICT (code) DO UPDATE SET description_text=EXCLUDED.description_text""",
            code, title, texto_completo, photo_id, callback_data.tmdb_id, callback_data.tipo
        )
        reply = await bot.send_message(
            chat_id=STORAGE_CHANNEL_ID,
            text=f"⬆️ <b>Responde a este mensaje con el video.</b>\nCódigo: <code>{code}</code>"
        )
        await conn.execute("INSERT INTO message_map (bot_message_id, target_code, target_type) VALUES ($1,$2,'description')", reply.message_id, code)

    try:
        if query.message.photo:
            await query.message.edit_caption(caption=texto_completo + "\n\n✅ <b>Enviado al canal. Responde allí con el video.</b>", reply_markup=None)
        else:
            await query.message.edit_text(text=texto_completo + "\n\n✅ <b>Enviado al canal. Responde allí con el video.</b>", reply_markup=None)
    except: pass

# ================= CANAL PRIVADO =================
@dp.channel_post(F.chat.id == STORAGE_CHANNEL_ID)
async def handle_storage(message: types.Message):
    text = (message.text or message.caption or "").strip()
    
    if text.lower().startswith("/cancelar"):
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM message_map WHERE used=FALSE")
        return await message.reply("✅ Procesos pendientes cancelados.")

    if text.lower().startswith("/season"): return await handle_season_command(message)
    if text.lower().startswith("/link"): return await handle_manual_link(message)

    if message.video or message.document: return await handle_content(message)
    if message.text or message.photo:
        if not _is_first_in_group(message.media_group_id): return
        return await handle_description(message)

async def handle_description(message: types.Message):
    text = message.text or message.caption or ""
    code = f"peli_{message.message_id}"
    first_line = text.split("\n")[0][:150].strip()
    title = first_line if first_line else f"Contenido {message.message_id}"
    photo_id = message.photo[-1].file_id if message.photo else None
    tags = " ".join(w for w in text.split() if w.startswith("#")).lower()

    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO content (code, title, description_text, description_photo_id, tags, status)
               VALUES ($1,$2,$3,$4,$5,'pending')
               ON CONFLICT (code) DO UPDATE SET
                 title=EXCLUDED.title, description_text=EXCLUDED.description_text,
                 description_photo_id=EXCLUDED.description_photo_id, tags=EXCLUDED.tags""",
            code, title, text, photo_id, tags,
        )

    reply = await message.reply(
        f"✅ Descripción guardada.\nCódigo: <code>{code}</code>\n\n"
        f"👉 Ahora reenvía el video/documento:\n"
        f"• Como <b>respuesta</b> a este mensaje, o\n"
        f"• Directamente (se vinculará solo en los próximos 10 min)\n"
        f"• Si es serie: responde con <code>/season N</code>"
    )
    await save_confirmation(reply.message_id, code, "description")

async def handle_content(message: types.Message):
    target = None
    if message.reply_to_message: target = await get_target_by_msg_id(message.reply_to_message.message_id)
    if not target: target = await get_recent_pending()

    if not target:
        text = message.caption or ""
        if text.strip() and len(text) > 50: return await create_standalone_movie(message)
        return await message.reply("⚠️ No hay descripción pendiente.\nReenvía el texto primero o usa <code>/link codigo</code> respondiendo a este video.")

    if target["target_type"] == "season": await add_episode(message, target["target_code"])
    else:
        await add_movie(message, target["target_code"])
        await mark_used(target["target_code"], "description")

async def add_movie(message: types.Message, code: str):
    file_id = message.video.file_id if message.video else message.document.file_id
    media_type = "video" if message.video else "document"
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE content SET file_id=$1, media_type=$2, content_type='movie', status='ready' WHERE code=$3", file_id, media_type, code)
    
    pub_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📢 Publicar ahora en Canal Público", callback_data=PublishCB(code=code).pack())]])
    await message.reply(f"✅ Película lista y vinculada.\nCódigo: <code>{code}</code>\n\n¿Deseas publicarlo en tu canal público?", reply_markup=pub_kb)

async def create_standalone_movie(message: types.Message):
    code = f"peli_{message.message_id}"
    text = message.caption or ""
    title = text.split("\n")[0][:150].strip() or f"Pelicula {message.message_id}"
    file_id = message.video.file_id if message.video else message.document.file_id
    media_type = "video" if message.video else "document"
    tags = " ".join(w for w in text.split() if w.startswith("#")).lower()

    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO content (code,title,description_text,description_photo_id,tags,content_type,file_id,media_type,status)
               VALUES ($1,$2,$3,NULL,$4,'movie',$5,$6,'ready')""",
            code, title, text, tags, file_id, media_type,
        )
    pub_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📢 Publicar ahora en Canal Público", callback_data=PublishCB(code=code).pack())]])
    await message.reply(f"✅ Post combinado guardado.\nCódigo: <code>{code}</code>", reply_markup=pub_kb)

async def handle_season_command(message: types.Message):
    if not message.reply_to_message: return await message.reply("⚠️ Debes <b>responder</b> al mensaje del bot con <code>/season N</code>")
    target = await get_target_by_msg_id(message.reply_to_message.message_id)
    if not target or target["target_type"] != "description": return await message.reply("⚠️ Debes responder al mensaje de confirmación de la descripción (donde te dio el código peli_XXX).")

    text = message.text or message.caption or ""
    match = re.search(r"/season\s*(\d+)", text.lower())
    if not match: return await message.reply("⚠️ Formato incorrecto. Uso: <code>/season 1</code>")

    await create_season(message, target["target_code"], int(match.group(1)))

async def create_season(message: types.Message, content_code: str, season_number: int):
    season_code = f"{content_code}_s{season_number}"
    async with db_pool.acquire() as conn:
        try:
            await conn.execute("INSERT INTO seasons (code, content_code, season_number) VALUES ($1,$2,$3)", season_code, content_code, season_number)
            await conn.execute("UPDATE content SET content_type='series', status='ready' WHERE code=$1", content_code)
        except asyncpg.UniqueViolationError: return await message.reply(f"⚠️ Temporada {season_number} ya existe.")

    confirmation = await message.reply(f"✅ Temporada {season_number} creada.\nCódigo: <code>{season_code}</code>\n\n👉 Reenvía los episodios respondiendo a <b>ESTE</b> mensaje o directamente.")
    await save_confirmation(confirmation.message_id, season_code, "season")
    
    pub_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📢 Publicar ahora en Canal Público", callback_data=PublishCB(code=content_code).pack())]])
    await message.reply("Puedes publicar el post de la serie ahora mismo:", reply_markup=pub_kb)

async def add_episode(message: types.Message, season_code: str):
    file_id = message.video.file_id if message.video else message.document.file_id
    media_type = "video" if message.video else "document"
    ep_title = (message.caption or "").strip()[:200] or None

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COALESCE(MAX(episode_number),0)+1 AS n FROM episodes WHERE season_code=$1", season_code)
        num = row["n"]
        await conn.execute("INSERT INTO episodes (season_code,episode_number,file_id,media_type,title) VALUES ($1,$2,$3,$4,$5)", season_code, num, file_id, media_type, ep_title or f"Episodio {num}")
    await message.reply(f"✅ Episodio {num} añadido a <code>{season_code}</code>.")

async def handle_manual_link(message: types.Message):
    text = (message.text or message.caption or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2: return await message.reply("Uso: <code>/link codigo</code> (ej: /link peli_746)")
    code = parts[1].strip()

    if message.video or message.document: target_msg = message
    elif message.reply_to_message and (message.reply_to_message.video or message.reply_to_message.document): target_msg = message.reply_to_message
    else: return await message.reply("⚠️ Debes enviar el video con este texto, o <b>responder a un video</b> con <code>/link codigo</code>.")

    async with db_pool.acquire() as conn:
        if await conn.fetchrow("SELECT code FROM seasons WHERE code=$1", code): return await add_episode(target_msg, code)
        if await conn.fetchrow("SELECT code FROM content WHERE code=$1", code): return await add_movie(target_msg, code)
    await message.reply(f"❌ Código <code>{code}</code> no encontrado.")

# ================= PUBLICACIÓN Y BORRADO =================
@dp.callback_query(PublishCB.filter(), IsAdmin())
async def on_publish(query: types.CallbackQuery, callback_data: PublishCB):
    if PUBLIC_CHANNEL_ID == 0: return await query.answer("⚠️ Falta PUBLIC_CHANNEL_ID.", show_alert=True)
    await query.answer("Publicando...")
    
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE content SET status='ready' WHERE code=$1", callback_data.code)
        
    if await send_formatted_post(PUBLIC_CHANNEL_ID, callback_data.code): await query.message.edit_text(f"✅ <b>¡Publicado!</b>")
    else: await query.message.edit_text("❌ Error al publicar. Revisa permisos del bot.")

@dp.message(Command("publicar"), IsAdmin())
async def cmd_publicar(message: types.Message, command: CommandObject):
    if not command.args: return await message.answer("Uso: <code>/publicar codigo</code>")
    if PUBLIC_CHANNEL_ID == 0: return await message.answer("⚠️ Falta PUBLIC_CHANNEL_ID.")
    status = await message.answer("⏳ Publicando...")
    async with db_pool.acquire() as conn: await conn.execute("UPDATE content SET status='ready' WHERE code=$1", command.args.strip())
    if await send_formatted_post(PUBLIC_CHANNEL_ID, command.args.strip()): await status.edit_text("✅ Publicado con éxito.")
    else: await status.edit_text("❌ Error al publicar.")

@dp.message(Command("borrar"), IsAdmin())
async def cmd_borrar(message: types.Message, command: CommandObject):
    if not command.args: return await message.answer("🗑 Uso: <code>/borrar codigo</code>")
    code = command.args.strip()
    async with db_pool.acquire() as conn:
        content = await conn.fetchrow("SELECT code, public_message_id FROM content WHERE code=$1", code)
    if not content: return await message.answer("❌ Código no encontrado.")
    
    status_msg = await message.answer(f"🗑 Eliminando <code>{code}</code>...")
    if content["public_message_id"] and PUBLIC_CHANNEL_ID != 0:
        try: await bot.delete_message(chat_id=PUBLIC_CHANNEL_ID, message_id=content["public_message_id"])
        except Exception as e: logging.warning(f"Error borrar post: {e}")
    
    async with db_pool.acquire() as conn: await conn.execute("DELETE FROM content WHERE code=$1", code)
    await status_msg.edit_text(f"✅ <b>Contenido eliminado.</b>\nCódigo: <code>{code}</code>")

@dp.message(Command("borrar_temporada"), IsAdmin())
async def cmd_borrar_temporada(message: types.Message, command: CommandObject):
    if not command.args: return await message.answer("🗑 Uso: <code>/borrar_temporada code_sX</code>")
    season_code = command.args.strip()
    
    async with db_pool.acquire() as conn:
        season = await conn.fetchrow("SELECT content_code FROM seasons WHERE code=$1", season_code)
        if not season: return await message.answer(f"❌ No se encontró <code>{season_code}</code>.")
        
        content_code = season["content_code"]
        status_msg = await message.answer(f"🗑 Eliminando temporada <code>{season_code}</code>...")
        
        await conn.execute("DELETE FROM seasons WHERE code=$1", season_code)
        
        remaining = await conn.fetchval("SELECT COUNT(*) FROM seasons WHERE content_code=$1", content_code)
        if remaining == 0: await conn.execute("UPDATE content SET content_type='movie' WHERE code=$1", content_code)
            
    await status_msg.edit_text(f"✅ <b>Temporada eliminada.</b>\nCódigo: <code>{season_code}</code>")

@dp.message(Command("borrar_episodio"), IsAdmin())
async def cmd_borrar_episodio(message: types.Message, command: CommandObject):
    if not command.args: return await message.answer("🗑 Uso: <code>/borrar_episodio code_sX numero</code>")
    try:
        season_code, ep_num_str = command.args.strip().rsplit(" ", 1)
        ep_num = int(ep_num_str)
    except ValueError:
        return await message.answer("❌ Formato incorrecto.")

    async with db_pool.acquire() as conn:
        exists = await conn.fetchrow("SELECT id FROM episodes WHERE season_code=$1 AND episode_number=$2", season_code, ep_num)
        if not exists: return await message.answer(f"❌ No se encontró el episodio {ep_num} en <code>{season_code}</code>.")
        async with conn.transaction():
            await conn.execute("DELETE FROM episodes WHERE season_code=$1 AND episode_number=$2", season_code, ep_num)
            await conn.execute("UPDATE episodes SET episode_number = episode_number - 1 WHERE season_code=$1 AND episode_number > $2", season_code, ep_num)
    await message.answer(f"✅ <b>Episodio {ep_num} eliminado de <code>{season_code}</code>.</b>\nEpisodios reordenados.")

# ================= ENTREGA AL USUARIO =================
async def deliver_description(chat_id: int, content):
    if content["description_photo_id"]: await bot.send_photo(chat_id, photo=content["description_photo_id"], caption=content["description_text"] or None)
    elif content["description_text"]: await bot.send_message(chat_id, text=content["description_text"])

async def deliver_by_code(chat_id: int, code: str):
    async with db_pool.acquire() as conn:
        season = await conn.fetchrow("SELECT * FROM seasons WHERE code=$1", code)
        if season:
            content = await conn.fetchrow("SELECT * FROM content WHERE code=$1", season["content_code"])
            if content: await deliver_description(chat_id, content)
            return await deliver_episodes(chat_id, code)

        content = await conn.fetchrow("SELECT * FROM content WHERE code=$1", code)
        if not content: return await bot.send_message(chat_id, "❌ Contenido no encontrado.")
        
        if content["content_type"] == "movie" and not content["file_id"]:
            return await bot.send_message(chat_id, "⚠️ Este contenido aún no está disponible.")

        fav_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⭐ Guardar en favoritos", callback_data=FavToggleCB(code=code).pack())]])
        await deliver_description(chat_id, content)

        aviso = await bot.send_message(chat_id, "⚠️ <b>Aviso:</b> El archivo se eliminará de este chat en 10 minutos por seguridad. Descárgalo antes.")
        asyncio.create_task(delete_later(chat_id, aviso.message_id, 600))

        if content["content_type"] == "movie":
            if content["media_type"] == "video": msg = await bot.send_video(chat_id, video=content["file_id"], reply_markup=fav_kb)
            else: msg = await bot.send_document(chat_id, document=content["file_id"], reply_markup=fav_kb)
            asyncio.create_task(delete_later(chat_id, msg.message_id, 600))
        else:
            await bot.send_message(chat_id, "📺 Temporadas disponibles:", reply_markup=fav_kb)
            seasons = await conn.fetch("SELECT code, season_number FROM seasons WHERE content_code=$1 ORDER BY season_number", code)
            if not seasons: return await bot.send_message(chat_id, "⚠️ Esta serie no tiene temporadas aún.")
            buttons = [[InlineKeyboardButton(text=f"🎬 Temporada {s['season_number']}", callback_data=SeasonCB(code=s["code"]).pack())] for s in seasons]
            await bot.send_message(chat_id, "Selecciona:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

async def deliver_episodes(chat_id: int, season_code: str):
    async with db_pool.acquire() as conn:
        eps = await conn.fetch("SELECT episode_number, file_id, media_type, title FROM episodes WHERE season_code=$1 ORDER BY episode_number", season_code)
    if not eps: return await bot.send_message(chat_id, "⚠️ Esta temporada no tiene episodios.")
    
    await bot.send_message(chat_id, f"📺 Enviando {len(eps)} episodio(s)...")
    aviso = await bot.send_message(chat_id, "⚠️ <b>Aviso:</b> Los archivos se eliminarán de este chat en 10 minutos por seguridad. Descárgalos antes.")
    asyncio.create_task(delete_later(chat_id, aviso.message_id, 600))

    for ep in eps:
        try:
            cap = f"<b>{ep['title']}</b>"
            if ep["media_type"] == "video": msg = await bot.send_video(chat_id, video=ep["file_id"], caption=cap)
            else: msg = await bot.send_document(chat_id, document=ep["file_id"], caption=cap)
            asyncio.create_task(delete_later(chat_id, msg.message_id, 600))
            await asyncio.sleep(0.15)
        except Exception as e: logging.error(f"Error ep {ep['episode_number']}: {e}")

# ================= START / BÚSQUEDA =================
@dp.message(CommandStart(deep_link=True))
async def start_deep(message: types.Message, command: CommandObject):
    await register_user(message.from_user)
    if not await check_sub(message.from_user.id): return await send_join_message(message, command.args)
    if command.args: await deliver_by_code(message.chat.id, command.args)

@dp.message(CommandStart())
async def start(message: types.Message):
    await register_user(message.from_user)
    if not await check_sub(message.from_user.id): return await send_join_message(message)
    
    async with db_pool.acquire() as conn: row = await conn.fetchrow("SELECT value FROM settings WHERE key='welcome_message'")
    if row and row["value"]: await message.answer(row["value"])
    else: await message.answer("👋 ¡Hola!\n\n🎬 Pulsa los botones del canal.\n🔍 Escribe el título o usa /buscar.\n⭐ /favoritos")

async def build_search(query: str):
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT code, title FROM content WHERE status='ready' AND (title ILIKE $1 OR tags ILIKE $1) LIMIT 20", f"%{query}%")

async def send_results(message: types.Message, query: str):
    if not await check_sub(message.from_user.id): return await send_join_message(message)
    rows = await build_search(query)
    if not rows: return await message.answer(f"🔍 Sin resultados para «{query}».")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"🎬 {r['title'][:50]}", callback_data=SearchCB(code=r["code"]).pack())] for r in rows])
    await message.answer(f"🔍 {len(rows)} resultado(s):", reply_markup=kb)

@dp.message(Command("buscar"))
async def buscar(message: types.Message, command: CommandObject):
    await register_user(message.from_user)
    if not command.args: 
        if not await check_sub(message.from_user.id): return await send_join_message(message)
        return await message.answer("🔍 Uso: <code>/buscar nombre</code>")
    await send_results(message, command.args)

@dp.callback_query(SearchCB.filter())
async def on_search(query: types.CallbackQuery, callback_data: SearchCB):
    if not await check_sub(query.from_user.id): return await send_join_message(query, callback_data.code)
    await query.answer()
    await deliver_by_code(query.from_user.id, callback_data.code)

@dp.callback_query(SeasonCB.filter())
async def on_season(query: types.CallbackQuery, callback_data: SeasonCB):
    if not await check_sub(query.from_user.id): return await send_join_message(query, callback_data.code)
    await query.answer()
    await deliver_by_code(query.from_user.id, callback_data.code)

@dp.inline_query()
async def inline_search(query: types.InlineQuery):
    text = query.query.strip()
    if not text: return await query.answer([], cache_time=1)
    rows = await build_search(text)
    results = []
    for r in rows:
        link = await create_start_link(bot, payload=r["code"], encode=False)
        results.append(InlineQueryResultArticle(id=r["code"], title=r["title"], description="Pulsa para ver", input_message_content=InputTextMessageContent(message_text=f"🎬 <b>{r['title']}</b>\n\n👉 <a href='{link}'>Ver contenido</a>")))
    await query.answer(results, cache_time=10)

# ================= FAVORITOS =================
@dp.callback_query(FavToggleCB.filter())
async def toggle_fav(query: types.CallbackQuery, callback_data: FavToggleCB):
    if not await check_sub(query.from_user.id): return await send_join_message(query, callback_data.code)
    uid, code = query.from_user.id, callback_data.code
    async with db_pool.acquire() as conn:
        exists = await conn.fetchrow("SELECT 1 FROM favorites WHERE user_id=$1 AND code=$2", uid, code)
        if exists:
            await conn.execute("DELETE FROM favorites WHERE user_id=$1 AND code=$2", uid, code)
            msg = "🗑 Eliminado."
        else:
            await conn.execute("INSERT INTO favorites (user_id,code) VALUES ($1,$2) ON CONFLICT DO NOTHING", uid, code)
            msg = "⭐ Añadido."
    await query.answer(msg)

@dp.message(Command("favoritos"))
async def favs(message: types.Message):
    if not await check_sub(message.from_user.id): return await send_join_message(message)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT m.code, m.title FROM favorites f JOIN content m ON m.code=f.code WHERE f.user_id=$1 AND m.status='ready'", message.from_user.id)
    if not rows: return await message.answer("⭐ Sin favoritos.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"🎬 {r['title'][:50]}", callback_data=SearchCB(code=r["code"]).pack())] for r in rows])
    await message.answer(f"⭐ Favoritos ({len(rows)}):", reply_markup=kb)

# ================= ADMIN — PANEL =================
ADMIN_SECTIONS = {
    "guia": (
        "📝 <b>CÓMO PUBLICAR CONTENIDO</b>\n\n"
        "<b>🪄 CON TMDB (Automático)</b>\nUsa el botón de abajo o el comando <code>/tmdb</code>. Buscará el póster y la sinopsis, y te dará un botón para enviarlo al almacenamiento.\n\n"
        "<b>🎬 Película en 2 pasos (Manual)</b>\n1. Reenvía la descripción → obtienes código.\n2. Reenvía el video respondiendo al bot.\n\n"
        "<b>📺 Serie con temporadas</b>\n1. Reenvía la descripción → obtienes código.\n2. Responde con <code>/season 1</code>.\n3. Reenvía los episodios respondiendo a ese mensaje."
    ),
    "tmdb_info": (
        "🪄 <b>GENERADOR TMDB</b>\n\n"
        "Esta herramienta busca automáticamente en TMDB el póster, sinopsis, año y géneros de una peli, serie o anime.\n\n"
        "Al pulsar el botón de abajo, se te pedirá que escribas el tipo y el nombre.\n"
        "<b>Formatos válidos:</b>\n"
        "• <code>peli Inception</code>\n"
        "• <code>serie Breaking Bad</code>\n"
        "• <code>anime One Piece</code>"
    ),
    "pelis": (
        "🎬 <b>COMANDOS DE PELÍCULAS</b>\n\n"
        "<b>/link codigo</b>\nResponde a un video para vincularlo a una película ya creada.\n<i>Ejemplo:</i> responde al video con <code>/link peli_123</code>\n\n"
        "📢 <b>Publicar:</b> Al vincular el video, el bot te dará un botón para publicarlo en tu canal público."
    ),
    "series": (
        "📺 <b>COMANDOS DE SERIES</b>\n\n"
        "<b>/season N</b>\nRespondiendo al mensaje del bot, crea una temporada.\n<i>Ejemplo:</i> <code>/season 1</code>.\n\n"
        "📢 <b>Publicar:</b> Puedes publicar en el canal público en cuanto crees la Temporada 1, o usar <code>/publicar peli_XXX</code> más tarde."
    ),
    "enlaces": (
        "🔗 <b>ENLACES Y BOTONES</b>\n\n"
        "El bot genera los enlaces automáticamente al publicar. Si necesitas un enlace manual, usa <code>/post codigo</code> para que el bot te lo muestre."
    ),
    "borrar": (
        "🗑 <b>ELIMINAR CONTENIDO</b>\n\n"
        "<b>/borrar codigo</b>\nElimina una peli/serie completa y su post del canal público.\n\n"
        "<b>/borrar_temporada code_sX</b>\nElimina una temporada y sus capítulos.\n\n"
        "<b>/borrar_episodio code_sX num</b>\nElimina un episodio y reordena el resto."
    ),
    "bienvenida": (
        "👋 <b>MENSAJE DE BIENVENIDA</b>\n\nConfigura el mensaje de <code>/start</code>. Pulsa en el botón para editarlo."
    ),
    "sticker": (
        "🎭 <b>STICKER DE PUBLICACIÓN</b>\n\nConfigura un sticker para enviar tras publicar. Pulsa en el botón para enviar uno nuevo."
    ),
    "post": (
        "📢 <b>ENVIAR POST / RESTAURAR CANAL</b>\n\n"
        "<b>/publicar codigo</b>\nVuelve a publicar el post en el canal.\n\n"
        "<b>/restaurar ID_CANAL</b>\nEnvía TODOS los posts a un nuevo canal si el principal es baneado.\n<i>Ejemplo:</i> <code>/restaurar -1001234567890</code>"
    ),
    "msgs": (
        "📣 <b>NOTIFICACIONES Y AVISOS</b>\n\n"
        "<b>/notify codigo</b>\nAvisa del estreno a los usuarios.\n"
        "<b>/broadcast</b>\nResponde a un mensaje con esto para enviarlo a todos."
    ),
    "stats": (
        "📊 <b>ESTADÍSTICAS</b>\n\n"
        "<b>/stats</b>\nMuestra usuarios, pelis, series y episodios.\n"
        "<b>/lista</b>\nMuestra todo el catálogo."
    ),
    "otros": (
        "⚙️ <b>OTROS COMANDOS</b>\n\n"
        "<b>/link codigo</b>\nVincula un video a una peli ya creada.\n"
        "<b>/admin</b>\nAbre este panel.\n\n"
        "🔒 <b>Seguridad</b>: Solo tú puedes usar estos comandos."
    ),
}

def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Cómo publicar", callback_data=AdminCB(section="guia").pack())],
        [InlineKeyboardButton(text="🪄 Usar TMDB", callback_data=AdminCB(section="tmdb_info").pack())],
        [InlineKeyboardButton(text="🎬 Películas", callback_data=AdminCB(section="pelis").pack()), InlineKeyboardButton(text="📺 Series", callback_data=AdminCB(section="series").pack())],
        [InlineKeyboardButton(text="🔗 Enlaces", callback_data=AdminCB(section="enlaces").pack()), InlineKeyboardButton(text="📢 Post/Restaurar", callback_data=AdminCB(section="post").pack())],
        [InlineKeyboardButton(text="🗑 Borrar Contenido", callback_data=AdminCB(section="borrar").pack()), InlineKeyboardButton(text="👋 Bienvenida", callback_data=AdminCB(section="bienvenida").pack())],
        [InlineKeyboardButton(text="🎭 Sticker Post", callback_data=AdminCB(section="sticker").pack())],
        [InlineKeyboardButton(text="📣 Mensajes", callback_data=AdminCB(section="msgs").pack()), InlineKeyboardButton(text="📊 Stats", callback_data=AdminCB(section="stats").pack())],
        [InlineKeyboardButton(text="⚙️ Otros", callback_data=AdminCB(section="otros").pack())],
        [InlineKeyboardButton(text="❌ Cerrar panel", callback_data=AdminCB(section="close").pack())],
    ])

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Volver al panel", callback_data=AdminCB(section="menu").pack())],[InlineKeyboardButton(text="❌ Cerrar", callback_data=AdminCB(section="close").pack())]])

def welcome_edit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Escribir nuevo mensaje", callback_data=AdminCB(section="edit_welcome").pack())],[InlineKeyboardButton(text="◀️ Volver al panel", callback_data=AdminCB(section="menu").pack())]])

def sticker_edit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎭 Enviar nuevo sticker", callback_data=AdminCB(section="edit_sticker").pack())],[InlineKeyboardButton(text="◀️ Volver al panel", callback_data=AdminCB(section="menu").pack())]])

def tmdb_edit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔍 Buscar en TMDB", callback_data=AdminCB(section="edit_tmdb").pack())],[InlineKeyboardButton(text="◀️ Volver al panel", callback_data=AdminCB(section="menu").pack())]])

@dp.message(Command("admin"), IsAdmin())
async def admin_panel(message: types.Message):
    await message.answer("🛠 <b>PANEL DE ADMINISTRADOR</b>\n\nToca una categoría para ver los comandos y su explicación:", reply_markup=admin_panel_kb())

@dp.callback_query(AdminCB.filter(), IsAdmin())
async def admin_callback(query: types.CallbackQuery, callback_data: AdminCB, state: FSMContext):
    section = callback_data.section
    if section == "close":
        await query.answer()
        try: await query.message.delete()
        except: pass
        return
    if section == "menu":
        await query.answer()
        return await query.message.edit_text("🛠 <b>PANEL DE ADMINISTRADOR</b>\n\nToca una categoría para ver los comandos y su explicación:", reply_markup=admin_panel_kb())

    if section == "edit_welcome":
        await query.answer()
        await state.set_state(AdminStates.waiting_for_welcome)
        return await query.message.edit_text("✏️ <b>Envía el nuevo mensaje de bienvenida</b>\n\nEnvíalo en el siguiente mensaje.\n\nPara cancelar, envía /cancelar", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancelar", callback_data=AdminCB(section="menu").pack())]]))

    if section == "edit_sticker":
        await query.answer()
        await state.set_state(AdminStates.waiting_for_sticker)
        return await query.message.edit_text("🎭 <b>Envía el sticker que quieres usar</b>\n\nEnvíalo en el siguiente mensaje.\n\nPara cancelar, envía /cancelar", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancelar", callback_data=AdminCB(section="menu").pack())]]))

    if section == "edit_tmdb":
        await query.answer()
        await state.set_state(AdminStates.waiting_for_tmdb)
        return await query.message.edit_text("🪄 <b>¿Qué deseas buscar en TMDB?</b>\n\nEscribe el tipo y el nombre. Ejemplos:\n• <code>peli Inception</code>\n• <code>serie Breaking Bad</code>\n\nPara cancelar, envía /cancelar", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancelar", callback_data=AdminCB(section="menu").pack())]]))

    text = ADMIN_SECTIONS.get(section)
    if not text: return await query.answer("Sección no encontrada.", show_alert=True)
    await query.answer()
    if section == "bienvenida": await query.message.edit_text(text, reply_markup=welcome_edit_kb())
    elif section == "sticker": await query.message.edit_text(text, reply_markup=sticker_edit_kb())
    elif section == "tmdb_info": await query.message.edit_text(text, reply_markup=tmdb_edit_kb())
    else: await query.message.edit_text(text, reply_markup=back_kb())

# ================= RECIBIR NUEVA BIENVENIDA O STICKER =================
@dp.message(AdminStates.waiting_for_welcome, IsAdmin())
async def process_new_welcome(message: types.Message, state: FSMContext):
    if message.text == "/cancelar":
        await state.clear()
        return await message.answer("❌ Edición cancelada.", reply_markup=admin_panel_kb())
    new_text = message.html_text 
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO settings (key, value) VALUES ('welcome_message', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", new_text)
    await state.clear()
    await message.answer("✅ <b>Mensaje de bienvenida actualizado.</b>\n\nAsí lo verán los usuarios:", reply_markup=admin_panel_kb())
    await message.answer(new_text)

@dp.message(AdminStates.waiting_for_sticker, IsAdmin())
async def process_new_sticker(message: types.Message, state: FSMContext):
    if message.text == "/cancelar":
        await state.clear()
        return await message.answer("❌ Edición cancelada.", reply_markup=admin_panel_kb())
    if not message.sticker: return await message.answer("⚠️ Eso no es un sticker. Por favor, envía un sticker o escribe /cancelar.")
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO settings (key, value) VALUES ('post_sticker', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", message.sticker.file_id)
    await state.clear()
    await message.answer("✅ <b>Sticker guardado.</b>\n\nVista previa:", reply_markup=admin_panel_kb())
    await message.answer_sticker(message.sticker.file_id)

@dp.message(F.text & ~F.text.startswith("/") & (F.chat.type == ChatType.PRIVATE))
async def text_search(message: types.Message):
    await register_user(message.from_user)
    await send_results(message, message.text)

# ================= RESTAURACIÓN Y OTROS COMANDOS ADMIN =================
@dp.message(Command("restaurar"), IsAdmin())
async def cmd_restaurar(message: types.Message, command: CommandObject):
    if not command.args: return await message.answer("🚨 Uso: <code>/restaurar -1001234567890</code>")
    try: new_channel_id = int(command.args.strip())
    except ValueError: return await message.answer("❌ El ID del canal debe ser un número.")

    status_msg = await message.answer("⏳ Iniciando la restauración...")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT code FROM content WHERE status='ready' ORDER BY created_at")
    if not rows: return await status_msg.edit_text("❌ No hay contenido para restaurar.")

    enviados = fallidos = 0
    for row in rows:
        if await send_formatted_post(new_channel_id, row["code"]):
            enviados += 1
            await asyncio.sleep(0.5)
        else: fallidos += 1
    await status_msg.edit_text(f"✅ Restauración completada.\n\nPublicados: {enviados}\nFallidos: {fallidos}")

@dp.message(Command("notify"), IsAdmin())
async def notify(message: types.Message, command: CommandObject):
    if not command.args: return await message.answer("Uso: <code>/notify codigo</code>")
    code = command.args.strip()
    async with db_pool.acquire() as conn:
        if await conn.fetchrow("SELECT 1 FROM seasons WHERE code=$1", code): title = f"Temporada ({code})"
        else:
            row = await conn.fetchrow("SELECT title FROM content WHERE code=$1 AND status='ready'", code)
            if not row: return await message.answer("❌ Código no encontrado.")
            title = row["title"]
        users = [r["user_id"] for r in await conn.fetch("SELECT user_id FROM users")]

    link = await create_start_link(bot, payload=code, encode=False)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎬 Ver ahora", url=link)]])
    status = await message.answer(f"📤 Notificando a {len(users)} usuarios...")
    sent = failed = 0
    for uid in users:
        try:
            await bot.send_message(uid, f"🎬 <b>Nuevo estreno:</b>\n\n{title}", reply_markup=kb)
            sent += 1
            await asyncio.sleep(0.05)
        except: failed += 1
    await status.edit_text(f"✅ Enviados: {sent} | ❌ Fallidos: {failed}")

@dp.message(Command("broadcast"), IsAdmin())
async def broadcast(message: types.Message):
    if not message.reply_to_message: return await message.answer("Responde a un mensaje con /broadcast")
    async with db_pool.acquire() as conn:
        users = [r["user_id"] for r in await conn.fetch("SELECT user_id FROM users")]
    status = await message.answer(f"📤 Enviando a {len(users)} usuarios...")
    sent = failed = 0
    for uid in users:
        try:
            await message.reply_to_message.copy_to(chat_id=uid)
            sent += 1
            await asyncio.sleep(0.05)
        except: failed += 1
    await status.edit_text(f"✅ Enviados: {sent} | ❌ Fallidos: {failed}")

@dp.message(Command("stats"), IsAdmin())
async def stats(message: types.Message):
    async with db_pool.acquire() as conn:
        u = (await conn.fetchrow("SELECT COUNT(*) AS c FROM users"))["c"]
        m = (await conn.fetchrow("SELECT COUNT(*) AS c FROM content WHERE status='ready' AND content_type='movie'"))["c"]
        s = (await conn.fetchrow("SELECT COUNT(*) AS c FROM content WHERE content_type='series'"))["c"]
        e = (await conn.fetchrow("SELECT COUNT(*) AS c FROM episodes"))["c"]
        p = (await conn.fetchrow("SELECT COUNT(*) AS c FROM content WHERE status='pending'"))["c"]
    await message.answer(f"📊 <b>Estadísticas</b>\n\n👥 Usuarios: {u}\n🎬 Películas: {m}\n📺 Series: {s}\n🎞 Episodios: {e}\n⏳ Pendientes: {p}")

@dp.message(Command("lista"), IsAdmin())
async def lista(message: types.Message):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT code, title, content_type, status FROM content ORDER BY code")
        if not rows: return await message.answer("Sin contenido.")
        lines = []
        for r in rows:
            icon = "✅" if r["status"] == "ready" else "❌"
            kind = "📺" if r["content_type"] == "series" else "🎬"
            lines.append(f"{icon}{kind} <code>{r['code']}</code> — {r['title'][:40]}")
            if r["content_type"] == "series":
                ss = await conn.fetch("SELECT code FROM seasons WHERE content_code=$1 ORDER BY season_number", r["code"])
                for s in ss:
                    cnt = (await conn.fetchrow("SELECT COUNT(*) AS c FROM episodes WHERE season_code=$1", s["code"]))["c"]
                    lines.append(f"   └─ <code>{s['code']}</code> ({cnt} eps)")
    await message.answer("📚 <b>Contenido</b>\n\n" + "\n".join(lines))

# ================= TAREAS DE FONDO =================
async def log_memory():
    while True:
        await asyncio.sleep(3600)
        try:
            peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            logging.info(f"📊 RAM pico: {peak_mb:.0f} MB")
        except Exception: pass

async def validate_file_ids():
    while True:
        await asyncio.sleep(6 * 3600)
        logging.info("🔍 Validando file_ids...")
        
        invalid_items = []
        
        async with db_pool.acquire() as conn:
            for row in await conn.fetch("SELECT code, file_id FROM content WHERE file_id IS NOT NULL"):
                try: 
                    await bot.get_file(row["file_id"])
                    await asyncio.sleep(0.3)
                except TelegramRetryAfter as e: await asyncio.sleep(e.retry_after)
                except TelegramBadRequest:
                    invalid_items.append(f"🎬 <code>{row['code']}</code>")
                except Exception: pass
            
            for row in await conn.fetch("SELECT id, season_code, episode_number, file_id FROM episodes"):
                try: 
                    await bot.get_file(row["file_id"])
                    await asyncio.sleep(0.3)
                except TelegramRetryAfter as e: await asyncio.sleep(e.retry_after)
                except TelegramBadRequest:
                    invalid_items.append(f"🎞 <code>{row['season_code']}</code> - Ep. {row['episode_number']}")
                except Exception: pass

        if invalid_items:
            msg = "⚠️ <b>Se detectaron archivos inválidos/caducados:</b>\n\n" + "\n".join(invalid_items[:30])
            if len(invalid_items) > 30: msg += f"\n... y {len(invalid_items)-30} más."
            msg += "\n\n<i>Usa /borrar o /borrar_episodio si quieres eliminarlos.</i>"
            try: await bot.send_message(ADMIN_ID, msg)
            except: pass
            
        logging.info("✅ Validación completa.")

# ================= ARRANQUE =================
async def main():
    await init_db()
    try:
        asyncio.create_task(validate_file_ids())
        asyncio.create_task(log_memory())
        logging.info("🤖 Bot iniciado.")
        await dp.start_polling(bot, handle_as_tasks=False, limit=10)
    finally:
        await db_pool.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(main())
