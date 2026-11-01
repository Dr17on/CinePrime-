import asyncio
import logging
import os
import time

import asyncpg
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command, CommandObject, CallbackData, BaseFilter
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    InlineQueryResultArticle, InputTextMessageContent,
)
from aiogram.utils.deep_linking import create_start_link
from aiogram.enums import ChatType
from aiogram.client.default import DefaultBotProperties

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", "0"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not DATABASE_URL:
    raise SystemExit("❌ Faltan BOT_TOKEN o DATABASE_URL")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
db_pool: asyncpg.Pool = None

# ================= FILTROS =================
class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id == ADMIN_ID

# Cache temporal para deduplicar media groups
_media_groups: dict[str, float] = {}

def _is_first_in_group(group_id: str | None) -> bool:
    if not group_id:
        return True
    now = time.time()
    for k in [k for k, v in _media_groups.items() if now - v > 300]:
        del _media_groups[k]
    if group_id in _media_groups:
        return False
    _media_groups[group_id] = now
    return True

# ================= CALLBACKS =================
class SearchCB(CallbackData, prefix="srch"): code: str
class FavToggleCB(CallbackData, prefix="fav"): code: str
class SeasonCB(CallbackData, prefix="season"): code: str
class AdminCB(CallbackData, prefix="adm"): section: str

# ================= DB =================
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS content (
                code TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description_text TEXT,
                description_photo_id TEXT,
                tags TEXT DEFAULT '',
                content_type TEXT DEFAULT 'movie',
                file_id TEXT,
                media_type TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS seasons (
                code TEXT PRIMARY KEY,
                content_code TEXT NOT NULL REFERENCES content(code) ON DELETE CASCADE,
                season_number INTEGER NOT NULL,
                UNIQUE(content_code, season_number)
            );
            CREATE TABLE IF NOT EXISTS episodes (
                id SERIAL PRIMARY KEY,
                season_code TEXT NOT NULL REFERENCES seasons(code) ON DELETE CASCADE,
                episode_number INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                media_type TEXT NOT NULL,
                title TEXT,
                UNIQUE(season_code, episode_number)
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS favorites (
                user_id BIGINT,
                code TEXT,
                added_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (user_id, code)
            );
            CREATE TABLE IF NOT EXISTS message_map (
                bot_message_id BIGINT PRIMARY KEY,
                target_code TEXT NOT NULL,
                target_type TEXT NOT NULL,
                used BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            ALTER TABLE message_map ADD COLUMN IF NOT EXISTS used BOOLEAN DEFAULT FALSE;
            ALTER TABLE message_map ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
        """)

async def register_user(user: types.User):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, username, first_name) VALUES ($1,$2,$3) "
            "ON CONFLICT (user_id) DO NOTHING",
            user.id, user.username, user.first_name,
        )

# ================= HELPERS DE LINKING =================
async def get_target_by_msg_id(bot_msg_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT target_code, target_type FROM message_map WHERE bot_message_id=$1",
            bot_msg_id,
        )

async def get_recent_pending():
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT target_code, target_type FROM message_map
            WHERE used = FALSE AND created_at > NOW() - INTERVAL '10 minutes'
            ORDER BY created_at DESC LIMIT 1
        """)

async def mark_used(target_code: str, target_type: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE message_map SET used=TRUE WHERE target_code=$1 AND target_type=$2",
            target_code, target_type,
        )

async def save_confirmation(bot_msg_id: int, target_code: str, target_type: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO message_map (bot_message_id, target_code, target_type) VALUES ($1,$2,$3)",
            bot_msg_id, target_code, target_type,
        )

# ================= CANAL PRIVADO =================
@dp.channel_post(F.chat.id == STORAGE_CHANNEL_ID)
async def handle_storage(message: types.Message):
    if message.video or message.document:
        return await handle_content(message)
    if message.text or message.photo:
        if not _is_first_in_group(message.media_group_id):
            return
        return await handle_description(message)

# ---------- DESCRIPCIÓN ----------
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
                 title=EXCLUDED.title,
                 description_text=EXCLUDED.description_text,
                 description_photo_id=EXCLUDED.description_photo_id,
                 tags=EXCLUDED.tags""",
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

# ---------- CONTENIDO ----------
async def handle_content(message: types.Message):
    text = message.caption or ""

    if text.startswith("/link"):
        return await handle_manual_link(message)
    if text.startswith("/season"):
        return await handle_season_command(message)

    target = None
    if message.reply_to_message:
        target = await get_target_by_msg_id(message.reply_to_message.message_id)
    if not target:
        target = await get_recent_pending()

    if not target:
        if text.strip() and len(text) > 50:
            return await create_standalone_movie(message)
        return await message.reply(
            "⚠️ No hay descripción pendiente para vincular.\n\n"
            "Opciones:\n"
            "• Reenvía primero el post con la descripción\n"
            "• O responde al video con <code>/link codigo</code>"
        )

    if target["target_type"] == "season":
        await add_episode(message, target["target_code"])
    else:
        await add_movie(message, target["target_code"])
        await mark_used(target["target_code"], "description")

async def add_movie(message: types.Message, code: str):
    file_id = message.video.file_id if message.video else message.document.file_id
    media_type = "video" if message.video else "document"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE content SET file_id=$1, media_type=$2, content_type='movie', status='ready' WHERE code=$3",
            file_id, media_type, code,
        )
    link = await create_start_link(bot, payload=code, encode=False)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎬 Ver Película", url=link)]])
    await message.reply(f"✅ Película vinculada.\nCódigo: <code>{code}</code>", reply_markup=kb)

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

    link = await create_start_link(bot, payload=code, encode=False)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎬 Ver Película", url=link)]])
    await message.reply(f"✅ Post combinado guardado.\nCódigo: <code>{code}</code>", reply_markup=kb)

async def handle_season_command(message: types.Message):
    if not message.reply_to_message:
        return await message.reply("⚠️ Responde a la descripción con <code>/season N</code>")
    target = await get_target_by_msg_id(message.reply_to_message.message_id)
    if not target or target["target_type"] != "description":
        return await message.reply("⚠️ Debes responder al mensaje de confirmación de la descripción.")

    text = message.text or message.caption or ""
    parts = text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.reply("⚠️ Uso: <code>/season N</code> (ej: /season 1)")

    await create_season(message, target["target_code"], int(parts[1]))

async def create_season(message: types.Message, content_code: str, season_number: int):
    season_code = f"{content_code}_s{season_number}"
    async with db_pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO seasons (code, content_code, season_number) VALUES ($1,$2,$3)",
                season_code, content_code, season_number,
            )
            await conn.execute("UPDATE content SET content_type='series' WHERE code=$1", content_code)
        except asyncpg.UniqueViolationError:
            return await message.reply(f"⚠️ Temporada {season_number} ya existe.")

    confirmation = await message.reply(
        f"✅ Temporada {season_number} creada.\nCódigo: <code>{season_code}</code>\n\n"
        f"👉 Reenvía los episodios:\n"
        f"• Como respuesta a <b>ESTE</b> mensaje, o\n"
        f"• Directamente (se vincularán solos)"
    )
    await save_confirmation(confirmation.message_id, season_code, "season")

    link = await create_start_link(bot, payload=season_code, encode=False)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"🎬 Temporada {season_number}", url=link
    )]])
    await message.reply(f"🔗 Botón de la temporada {season_number}:", reply_markup=kb)

async def add_episode(message: types.Message, season_code: str):
    file_id = message.video.file_id if message.video else message.document.file_id
    media_type = "video" if message.video else "document"
    ep_title = (message.caption or "").strip()[:200] or None

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COALESCE(MAX(episode_number),0)+1 AS n FROM episodes WHERE season_code=$1",
            season_code,
        )
        num = row["n"]
        await conn.execute(
            "INSERT INTO episodes (season_code,episode_number,file_id,media_type,title) VALUES ($1,$2,$3,$4,$5)",
            season_code, num, file_id, media_type, ep_title or f"Episodio {num}",
        )
    await message.reply(f"✅ Episodio {num} añadido a <code>{season_code}</code>.")

async def handle_manual_link(message: types.Message):
    text = message.text or message.caption or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Uso: <code>/link codigo</code> (como respuesta a un video)")
    code = parts[1].strip()

    async with db_pool.acquire() as conn:
        season = await conn.fetchrow("SELECT code FROM seasons WHERE code=$1", code)
        if season:
            return await add_episode(message, code)
        content = await conn.fetchrow("SELECT code FROM content WHERE code=$1", code)
        if content:
            return await add_movie(message, code)
    await message.reply(f"❌ Código <code>{code}</code> no encontrado.")

# ================= ENTREGA AL USUARIO =================
async def deliver_description(chat_id: int, content):
    if content["description_photo_id"]:
        await bot.send_photo(chat_id, photo=content["description_photo_id"],
                             caption=content["description_text"] or None)
    elif content["description_text"]:
        await bot.send_message(chat_id, text=content["description_text"])

async def deliver_by_code(chat_id: int, code: str):
    async with db_pool.acquire() as conn:
        season = await conn.fetchrow("SELECT * FROM seasons WHERE code=$1", code)
        if season:
            content = await conn.fetchrow("SELECT * FROM content WHERE code=$1", season["content_code"])
            if content:
                await deliver_description(chat_id, content)
            return await deliver_episodes(chat_id, code)

        content = await conn.fetchrow("SELECT * FROM content WHERE code=$1", code)
        if not content:
            return await bot.send_message(chat_id, "❌ Contenido no encontrado.")
        if content["status"] != "ready":
            return await bot.send_message(chat_id, "⚠️ Este contenido aún no está disponible.")

        fav_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="⭐ Guardar en favoritos", callback_data=FavToggleCB(code=code).pack(),
        )]])
        await deliver_description(chat_id, content)

        if content["content_type"] == "movie":
            if content["media_type"] == "video":
                await bot.send_video(chat_id, video=content["file_id"], reply_markup=fav_kb)
            else:
                await bot.send_document(chat_id, document=content["file_id"], reply_markup=fav_kb)
        else:
            await bot.send_message(chat_id, "📺 Temporadas disponibles:", reply_markup=fav_kb)
            seasons = await conn.fetch(
                "SELECT code, season_number FROM seasons WHERE content_code=$1 ORDER BY season_number", code
            )
            if not seasons:
                return await bot.send_message(chat_id, "⚠️ Esta serie no tiene temporadas aún.")
            buttons = [[InlineKeyboardButton(
                text=f"🎬 Temporada {s['season_number']}",
                callback_data=SeasonCB(code=s["code"]).pack(),
            )] for s in seasons]
            await bot.send_message(chat_id, "Selecciona:",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

async def deliver_episodes(chat_id: int, season_code: str):
    async with db_pool.acquire() as conn:
        eps = await conn.fetch(
            "SELECT episode_number, file_id, media_type, title FROM episodes "
            "WHERE season_code=$1 ORDER BY episode_number",
            season_code,
        )
    if not eps:
        return await bot.send_message(chat_id, "⚠️ Esta temporada no tiene episodios.")
    await bot.send_message(chat_id, f"📺 Enviando {len(eps)} episodio(s)...")
    for ep in eps:
        try:
            cap = f"<b>{ep['title']}</b>"
            if ep["media_type"] == "video":
                await bot.send_video(chat_id, video=ep["file_id"], caption=cap)
            else:
                await bot.send_document(chat_id, document=ep["file_id"], caption=cap)
            await asyncio.sleep(0.15)
        except Exception as e:
            logging.error(f"Error ep {ep['episode_number']}: {e}")
            await bot.send_message(chat_id, f"❌ Error con episodio {ep['episode_number']}")

# ================= START / BÚSQUEDA =================
@dp.message(CommandStart(deep_link=True))
async def start_deep(message: types.Message, command: CommandObject):
    await register_user(message.from_user)
    if command.args:
        await deliver_by_code(message.chat.id, command.args)

@dp.message(CommandStart())
async def start(message: types.Message):
    await register_user(message.from_user)
    await message.answer(
        "👋 ¡Hola!\n\n🎬 Pulsa los botones del canal.\n"
        "🔍 Escribe el título o usa /buscar.\n⭐ /favoritos"
    )

async def build_search(query: str):
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT code, title FROM content WHERE status='ready' "
            "AND (title ILIKE $1 OR tags ILIKE $1) LIMIT 20",
            f"%{query}%",
        )

async def send_results(message: types.Message, query: str):
    rows = await build_search(query)
    if not rows:
        return await message.answer(f"🔍 Sin resultados para «{query}».")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎬 {r['title'][:50]}", callback_data=SearchCB(code=r["code"]).pack())]
        for r in rows
    ])
    await message.answer(f"🔍 {len(rows)} resultado(s):", reply_markup=kb)

@dp.message(Command("buscar"))
async def buscar(message: types.Message, command: CommandObject):
    await register_user(message.from_user)
    if not command.args:
        return await message.answer("🔍 Uso: <code>/buscar nombre</code>")
    await send_results(message, command.args)

@dp.message(F.text & ~F.text.startswith("/") & (F.chat.type == ChatType.PRIVATE))
async def text_search(message: types.Message):
    await register_user(message.from_user)
    await send_results(message, message.text)

@dp.callback_query(SearchCB.filter())
async def on_search(query: types.CallbackQuery, callback_data: SearchCB):
    await query.answer()
    await deliver_by_code(query.from_user.id, callback_data.code)

@dp.callback_query(SeasonCB.filter())
async def on_season(query: types.CallbackQuery, callback_data: SeasonCB):
    await query.answer()
    await deliver_by_code(query.from_user.id, callback_data.code)

@dp.inline_query()
async def inline_search(query: types.InlineQuery):
    text = query.query.strip()
    if not text:
        return await query.answer([], cache_time=1)
    rows = await build_search(text)
    results = []
    for r in rows:
        link = await create_start_link(bot, payload=r["code"], encode=False)
        results.append(InlineQueryResultArticle(
            id=r["code"], title=r["title"], description="Pulsa para ver",
            input_message_content=InputTextMessageContent(
                message_text=f"🎬 <b>{r['title']}</b>\n\n👉 <a href='{link}'>Ver contenido</a>"
            ),
        ))
    await query.answer(results, cache_time=10)

# ================= FAVORITOS =================
@dp.callback_query(FavToggleCB.filter())
async def toggle_fav(query: types.CallbackQuery, callback_data: FavToggleCB):
    uid, code = query.from_user.id, callback_data.code
    async with db_pool.acquire() as conn:
        exists = await conn.fetchrow("SELECT 1 FROM favorites WHERE user_id=$1 AND code=$2", uid, code)
        if exists:
            await conn.execute("DELETE FROM favorites WHERE user_id=$1 AND code=$2", uid, code)
            msg = "🗑 Eliminado."
        else:
            await conn.execute(
                "INSERT INTO favorites (user_id,code) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                uid, code,
            )
            msg = "⭐ Añadido."
    await query.answer(msg)

@dp.message(Command("favoritos"))
async def favs(message: types.Message):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT m.code, m.title FROM favorites f JOIN content m ON m.code=f.code "
            "WHERE f.user_id=$1 AND m.status='ready'", message.from_user.id,
        )
    if not rows:
        return await message.answer("⭐ Sin favoritos.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎬 {r['title'][:50]}", callback_data=SearchCB(code=r["code"]).pack())]
        for r in rows
    ])
    await message.answer(f"⭐ Favoritos ({len(rows)}):", reply_markup=kb)

# ================= ADMIN — PANEL =================
ADMIN_SECTIONS = {
    "guia": (
        "📝 <b>CÓMO PUBLICAR CONTENIDO</b>\n\n"
        "<b>🎬 Película en 1 post (recomendado)</b>\n"
        "Reenvía al canal privado un post que ya tenga <b>video + descripción</b>. "
        "El bot lo detecta automáticamente y te devuelve el botón listo.\n\n"
        "<b>🎬 Película en 2 pasos</b>\n"
        "1. Reenvía la descripción (texto o foto) → el bot te dará un código.\n"
        "2. Reenvía el video:\n"
        "   • <b>Respondiendo</b> al mensaje del bot con el código.\n"
        "   • O simplemente reenvíalo en los próximos 10 min (auto-vinculación).\n\n"
        "<b>📺 Serie con temporadas</b>\n"
        "1. Reenvía la descripción → obtienes <code>peli_XXX</code>.\n"
        "2. Responde al mensaje del bot con <code>/season 1</code>.\n"
        "3. Reenvía los episodios de la T1 (respondiendo al mensaje de la temporada "
        "o dentro de los 10 min).\n"
        "4. Repite con <code>/season 2</code>, <code>/season 3</code>...\n\n"
        "⚠️ Si la auto-vinculación falla, responde al video con "
        "<code>/link codigo</code>."
    ),
    "pelis": (
        "🎬 <b>COMANDOS DE PELÍCULAS</b>\n\n"
        "<b>/link codigo</b>\n"
        "Responde a un video con este comando para vincularlo manualmente "
        "a una película ya creada.\n"
        "<i>Ejemplo:</i> responde al video con <code>/link peli_123</code>\n\n"
        "🔗 <b>Enlace generado:</b>\n"
        "<code>https://t.me/TuBot?start=peli_123</code>\n\n"
        "Al pulsarlo, el usuario recibe primero la descripción y luego el video."
    ),
    "series": (
        "📺 <b>COMANDOS DE SERIES</b>\n\n"
        "<b>/season N</b>\n"
        "Respondiendo al mensaje de confirmación de una descripción, crea una temporada.\n"
        "<i>Ejemplo:</i> <code>/season 1</code>, <code>/season 2</code>...\n\n"
        "🔗 <b>Enlace generado por temporada:</b>\n"
        "<code>https://t.me/TuBot?start=peli_XXX_s1</code>\n"
        "<code>https://t.me/TuBot?start=peli_XXX_s2</code>\n\n"
        "Al pulsar el de una temporada, el bot envía <b>todos los episodios de esa "
        "temporada, en orden, uno tras otro</b>. No mezcla temporadas.\n\n"
        "💡 En tu canal público puedes poner un botón por temporada, todos apuntando "
        "a la serie, cada uno con su <code>_sN</code>."
    ),
    "enlaces": (
        "🔗 <b>ENLACES Y BOTONES</b>\n\n"
        "<b>Película:</b>\n"
        "<code>https://t.me/TuBot?start=peli_123</code>\n\n"
        "<b>Serie completa (menú de temporadas):</b>\n"
        "<code>https://t.me/TuBot?start=peli_456</code>\n\n"
        "<b>Temporada concreta:</b>\n"
        "<code>https://t.me/TuBot?start=peli_456_s2</code>\n\n"
        "💡 Copia ese enlace y pégalo en el botón inline de tu post del canal público. "
        "Si es una serie con varias temporadas, pon varios botones, uno por temporada.\n\n"
        "🛠 Cuando creas un contenido o una temporada, el bot te devuelve el botón listo "
        "para reenviarlo directamente al canal público."
    ),
    "msgs": (
        "📢 <b>MENSAJES MASIVOS</b>\n\n"
        "<b>/notify codigo</b>\n"
        "Envía a todos los usuarios registrados un mensaje corto con el botón «Ver ahora» "
        "del contenido indicado. Ideal para anunciar estrenos.\n"
        "<i>Ejemplo:</i> <code>/notify peli_123</code> o <code>/notify peli_456_s1</code>\n\n"
        "<b>/broadcast</b>\n"
        "Responde a cualquier mensaje con <code>/broadcast</code> y el bot lo reenviará "
        "a todos los usuarios (texto, foto, video, etc.).\n\n"
        "⚠️ El bot respeta el límite de Telegram (~30 msg/seg). Los usuarios que hayan "
        "bloqueado al bot se cuentan como fallidos."
    ),
    "stats": (
        "📊 <b>ESTADÍSTICAS Y LISTADO</b>\n\n"
        "<b>/stats</b>\n"
        "Muestra:\n"
        "• 👥 Usuarios registrados\n"
        "• 🎬 Películas listas\n"
        "• 📺 Series creadas\n"
        "• 🎞 Episodios totales\n"
        "• ⏳ Contenido pendiente de vincular\n\n"
        "<b>/lista</b>\n"
        "Lista todo el contenido con su código, título y estado.\n"
        "En las series muestra también cada temporada y el número de episodios.\n\n"
        "💡 Usa <code>/lista</code> para recordar los códigos antes de usar "
        "<code>/notify</code>."
    ),
    "otros": (
        "⚙️ <b>OTROS COMANDOS</b>\n\n"
        "<b>/link codigo</b>\n"
        "Vincula manualmente un video a un contenido ya creado. Responde al video "
        "con <code>/link codigo</code>.\n\n"
        "<b>/admin</b>\n"
        "Abre este panel (solo tú puedes usarlo).\n\n"
        "🧹 <b>Validación automática</b>\n"
        "Cada 6 h el bot comprueba que los <code>file_id</code> sigan siendo válidos. "
        "Si alguno caduca (porque borraste el archivo en el canal privado), el contenido "
        "se marca como <code>inválido</code> y te avisa por privado.\n\n"
        "🔒 <b>Seguridad</b>\n"
        "Todos los comandos de admin están protegidos: si otro usuario los intenta, "
        "el bot no responde."
    ),
}

def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Cómo publicar", callback_data=AdminCB(section="guia").pack())],
        [
            InlineKeyboardButton(text="🎬 Películas", callback_data=AdminCB(section="pelis").pack()),
            InlineKeyboardButton(text="📺 Series",   callback_data=AdminCB(section="series").pack()),
        ],
        [
            InlineKeyboardButton(text="🔗 Enlaces",  callback_data=AdminCB(section="enlaces").pack()),
            InlineKeyboardButton(text="📢 Mensajes", callback_data=AdminCB(section="msgs").pack()),
        ],
        [
            InlineKeyboardButton(text="📊 Stats",    callback_data=AdminCB(section="stats").pack()),
            InlineKeyboardButton(text="⚙️ Otros",    callback_data=AdminCB(section="otros").pack()),
        ],
        [InlineKeyboardButton(text="❌ Cerrar panel", callback_data=AdminCB(section="close").pack())],
    ])

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Volver al panel", callback_data=AdminCB(section="menu").pack())],
        [InlineKeyboardButton(text="❌ Cerrar",           callback_data=AdminCB(section="close").pack())],
    ])

@dp.message(Command("admin"), IsAdmin())
async def admin_panel(message: types.Message):
    await message.answer(
        "🛠 <b>PANEL DE ADMINISTRADOR</b>\n\n"
        "Toca una categoría para ver los comandos y su explicación:",
        reply_markup=admin_panel_kb(),
    )

@dp.callback_query(AdminCB.filter(), IsAdmin())
async def admin_callback(query: types.CallbackQuery, callback_data: AdminCB):
    section = callback_data.section

    if section == "close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if section == "menu":
        await query.answer()
        return await query.message.edit_text(
            "🛠 <b>PANEL DE ADMINISTRADOR</b>\n\n"
            "Toca una categoría para ver los comandos y su explicación:",
            reply_markup=admin_panel_kb(),
        )

    text = ADMIN_SECTIONS.get(section)
    if not text:
        return await query.answer("Sección no encontrada.", show_alert=True)

    await query.answer()
    await query.message.edit_text(text, reply_markup=back_kb())

# ================= ADMIN — COMANDOS =================
@dp.message(Command("notify"), IsAdmin())
async def notify(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("Uso: <code>/notify codigo</code>")
    code = command.args.strip()
    async with db_pool.acquire() as conn:
        if await conn.fetchrow("SELECT 1 FROM seasons WHERE code=$1", code):
            title = f"Temporada ({code})"
        else:
            row = await conn.fetchrow("SELECT title FROM content WHERE code=$1 AND status='ready'", code)
            if not row:
                return await message.answer("❌ Código no encontrado.")
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
        except Exception:
            failed += 1
    await status.edit_text(f"✅ Enviados: {sent} | ❌ Fallidos: {failed}")

@dp.message(Command("broadcast"), IsAdmin())
async def broadcast(message: types.Message):
    if not message.reply_to_message:
        return await message.answer("Responde a un mensaje con /broadcast")
    async with db_pool.acquire() as conn:
        users = [r["user_id"] for r in await conn.fetch("SELECT user_id FROM users")]
    status = await message.answer(f"📤 Enviando a {len(users)} usuarios...")
    sent = failed = 0
    for uid in users:
        try:
            await message.reply_to_message.copy_to(chat_id=uid)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await status.edit_text(f"✅ Enviados: {sent} | ❌ Fallidos: {failed}")

@dp.message(Command("stats"), IsAdmin())
async def stats(message: types.Message):
    async with db_pool.acquire() as conn:
        u = (await conn.fetchrow("SELECT COUNT(*) AS c FROM users"))["c"]
        m = (await conn.fetchrow(
            "SELECT COUNT(*) AS c FROM content WHERE status='ready' AND content_type='movie'"))["c"]
        s = (await conn.fetchrow(
            "SELECT COUNT(*) AS c FROM content WHERE content_type='series'"))["c"]
        e = (await conn.fetchrow("SELECT COUNT(*) AS c FROM episodes"))["c"]
        p = (await conn.fetchrow(
            "SELECT COUNT(*) AS c FROM content WHERE status='pending'"))["c"]
    await message.answer(
        f"📊 <b>Estadísticas</b>\n\n👥 Usuarios: {u}\n🎬 Películas: {m}\n"
        f"📺 Series: {s}\n🎞 Episodios: {e}\n⏳ Pendientes: {p}"
    )

@dp.message(Command("lista"), IsAdmin())
async def lista(message: types.Message):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT code, title, content_type, status FROM content ORDER BY code")
        if not rows:
            return await message.answer("Sin contenido.")
        lines = []
        for r in rows:
            icon = "✅" if r["status"] == "ready" else "⏳"
            kind = "📺" if r["content_type"] == "series" else "🎬"
            lines.append(f"{icon}{kind} <code>{r['code']}</code> — {r['title'][:40]}")
            if r["content_type"] == "series":
                ss = await conn.fetch(
                    "SELECT code, season_number FROM seasons WHERE content_code=$1 "
                    "ORDER BY season_number", r["code"],
                )
                for s in ss:
                    cnt = (await conn.fetchrow(
                        "SELECT COUNT(*) AS c FROM episodes WHERE season_code=$1", s["code"]
                    ))["c"]
                    lines.append(f"   └─ <code>{s['code']}</code> ({cnt} eps)")
    await message.answer("📚 <b>Contenido</b>\n\n" + "\n".join(lines))

# ================= VALIDACIÓN PERIÓDICA =================
async def validate_file_ids():
    while True:
        await asyncio.sleep(6 * 3600)
        logging.info("🔍 Validando file_ids...")
        async with db_pool.acquire() as conn:
            for row in await conn.fetch("SELECT code, file_id FROM content WHERE file_id IS NOT NULL"):
                try:
                    await bot.get_file(row["file_id"])
                except Exception:
                    await conn.execute("UPDATE content SET status='invalid' WHERE code=$1", row["code"])
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"⚠️ file_id inválido en <code>{row['code']}</code>",
                        )
                    except Exception:
                        pass
            for row in await conn.fetch("SELECT id, file_id FROM episodes"):
                try:
                    await bot.get_file(row["file_id"])
                except Exception:
                    await conn.execute("DELETE FROM episodes WHERE id=$1", row["id"])
        logging.info("✅ Validación completa.")

# ================= ARRANQUE =================
async def main():
    await init_db()
    asyncio.create_task(validate_file_ids())
    logging.info("🤖 Bot iniciado.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(main())
