import asyncio
import os
import logging
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import (
    FloodWait, ChatWriteForbidden, SlowmodeWait,
    UserIsBlocked, PeerFlood, ChatAdminRequired,
    UserBannedInChannel, RPCError
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

# ─── Load sessions ─────────────────────────────────────────────────────────────
def load_sessions() -> list[str]:
    sessions = []
    base = os.environ.get("SESSION_STRING")
    if base:
        sessions.append(base)
    i = 1
    while True:
        s = os.environ.get(f"SESSION_STRING{i}")
        if s:
            sessions.append(s)
            i += 1
        else:
            break
    return sessions

# ─── Per-account state ─────────────────────────────────────────────────────────
spam_tasks: dict[int, dict[int, asyncio.Task]] = {}
first_message_seen: dict[int, set[int]] = {}

# Auto-responder texts — None means not configured yet
auto_msg1: str | None = None
auto_msg2: str | None = None

# ─── Client factory ────────────────────────────────────────────────────────────
def make_client(session_string: str, idx: int) -> Client:
    return Client(
        name=f"account_{idx}",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_string,
        in_memory=True,
    )

# ─── Helpers ───────────────────────────────────────────────────────────────────
async def notify_me(client: Client, text: str):
    """Send a notification to Saved Messages (избранное)."""
    try:
        await client.send_message("me", text)
    except Exception as e:
        logger.warning(f"Could not send notification to Saved Messages: {e}")


async def spam_loop(client: Client, chat_id: int, chat_title: str, text: str, acc_idx: int, interval_sec: int):
    """
    Sends `text` to `chat_id` every `interval_sec` seconds.
    Automatically adapts to slowmode. Stops on ban/mute.
    Reports start and stop to Saved Messages.
    """
    interval_min = interval_sec // 60
    await notify_me(
        client,
        f"▶️ Спам запущен\n"
        f"📍 Чат: {chat_title}\n"
        f"⏱ Интервал: каждые {interval_min} мин.\n"
        f"💬 Текст: {text[:100]}{'…' if len(text) > 100 else ''}"
    )

    consecutive_errors = 0
    current_interval = interval_sec
    stop_reason = None

    try:
        while True:
            try:
                await client.send_message(chat_id, text)
                consecutive_errors = 0
                logger.info(f"[acc{acc_idx}] Spam sent to {chat_id}, next in {current_interval}s")
                await asyncio.sleep(current_interval)

            except SlowmodeWait as e:
                wait = e.value + 1
                logger.info(f"[acc{acc_idx}] Slowmode {e.value}s in {chat_id}")
                if wait > current_interval:
                    current_interval = wait
                await asyncio.sleep(wait)

            except UserBannedInChannel as e:
                stop_reason = "🚫 Аккаунт забанен в канале/группе"
                break

            except ChatWriteForbidden:
                stop_reason = "🔇 Нет прав на отправку сообщений (мут или запрет)"
                break

            except ChatAdminRequired:
                stop_reason = "👮 Требуются права администратора"
                break

            except FloodWait as e:
                logger.warning(f"[acc{acc_idx}] FloodWait {e.value}s")
                await asyncio.sleep(e.value)

            except asyncio.CancelledError:
                stop_reason = "⛔️ Остановлен командой /stopspam"
                raise

            except RPCError as e:
                consecutive_errors += 1
                logger.error(f"[acc{acc_idx}] RPCError in spam_loop: {e}")
                if consecutive_errors >= 5:
                    stop_reason = f"❌ Слишком много ошибок подряд: {e}"
                    break
                await asyncio.sleep(5)

            except Exception as e:
                consecutive_errors += 1
                logger.error(f"[acc{acc_idx}] Unexpected error: {e}")
                if consecutive_errors >= 5:
                    stop_reason = f"❌ Неожиданная ошибка: {e}"
                    break
                await asyncio.sleep(5)

    except asyncio.CancelledError:
        pass
    finally:
        spam_tasks[acc_idx].pop(chat_id, None)
        reason = stop_reason or "⛔️ Остановлен командой /stopspam"
        await notify_me(
            client,
            f"⏹ Спам остановлен\n"
            f"📍 Чат: {chat_title}\n"
            f"📌 Причина: {reason}"
        )
        logger.info(f"[acc{acc_idx}] Spam stopped in {chat_id}: {reason}")


def build_handlers(client: Client, acc_idx: int):
    global auto_msg1, auto_msg2

    # ── /spam [Nс] <текст> ───────────────────────────────────────────────────
    # /spam текст          → каждую минуту
    # /spam 5с текст       → каждые 5 минут (1с = 1 минута)
    @client.on_message(filters.command("spam", prefixes="/") & filters.me)
    async def cmd_spam(c: Client, msg: Message):
        args = msg.text.split(maxsplit=1)
        if len(args) < 2:
            await msg.reply(
                "Использование:\n"
                "  /spam текст — каждую минуту\n"
                "  /spam 5с текст — каждые 5 минут\n\n"
                "1с = 1 минута. Slowmode подстраивается автоматически."
            )
            return

        rest = args[1]
        interval_min = 1  # default

        parts = rest.split(maxsplit=1)
        if parts[0].endswith("с") and parts[0][:-1].isdigit():
            interval_min = int(parts[0][:-1])
            if len(parts) < 2:
                await msg.reply("Укажи текст: /spam 3с твой текст")
                return
            spam_text = parts[1]
        else:
            spam_text = rest

        interval_sec = interval_min * 60
        chat_id = msg.chat.id
        chat_title = msg.chat.title or msg.chat.first_name or str(chat_id)

        old_task = spam_tasks[acc_idx].get(chat_id)
        if old_task and not old_task.done():
            old_task.cancel()
            await asyncio.sleep(0.5)

        task = asyncio.create_task(
            spam_loop(c, chat_id, chat_title, spam_text, acc_idx, interval_sec)
        )
        spam_tasks[acc_idx][chat_id] = task
        await msg.delete()

    # ── /stopspam ────────────────────────────────────────────────────────────
    @client.on_message(filters.command("stopspam", prefixes="/") & filters.me)
    async def cmd_stopspam(c: Client, msg: Message):
        chat_id = msg.chat.id
        task = spam_tasks[acc_idx].get(chat_id)
        if task and not task.done():
            task.cancel()
            await msg.delete()
        else:
            await msg.reply("Нет активного спама в этом чате.")

    # ── /setmsg1 <текст> ─────────────────────────────────────────────────────
    @client.on_message(filters.command("setmsg1", prefixes="/") & filters.me)
    async def cmd_setmsg1(c: Client, msg: Message):
        global auto_msg1
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2:
            current = auto_msg1 or "(не задано)"
            await msg.reply(f"Текущее 1-е сообщение:\n{current}\n\nЧтобы изменить: /setmsg1 новый текст")
            return
        auto_msg1 = parts[1]
        await msg.reply(f"✅ 1-е сообщение обновлено:\n{auto_msg1}")

    # ── /setmsg2 <текст> ─────────────────────────────────────────────────────
    @client.on_message(filters.command("setmsg2", prefixes="/") & filters.me)
    async def cmd_setmsg2(c: Client, msg: Message):
        global auto_msg2
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2:
            current = auto_msg2 or "(не задано)"
            await msg.reply(f"Текущее 2-е сообщение:\n{current}\n\nЧтобы изменить: /setmsg2 новый текст")
            return
        auto_msg2 = parts[1]
        await msg.reply(f"✅ 2-е сообщение обновлено:\n{auto_msg2}")

    # ── /msgs — показать текущие тексты ─────────────────────────────────────
    @client.on_message(filters.command("msgs", prefixes="/") & filters.me)
    async def cmd_msgs(c: Client, msg: Message):
        m1 = auto_msg1 or "_(не задано — автоответчик неактивен)_"
        m2 = auto_msg2 or "_(не задано — автоответчик неактивен)_"
        await msg.reply(
            f"📨 **Автоответчик:**\n\n"
            f"**1-е сообщение:**\n{m1}\n\n"
            f"**2-е сообщение:**\n{m2}"
        )

    # ── Private message auto-responder ───────────────────────────────────────
    @client.on_message(filters.private & ~filters.me & ~filters.bot)
    async def auto_reply(c: Client, msg: Message):
        user_id = msg.from_user.id if msg.from_user else None
        if user_id is None:
            return

        seen = first_message_seen[acc_idx]

        # ── Flood detection ────────────────────────────────────────────────
        try:
            messages = [m async for m in c.get_chat_history(user_id, limit=5)]

            if len(messages) >= 5 and all(getattr(m, "sticker", None) for m in messages[:5]):
                await c.block_user(user_id)
                await c.delete_chat_history(user_id)
                logger.info(f"[acc{acc_idx}] Blocked {user_id} — sticker flood")
                return

            texts = [m.text for m in messages[:5] if m.text]
            if len(texts) >= 5 and len(set(texts)) == 1:
                await c.block_user(user_id)
                await c.delete_chat_history(user_id)
                logger.info(f"[acc{acc_idx}] Blocked {user_id} — text flood")
                return
        except Exception as e:
            logger.warning(f"[acc{acc_idx}] History check error for {user_id}: {e}")

        # ── First-time message → auto-reply ───────────────────────────────
        # Only reply if both messages are configured
        if user_id not in seen and auto_msg1 is not None and auto_msg2 is not None:
            seen.add(user_id)
            try:
                await c.send_message(user_id, auto_msg1)
                await c.send_chat_action(user_id, "typing")
                await asyncio.sleep(5)
                await c.send_message(user_id, auto_msg2)
                await c.update_chat_notification_settings(
                    user_id,
                    mute_until=2147483647
                )
                await c.archive_chats(user_id)
                logger.info(f"[acc{acc_idx}] Auto-replied + muted + archived {user_id}")
            except (UserIsBlocked, PeerFlood) as e:
                logger.warning(f"[acc{acc_idx}] Could not reply to {user_id}: {e}")
            except Exception as e:
                logger.error(f"[acc{acc_idx}] Auto-reply error for {user_id}: {e}")


async def main():
    sessions = load_sessions()
    if not sessions:
        logger.error(
            "Не найдено ни одной сессии! "
            "Добавь SESSION_STRING (и SESSION_STRING1, SESSION_STRING2, ...) в переменные Railway."
        )
        return

    logger.info(f"Запускаю {len(sessions)} аккаунт(ов)...")

    clients = []
    for idx, session_string in enumerate(sessions):
        spam_tasks[idx] = {}
        first_message_seen[idx] = set()
        c = make_client(session_string, idx)
        build_handlers(c, idx)
        clients.append(c)

    for c in clients:
        await c.start()
        me = await c.get_me()
        logger.info(f"✅ Аккаунт подключён: {me.first_name} (@{me.username})")

    logger.info("Все аккаунты запущены. Работаю...")
    await asyncio.gather(*[asyncio.Event().wait() for _ in clients])


if __name__ == "__main__":
    asyncio.run(main())
