import asyncio
import os
import logging
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import (
    FloodWait, ChatWriteForbidden, SlowmodeWait,
    UserIsBlocked, PeerFlood, ChatAdminRequired,
    UserBannedInChannel, RPCError, PeerIdInvalid,
    ChannelPrivate, InputUserDeactivated,
    UserDeactivated, UserDeactivatedBan, FloodPremiumWait,
    MessageDeleteForbidden
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
FLOOD_THRESHOLD = 5

# ─── Load sessions ─────────────────────────────────────────────────────────────
def load_sessions() -> list[str]:
    sessions = []
    if s := os.environ.get("SESSION_STRING"):
        sessions.append(s)
    i = 1
    while s := os.environ.get(f"SESSION_STRING{i}"):
        sessions.append(s)
        i += 1
    return sessions

# ─── Per-account state ─────────────────────────────────────────────────────────
spam_tasks: dict[int, dict[int, asyncio.Task]] = {}
first_message_seen: dict[int, set[int]] = {}
user_recent: dict[int, dict[int, list[str]]] = {}

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

# ─── Safe wrappers ─────────────────────────────────────────────────────────────
async def notify_me(client: Client, text: str):
    try:
        await client.send_message("me", text)
    except Exception as e:
        logger.warning(f"notify_me failed: {e}")

async def safe_block_and_delete(client: Client, user_id: int, acc_idx: int):
    try:
        await client.block_user(user_id)
    except Exception as e:
        logger.debug(f"[acc{acc_idx}] block_user {user_id}: {e}")
    try:
        await client.delete_chat_history(user_id)
    except Exception as e:
        logger.debug(f"[acc{acc_idx}] delete_chat_history {user_id}: {e}")

async def safe_mute_and_archive(client: Client, user_id: int, acc_idx: int):
    try:
        from pyrogram.raw import functions, types as raw_types
        await client.invoke(
            functions.account.UpdateNotifySettings(
                peer=raw_types.InputNotifyPeer(
                    peer=await client.resolve_peer(user_id)
                ),
                settings=raw_types.InputPeerNotifySettings(
                    mute_until=2147483647,
                    show_previews=False,
                    silent=True,
                )
            )
        )
    except Exception as e:
        logger.debug(f"[acc{acc_idx}] mute {user_id}: {e}")
    try:
        await client.archive_chats([user_id])
    except Exception as e:
        logger.debug(f"[acc{acc_idx}] archive {user_id}: {e}")

# ─── Spam loop ─────────────────────────────────────────────────────────────────
async def spam_loop(
    client: Client,
    chat_id: int,
    chat_title: str,
    text: str,
    acc_idx: int,
    interval_sec: int,
):
    interval_min = interval_sec // 60
    await notify_me(
        client,
        f"▶️ Спам запущен\n"
        f"📍 Чат: {chat_title}\n"
        f"⏱ Интервал: каждые {interval_min} мин.\n"
        f"💬 Текст: {text[:100]}{'…' if len(text) > 100 else ''}",
    )

    consecutive_errors = 0
    current_interval = interval_sec
    stop_reason: str | None = None

    try:
        while True:
            try:
                await client.send_message(chat_id, text)
                consecutive_errors = 0
                logger.info(f"[acc{acc_idx}] spam → {chat_id}, next in {current_interval}s")
                await asyncio.sleep(current_interval)

            except SlowmodeWait as e:
                wait = e.value + 1
                logger.info(f"[acc{acc_idx}] slowmode {e.value}s in {chat_id}, adapting")
                if wait > current_interval:
                    current_interval = wait
                await asyncio.sleep(wait)

            except UserBannedInChannel:
                stop_reason = "🚫 Аккаунт забанен в группе/канале"
                break

            except ChatWriteForbidden:
                stop_reason = "🔇 Нет прав на отправку (мут или запрет)"
                break

            except ChatAdminRequired:
                stop_reason = "👮 Требуются права администратора"
                break

            except (UserDeactivated, UserDeactivatedBan):
                stop_reason = "💀 Аккаунт деактивирован или забанен Telegram"
                break

            except (FloodWait, FloodPremiumWait) as e:
                logger.warning(f"[acc{acc_idx}] FloodWait {e.value}s")
                await asyncio.sleep(e.value)

            except asyncio.CancelledError:
                stop_reason = "⛔️ Остановлен командой /stopspam"
                raise

            except RPCError as e:
                consecutive_errors += 1
                logger.error(f"[acc{acc_idx}] RPCError: {e}")
                if consecutive_errors >= 5:
                    stop_reason = f"❌ Слишком много ошибок: {e}"
                    break
                await asyncio.sleep(10)

            except Exception as e:
                consecutive_errors += 1
                logger.error(f"[acc{acc_idx}] error in spam: {e}")
                if consecutive_errors >= 5:
                    stop_reason = f"❌ Неожиданная ошибка: {e}"
                    break
                await asyncio.sleep(10)

    except asyncio.CancelledError:
        pass
    finally:
        spam_tasks[acc_idx].pop(chat_id, None)
        reason = stop_reason or "⛔️ Остановлен командой /stopspam"
        await notify_me(
            client,
            f"⏹ Спам остановлен\n"
            f"📍 Чат: {chat_title}\n"
            f"📌 Причина: {reason}",
        )

# ─── Handlers ──────────────────────────────────────────────────────────────────
def build_handlers(client: Client, acc_idx: int):
    global auto_msg1, auto_msg2

    # Используем filters.outgoing — надёжнее чем filters.me для исходящих в группах
    my_cmd = filters.command(["spam", "stopspam", "setmsg1", "setmsg2", "msgs"], prefixes="/") & filters.outgoing

    @client.on_message(filters.command("spam", prefixes="/") & filters.outgoing)
    async def cmd_spam(c: Client, msg: Message):
        args = msg.text.split(maxsplit=1)
        if len(args) < 2:
            await msg.reply(
                "📌 Использование:\n"
                "  /spam текст — каждую минуту\n"
                "  /spam 5с текст — каждые 5 минут\n\n"
                "1с = 1 минута. Slowmode подстраивается автоматически."
            )
            return

        rest = args[1]
        interval_min = 1
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
        chat_title = (
            getattr(msg.chat, "title", None)
            or getattr(msg.chat, "first_name", None)
            or str(chat_id)
        )

        old_task = spam_tasks[acc_idx].get(chat_id)
        if old_task and not old_task.done():
            old_task.cancel()
            await asyncio.sleep(0.3)

        task = asyncio.create_task(
            spam_loop(c, chat_id, chat_title, spam_text, acc_idx, interval_sec)
        )
        spam_tasks[acc_idx][chat_id] = task

        # Удаляем команду — если нет прав в группе просто игнорируем
        try:
            await msg.delete()
        except (MessageDeleteForbidden, RPCError):
            pass

    @client.on_message(filters.command("stopspam", prefixes="/") & filters.outgoing)
    async def cmd_stopspam(c: Client, msg: Message):
        chat_id = msg.chat.id
        task = spam_tasks[acc_idx].get(chat_id)
        if task and not task.done():
            task.cancel()
        else:
            await msg.reply("Нет активного спама в этом чате.")
            return
        try:
            await msg.delete()
        except Exception:
            pass

    @client.on_message(filters.command("setmsg1", prefixes="/") & filters.outgoing)
    async def cmd_setmsg1(c: Client, msg: Message):
        global auto_msg1
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply(f"Текущее 1-е:\n{auto_msg1 or '(не задано)'}\n\nИзменить: /setmsg1 текст")
            return
        auto_msg1 = parts[1]
        await msg.reply(f"✅ 1-е сообщение:\n{auto_msg1}")

    @client.on_message(filters.command("setmsg2", prefixes="/") & filters.outgoing)
    async def cmd_setmsg2(c: Client, msg: Message):
        global auto_msg2
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply(f"Текущее 2-е:\n{auto_msg2 or '(не задано)'}\n\nИзменить: /setmsg2 текст")
            return
        auto_msg2 = parts[1]
        await msg.reply(f"✅ 2-е сообщение:\n{auto_msg2}")

    @client.on_message(filters.command("msgs", prefixes="/") & filters.outgoing)
    async def cmd_msgs(c: Client, msg: Message):
        m1 = auto_msg1 or "_(не задано — автоответчик неактивен)_"
        m2 = auto_msg2 or "_(не задано — автоответчик неактивен)_"
        await msg.reply(f"📨 **Автоответчик:**\n\n**1-е:**\n{m1}\n\n**2-е:**\n{m2}")

    # ── Автоответчик в ЛС ──────────────────────────────────────────────────────
    @client.on_message(
        filters.private
        & filters.incoming
        & ~filters.me
        & ~filters.bot
        & ~filters.service
    )
    async def auto_reply(c: Client, msg: Message):
        try:
            if not msg.from_user:
                return
            user_id = msg.from_user.id
            # Только реальные люди: положительный ID, не бот
            if user_id <= 0 or msg.from_user.is_bot:
                return

            # ── Антифлуд через in-memory трекинг ──────────────────────────────
            recent = user_recent[acc_idx]
            if user_id not in recent:
                recent[user_id] = []

            if msg.sticker:
                fp = "__sticker__"
            elif msg.text:
                fp = msg.text.strip()
            else:
                fp = f"__media_{msg.media}__"

            history = recent[user_id]
            history.append(fp)
            if len(history) > FLOOD_THRESHOLD:
                history.pop(0)

            if len(history) >= FLOOD_THRESHOLD and len(set(history)) == 1:
                await safe_block_and_delete(c, user_id, acc_idx)
                recent.pop(user_id, None)
                logger.info(f"[acc{acc_idx}] Blocked {user_id} — flood")
                return

            # ── Первое сообщение → автоответ ──────────────────────────────────
            seen = first_message_seen[acc_idx]
            if user_id not in seen and auto_msg1 is not None and auto_msg2 is not None:
                seen.add(user_id)
                try:
                    await c.send_message(user_id, auto_msg1)
                    await c.send_chat_action(user_id, "typing")
                    await asyncio.sleep(5)
                    await c.send_message(user_id, auto_msg2)
                except (UserIsBlocked, PeerFlood, PeerIdInvalid,
                        InputUserDeactivated, UserDeactivated) as e:
                    logger.debug(f"[acc{acc_idx}] auto-reply skip {user_id}: {e}")
                    return
                except Exception as e:
                    logger.warning(f"[acc{acc_idx}] auto-reply error {user_id}: {e}")
                    return
                await safe_mute_and_archive(c, user_id, acc_idx)

        except Exception as e:
            logger.error(f"[acc{acc_idx}] unhandled in auto_reply: {e}")

# ─── Main ──────────────────────────────────────────────────────────────────────
async def main():
    sessions = load_sessions()
    if not sessions:
        logger.error("Не найдено SESSION_STRING! Добавь в переменные Railway.")
        return

    logger.info(f"Запускаю {len(sessions)} аккаунт(ов)...")
    clients = []
    for idx, session_string in enumerate(sessions):
        spam_tasks[idx] = {}
        first_message_seen[idx] = set()
        user_recent[idx] = {}
        c = make_client(session_string, idx)
        build_handlers(c, idx)
        clients.append(c)

    for c in clients:
        await c.start()
        me = await c.get_me()
        logger.info(f"✅ Подключён: {me.first_name} (@{me.username})")

    logger.info("Все аккаунты запущены.")
    await asyncio.gather(*[asyncio.Event().wait() for _ in clients])

if __name__ == "__main__":
    asyncio.run(main())
