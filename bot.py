import logging
import os
from datetime import datetime, timezone

import aiomysql
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("player_stats_bot")


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return value


BOT_TOKEN = required_env("BOT_TOKEN")
RENDER_EXTERNAL_URL = required_env("RENDER_EXTERNAL_URL").rstrip("/")
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}/webhook"

DB_HOST = required_env("DB_HOST")
DB_USER = required_env("DB_USER")
DB_PASSWORD = required_env("DB_PASSWORD")
DB_NAME = required_env("DB_NAME")
try:
    DB_PORT = int(os.getenv("DB_PORT", "3306"))
except ValueError as exc:
    raise RuntimeError("DB_PORT должен быть числом") from exc

if not 1 <= DB_PORT <= 65535:
    raise RuntimeError("DB_PORT должен быть в диапазоне 1–65535")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool: aiomysql.Pool | None = None


class DatabaseUnavailable(Exception):
    """The database pool is not available."""


class DatabaseQueryError(Exception):
    """A database query failed."""


def fmt_money(value) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (ValueError, TypeError, OverflowError):
        return "—"


def fmt_date(value) -> str:
    if value is None:
        return "—"
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    except (ValueError, TypeError, OverflowError, OSError):
        return "—"


async def get_player_stats(nickname: str):
    if db_pool is None:
        raise DatabaseUnavailable

    sql = """
        SELECT
            a.id, a.name, a.level, a.money, a.bank,
            a.admin, a.family_id, a.family_rang,
            a.online, a.last_login, a.reg_time,
            a.donate_current, a.coins, a.moneti,
            a.premium, a.warn, a.mute, a.jail,
            (SELECT COUNT(*) FROM ownable_cars WHERE owner_id = a.id) AS cars_count,
            (SELECT COUNT(*) FROM business WHERE owner_id = a.id) AS business_count,
            (SELECT COUNT(*) FROM houses WHERE owner_id = a.id) AS houses_count,
            (SELECT name FROM familynew WHERE id = a.family_id LIMIT 1) AS family_name
        FROM accounts AS a
        WHERE a.name = %s
        LIMIT 1
    """
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, (nickname,))
                return await cur.fetchone()
    except Exception as exc:
        logger.exception("Ошибка выполнения запроса статистики игрока")
        raise DatabaseQueryError from exc


@dp.message(Command("start"))
async def start_handler(message: types.Message):
    first_name = message.from_user.first_name if message.from_user else "игрок"
    await message.answer(
        f"Привет, {first_name}! 👋\n"
        "Отправь игровой ник (до 24 символов), и я покажу статистику игрока."
    )


@dp.message()
async def stats_handler(message: types.Message):
    nickname = (message.text or "").strip()
    if not nickname:
        await message.answer("Отправь игровой ник текстовым сообщением.")
        return
    if len(nickname) > 24:
        await message.answer("Ник слишком длинный. Максимальная длина — 24 символа.")
        return

    try:
        player = await get_player_stats(nickname)
    except (DatabaseUnavailable, DatabaseQueryError):
        await message.answer("⚠️ Сейчас не удаётся получить данные из базы. Попробуй немного позже.")
        return

    if player is None:
        await message.answer("❌ Игрок не найден. Проверь написание ника.")
        return

    admin_level = player.get("admin") or 0
    admin_status = f"✅ Да (уровень {admin_level})" if admin_level else "❌ Нет"
    online_status = "🟢 Онлайн" if player.get("online") else "⚫ Оффлайн"

    family_info = "—"
    family_id = player.get("family_id")
    if family_id:
        family_name = player.get("family_name") or f"ID {family_id}"
        family_info = f"{family_name} (ранг {player.get('family_rang') or '—'})"

    premium_status = "✅ Есть" if player.get("premium") else "❌ Нет"

    response = (
        f"👤 {player.get('name') or nickname}\n"
        f"{online_status}\n\n"
        f"🔹 Уровень: {player.get('level', '—')}\n"
        f"💰 Деньги: {fmt_money(player.get('money'))} $\n"
        f"🏦 Банк: {fmt_money(player.get('bank'))} $\n"
        f"💎 Донат: {player.get('donate_current', 0)}\n"
        f"🪙 Coins: {player.get('coins', 0)} | Moneti: {player.get('moneti', 0)}\n"
        f"🚗 Автопарк: {player.get('cars_count', 0)} машин\n"
        f"🏢 Бизнесов: {player.get('business_count', 0)}\n"
        f"🏠 Домов: {player.get('houses_count', 0)}\n"
        f"👨‍👩‍👧 Семья: {family_info}\n"
        f"🛡️ Админ: {admin_status}\n"
        f"⭐ Premium: {premium_status}\n\n"
        f"⚠️ Варны: {player.get('warn', 0)} | 🔇 Мут: {player.get('mute', 0)}\n"
        f"🚔 Джейл: {'Да' if player.get('jail') else 'Нет'}\n\n"
        f"📅 Регистрация: {fmt_date(player.get('reg_time'))}\n"
        f"🕐 Последний вход: {fmt_date(player.get('last_login'))}"
    )
    await message.answer(response)


@dp.errors()
async def global_error_handler(event):
    logger.exception("Необработанная ошибка при обработке Telegram update", exc_info=event.exception)
    return True


async def on_startup(bot: Bot):
    global db_pool
    logger.info("Запуск приложения. URL вебхука: %s", WEBHOOK_URL)

    try:
        me = await bot.get_me()
        logger.info("Telegram bot identity: id=%s username=@%s", me.id, me.username or "без_username")
    except Exception:
        logger.exception("Не удалось получить информацию о Telegram-боте")
        raise

    try:
        logger.info("Создание пула соединений с MySQL...")
        db_pool = await aiomysql.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            autocommit=True,
            minsize=1,
            maxsize=5,
            connect_timeout=10,
            charset="utf8mb4",
        )
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()
        logger.info("Подключение к MySQL проверено.")
    except Exception:
        logger.exception("MySQL недоступен; команды /start продолжат работать, статистика временно недоступна")
        if db_pool is not None:
            db_pool.close()
            await db_pool.wait_closed()
            db_pool = None

    # Preserve queued updates; never silently discard messages on redeploy.
    logger.info("Установка вебхука Telegram...")
    await bot.set_webhook(
        WEBHOOK_URL,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=False,
    )
    info = await bot.get_webhook_info()
    logger.info(
        "Webhook status: url_configured=%s pending_updates=%s "
        "last_error_date=%s last_error_message=%s",
        bool(info.url),
        info.pending_update_count,
        info.last_error_date,
        info.last_error_message,
    )
    if not info.url:
        raise RuntimeError("Telegram не подтвердил установку вебхука")
    logger.info("Вебхук установлен и проверен.")


async def on_shutdown(bot: Bot):
    global db_pool
    if db_pool is not None:
        db_pool.close()
        await db_pool.wait_closed()
        db_pool = None
        logger.info("Пул соединений MySQL закрыт.")
    logger.info("Экземпляр приложения останавливается; вебхук оставлен для следующего запуска.")


async def health(_request):
    return web.json_response({"status": "ok"})


def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Wait for the update handler to finish before acknowledging Telegram.
    # This allows Telegram to retry delivery if processing fails.
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        handle_in_background=False,
    )
    webhook_handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    port = int(os.getenv("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
