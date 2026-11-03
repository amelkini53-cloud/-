import asyncio
import os
import logging
import aiomysql
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", "https://your-app.onrender.com") + "/webhook"

DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", 3306))
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None


def fmt_money(n) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except (ValueError, TypeError):
        return str(n)


def fmt_date(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError, OSError):
        return "—"


async def get_player_stats(nickname: str):
    if not db_pool:
        logging.error("Пул соединений с БД не инициализирован")
        return None

    sql = """
        SELECT
            a.id, a.name, a.level, a.money, a.bank,
            a.admin, a.family_id, a.family_rang,
            a.online, a.last_login, a.reg_time,
            a.donate_current, a.coins, a.moneti,
            a.premium, a.warn, a.mute, a.jail,
            (SELECT COUNT(*) FROM ownable_cars WHERE owner_id = a.id) AS cars_count,
            (SELECT COUNT(*) FROM business     WHERE owner_id = a.id) AS business_count,
            (SELECT COUNT(*) FROM houses       WHERE owner_id = a.id) AS houses_count,
            (SELECT name FROM familynew WHERE id = a.family_id LIMIT 1) AS family_name
        FROM accounts a
        WHERE a.name = %s
        LIMIT 1
    """
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, (nickname,))
                return await cur.fetchone()
    except Exception as e:
        logging.exception(f"Ошибка БД при запросе игрока {nickname}: {e}")
        return None


@dp.message(Command("start"))
async def start_handler(message: types.Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n"
        "Отправь мне ник игрока (как в игре), и я покажу его профиль."
    )


@dp.message()
async def stats_handler(message: types.Message):
    nickname = (message.text or "").strip()
    if not nickname or len(nickname) > 24:
        return

    player = await get_player_stats(nickname)

    if not player:
        await message.answer(f"❌ Игрок с ником {nickname} не найден.")
        return

    admin_status = f"✅ Да (уровень {player['admin']})" if player.get("admin") else "❌ Нет"
    online_status = "🟢 Онлайн" if player.get("online") else "⚫ Оффлайн"

    family_info = "—"
    if player.get("family_id") and player["family_id"] > 0:
        family_name = player.get("family_name") or f"ID {player['family_id']}"
        family_info = f"{family_name} (ранг {player.get('family_rang', '—')})"

    premium_status = "✅ Есть" if player.get("premium") else "❌ Нет"

    text = (
        f"👤 {player['name']}\n"
        f"{online_status}\n\n"
        f"🔹 Уровень: {player['level']}\n"
        f"💰 Деньги: {fmt_money(player['money'])} $\n"
        f"🏦 Банк: {fmt_money(player['bank'])} $\n"
        f"💎 Донат: {player.get('donate_current', 0)}\n"
        f"🪙 Coins: {player.get('coins', 0)} | Moneti: {player.get('moneti', 0)}\n"
        f"🚗 Автопарк: {player['cars_count']} машин\n"
        f"🏢 Бизнесов: {player['business_count']}\n"
        f"🏠 Домов: {player['houses_count']}\n"
        f"👨‍👩‍👧 Семья: {family_info}\n"
        f"🛡️ Админ: {admin_status}\n"
        f"⭐ Premium: {premium_status}\n\n"
        f"⚠️ Варны: {player.get('warn', 0)} | 🔇 Мут: {player.get('mute', 0)}\n"
        f"🚔 Джейл: {'Да' if player.get('jail') else 'Нет'}\n\n"
        f"📅 Регистрация: {fmt_date(player.get('reg_time'))}\n"
        f"🕐 Последний вход: {fmt_date(player.get('last_login'))}"
    )

    # Plain text avoids Telegram Markdown parsing errors from player/database values.
    await message.answer(text)


async def on_startup(bot: Bot):
    global db_pool
    logging.info("Установка вебхука...")
    await bot.set_webhook(WEBHOOK_URL)
    logging.info("Создание пула соединений с MySQL...")
    try:
        db_pool = await aiomysql.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            autocommit=True,
            minsize=1,
            maxsize=5,
            charset="utf8mb4",
        )
        logging.info("Пул соединений с MySQL создан.")
    except Exception as e:
        logging.exception(f"Не удалось подключиться к БД: {e}")


async def on_shutdown(bot: Bot):
    global db_pool
    if db_pool:
        db_pool.close()
        await db_pool.wait_closed()
        logging.info("Пул соединений закрыт.")
    await bot.delete_webhook()


def main():
    app = web.Application()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    async def health(request):
        return web.Response(text="OK")

    app.router.add_get("/health", health)
    port = int(os.environ.get("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
