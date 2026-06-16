import asyncio
import os
import logging
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import (
    FloodWait, UserBanned, ChatWriteForbidden, SlowmodeWait,
    UserIsBlocked, PeerFlood, ChatAdminRequired
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ─── Telegram API credentials (public Telegram Desktop credentials) ────────────
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

# ─── Load sessions from environment ───────────────────────────────────────────
def load_sessions() -> list[str]:
    """
    Reads SESSION_STRING, SESSION_STRING1, SESSION_STRING2, ... from env.
    Add as many as you want — they are all picked up automatically.
    """
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

# ─── Per-account state ────────────────────────────────────────────────────────
spam_tasks: dict[int, dict[int, asyncio.Task]] = {}
first_message_seen: dict[int, set[int]] = {}

# ─── Helpers ──────────────────────────────────────────────────────────────────
def make_client(session_string: str, idx: int) -> Client:
    return Client(
        name=f"account_{idx}",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_string,
        in_memory=True,
    )


async def spam_loop(client: Client, chat_id: int, text: str, acc_idx: int):
    """Continuously sends text to chat_id every minute (or slowmode interval)."""
    interval = 60
    consecutive_errors = 0

    while True:
        try:
            await client.send_message(chat_id, text)
            consecutive_errors = 0
            logger.info(f"[acc{acc_idx}] Spam sent to {chat_id}")
        except SlowmodeWait as e:
            interval = e.value + 2
            logger.info(f"[acc{acc_idx}] Slowmode detected: waiting {e.value}s, adapting interval")
            await asyncio.sleep(e.value)
            continue
        except (UserBanned, ChatWriteForbidden, ChatAdminRequired) as e:
            logger.warning(f"[acc{acc_idx}] Spam stopped in {chat_id}: {e}")
            spam_tasks[acc_idx].pop(chat_id, None)
            return
        except FloodWait as e:
            logger.warning(f"[acc{acc_idx}] FloodWait {e.value}s")
            await asyncio.sleep(e.value)
            continue
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"[acc{acc_idx}] Unexpected error in spam_loop: {e}")
            if consecutive_errors >= 5:
                spam_tasks[acc_idx].pop(chat_id, None)
                return

        await asyncio.sleep(interval)


def build_handlers(client: Client, acc_idx: int):
    """Register all event handlers on the given client."""

    # ── /spam <text> ──────────────────────────────────────────────────────────
    @client.on_message(filters.command("spam", prefixes="/") & filters.me)
    async def cmd_spam(c: Client, msg: Message):
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply("Использование: /spam текст который нужно спамить")
            return

        spam_text = parts[1]
        chat_id = msg.chat.id

        # Cancel previous spam task in this chat if running
        old_task = spam_tasks[acc_idx].get(chat_id)
        if old_task and not old_task.done():
            old_task.cancel()

        task = asyncio.create_task(spam_loop(c, chat_id, spam_text, acc_idx))
        spam_tasks[acc_idx][chat_id] = task
        await msg.delete()
        logger.info(f"[acc{acc_idx}] Spam started in {chat_id}: {spam_text[:30]}...")

    # ── /stopspam ─────────────────────────────────────────────────────────────
    @client.on_message(filters.command("stopspam", prefixes="/") & filters.me)
    async def cmd_stopspam(c: Client, msg: Message):
        chat_id = msg.chat.id
        task = spam_tasks[acc_idx].pop(chat_id, None)
        if task and not task.done():
            task.cancel()
            await msg.reply("✅ Спам остановлен.")
        else:
            await msg.reply("Нет активного спама в этом чате.")

    # ── Private message auto-responder ────────────────────────────────────────
    @client.on_message(filters.private & ~filters.me & ~filters.bot)
    async def auto_reply(c: Client, msg: Message):
        user_id = msg.from_user.id if msg.from_user else None
        if user_id is None:
            return

        seen = first_message_seen[acc_idx]

        # ── Flood detection: 5+ identical messages or stickers → block ────────
        try:
            messages = [m async for m in c.get_chat_history(user_id, limit=5)]

            # 5 stickers in a row
            if len(messages) >= 5 and all(getattr(m, "sticker", None) for m in messages[:5]):
                await c.block_user(user_id)
                await c.delete_chat_history(user_id)
                logger.info(f"[acc{acc_idx}] Blocked {user_id} — sticker flood")
                return

            # 5 identical text messages in a row
            texts = [m.text for m in messages[:5] if m.text]
            if len(texts) >= 5 and len(set(texts)) == 1:
                await c.block_user(user_id)
                await c.delete_chat_history(user_id)
                logger.info(f"[acc{acc_idx}] Blocked {user_id} — text flood")
                return
        except Exception as e:
            logger.warning(f"[acc{acc_idx}] History check error for {user_id}: {e}")

        # ── First-time contact → send auto-reply sequence ─────────────────────
        if user_id not in seen:
            seen.add(user_id)
            try:
                await c.send_message(user_id, "Привет")
                await c.send_chat_action(user_id, "typing")
                await asyncio.sleep(5)
                await c.send_message(
                    user_id,
                    "Я сейчас немного занята если ты ищешь сливы то все ссылки в моем профиле в закрепленном канале 😅"
                )
                # Mute forever (unix timestamp max = muted forever)
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
            "Добавь SESSION_STRING (и SESSION_STRING1, SESSION_STRING2, ...) в переменные окружения."
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
    # Keep running forever
    await asyncio.gather(*[asyncio.Event().wait() for _ in clients])


if __name__ == "__main__":
    asyncio.run(main())
