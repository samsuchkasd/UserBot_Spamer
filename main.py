import asyncio
import os
import re
import logging
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from pyrogram.errors import (
    FloodWait, ChatWriteForbidden, SlowmodeWait,
    UserIsBlocked, PeerFlood, ChatAdminRequired,
    UserBannedInChannel, RPCError, PeerIdInvalid,
    InputUserDeactivated, UserDeactivated,
    MessageDeleteForbidden, ChannelInvalid,
    UsernameInvalid, UsernameNotOccupied, ChatAdminInviteRequired,
    ChatForwardsRestricted
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

API_ID  = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
FLOOD_THRESHOLD = 5

# ─── Sessions ──────────────────────────────────────────────────────────────────
def load_sessions() -> list[str]:
    sessions = []
    if s := os.environ.get("SESSION_STRING"):
        sessions.append(s)
    i = 1
    while s := os.environ.get(f"SESSION_STRING{i}"):
        sessions.append(s)
        i += 1
    return sessions

# ─── State ─────────────────────────────────────────────────────────────────────
spam_tasks:         dict[int, dict[int, asyncio.Task]] = {}
first_message_seen: dict[int, set[int]]               = {}
user_recent:        dict[int, dict[int, list[str]]]   = {}
media_tasks:        dict[int, asyncio.Task | None]    = {}
me_ids:             dict[int, int]                    = {}

auto_msg1: str | None = None
auto_msg2: str | None = None

# ─── Channel link parser ────────────────────────────────────────────────────────
CHANNEL_RE = re.compile(
    r"(?:https?://)?t\.me/([a-zA-Z0-9_]{3,})"
    r"|@([a-zA-Z0-9_]{3,})"
    r"|(-100\d{7,})"
)

def parse_channel(text: str) -> str | None:
    text = text.strip()
    m = CHANNEL_RE.search(text)
    if m:
        return m.group(1) or m.group(2) or m.group(3)
    if re.fullmatch(r"-?\d{7,}", text):
        return text
    return None

# ─── Client factory ────────────────────────────────────────────────────────────
def make_client(session_string: str, idx: int) -> Client:
    return Client(
        name=f"account_{idx}",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_string,
        in_memory=True,
    )

# ─── Safe helpers ───────────────────────────────────────────────────────────────
async def notify_me(client: Client, text: str):
    try:
        await client.send_message("me", text)
    except Exception as e:
        logger.warning(f"notify_me: {e}")

async def safe_block_and_delete(client: Client, user_id: int, acc_idx: int):
    for fn in [client.block_user, client.delete_chat_history]:
        try:
            await fn(user_id)
        except Exception as e:
            logger.debug(f"[acc{acc_idx}] {fn.__name__} {user_id}: {e}")

async def safe_mute_and_archive(client: Client, user_id: int, acc_idx: int):
    # All peer operations wrapped individually — one failing must not break the other
    try:
        from pyrogram.raw import functions, types as raw_types
        peer = await client.resolve_peer(user_id)
        await client.invoke(
            functions.account.UpdateNotifySettings(
                peer=raw_types.InputNotifyPeer(peer=peer),
                settings=raw_types.InputPeerNotifySettings(
                    mute_until=2147483647, show_previews=False, silent=True,
                )
            )
        )
    except (PeerIdInvalid, ValueError, KeyError, TypeError) as e:
        logger.debug(f"[acc{acc_idx}] mute peer resolve {user_id}: {e}")
    except Exception as e:
        logger.debug(f"[acc{acc_idx}] mute {user_id}: {e}")

    try:
        await client.archive_chats([user_id])
    except Exception as e:
        logger.debug(f"[acc{acc_idx}] archive {user_id}: {e}")

# ─── Media: copy with fallback to download+reupload ────────────────────────────
async def copy_or_reupload(client: Client, src_chat_id: int, msg: Message, dst_chat_id: int) -> bool:
    """
    Try copy_message first.
    If CHAT_FORWARDS_RESTRICTED — download the file and re-upload without author.
    Returns True on success.
    """
    # Attempt 1: copy_message (no forward header)
    try:
        await client.copy_message(
            chat_id=dst_chat_id,
            from_chat_id=src_chat_id,
            message_id=msg.id,
        )
        return True
    except ChatForwardsRestricted:
        pass  # protected channel — fall through to download+reupload
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return False
    except Exception as e:
        logger.debug(f"copy_message failed: {e}")
        return False

    # Attempt 2: download + re-upload (bypasses copy protection)
    tmp_path = None
    try:
        tmp_path = await client.download_media(msg, file_name=f"/tmp/tg_media_{msg.id}_")

        caption = msg.caption or ""

        if msg.photo:
            await client.send_photo(dst_chat_id, tmp_path, caption=caption)
        elif msg.video:
            await client.send_video(dst_chat_id, tmp_path, caption=caption,
                                    duration=msg.video.duration,
                                    width=msg.video.width,
                                    height=msg.video.height)
        elif msg.document:
            await client.send_document(dst_chat_id, tmp_path, caption=caption)
        elif msg.audio:
            await client.send_audio(dst_chat_id, tmp_path, caption=caption,
                                    duration=msg.audio.duration)
        elif msg.voice:
            await client.send_voice(dst_chat_id, tmp_path, duration=msg.voice.duration)
        elif msg.video_note:
            await client.send_video_note(dst_chat_id, tmp_path,
                                         duration=msg.video_note.duration)
        elif msg.animation:
            await client.send_animation(dst_chat_id, tmp_path, caption=caption)
        elif msg.sticker:
            await client.send_sticker(dst_chat_id, tmp_path)
        else:
            return False

        return True

    except FloodWait as e:
        await asyncio.sleep(e.value)
        return False
    except Exception as e:
        logger.warning(f"reupload failed for msg {msg.id}: {e}")
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

# ─── Media download task ────────────────────────────────────────────────────────
MEDIA_TYPES = {
    enums.MessageMediaType.PHOTO,
    enums.MessageMediaType.VIDEO,
    enums.MessageMediaType.DOCUMENT,
    enums.MessageMediaType.ANIMATION,
    enums.MessageMediaType.AUDIO,
    enums.MessageMediaType.VOICE,
    enums.MessageMediaType.VIDEO_NOTE,
    enums.MessageMediaType.STICKER,
}

async def download_channel_media(client: Client, channel: str, acc_idx: int):
    try:
        chat = await client.get_chat(channel)
        chat_title = getattr(chat, "title", str(channel))
        chat_id    = chat.id
    except (UsernameInvalid, UsernameNotOccupied, ChannelInvalid,
            PeerIdInvalid, ValueError, KeyError) as e:
        await notify_me(client, f"❌ Канал не найден: {channel}\n{e}")
        return
    except ChatAdminInviteRequired:
        await notify_me(client, f"❌ Нет доступа (нужно быть участником): {channel}")
        return
    except Exception as e:
        await notify_me(client, f"❌ Ошибка при получении канала {channel}:\n{e}")
        return

    me_id = me_ids[acc_idx]
    await notify_me(client,
        f"📥 Начинаю скачивание медиа из «{chat_title}»\n"
        f"Отправлю сюда без указания автора.\n"
        f"(Для остановки: /stopmedia)")

    total = 0
    failed = 0

    try:
        async for msg in client.get_chat_history(chat_id):
            if not msg.media or msg.media not in MEDIA_TYPES:
                continue

            ok = await copy_or_reupload(client, chat_id, msg, me_id)
            if ok:
                total += 1
            else:
                failed += 1

            if total > 0 and total % 10 == 0:
                await notify_me(client, f"📥 «{chat_title}»: {total} файлов…")

            await asyncio.sleep(0.5)

    except asyncio.CancelledError:
        await notify_me(client, f"⛔️ Скачивание «{chat_title}» остановлено.\nСкопировано: {total}, ошибок: {failed}")
        return
    except Exception as e:
        await notify_me(client, f"❌ Ошибка во время скачивания:\n{e}\nСкопировано: {total}")
        return
    finally:
        media_tasks[acc_idx] = None

    await notify_me(client,
        f"✅ Готово! «{chat_title}»\n"
        f"📁 Скопировано: {total}\n"
        f"❌ Ошибок: {failed}")

# ─── Spam loop ──────────────────────────────────────────────────────────────────
async def spam_loop(client, chat_id, chat_title, text, acc_idx, interval_sec):
    interval_min = interval_sec // 60
    await notify_me(client,
        f"▶️ Спам запущен\n📍 {chat_title}\n"
        f"⏱ Каждые {interval_min} мин.\n"
        f"💬 {text[:100]}{'…' if len(text)>100 else ''}")

    consecutive_errors = 0
    current_interval = interval_sec
    stop_reason = None

    try:
        while True:
            try:
                await client.send_message(chat_id, text)
                consecutive_errors = 0
                await asyncio.sleep(current_interval)
            except SlowmodeWait as e:
                wait = e.value + 1
                if wait > current_interval:
                    current_interval = wait
                await asyncio.sleep(wait)
            except UserBannedInChannel:
                stop_reason = "🚫 Аккаунт забанен в группе/канале"; break
            except ChatWriteForbidden:
                stop_reason = "🔇 Нет прав на отправку"; break
            except ChatAdminRequired:
                stop_reason = "👮 Требуются права администратора"; break
            except UserDeactivated:
                stop_reason = "💀 Аккаунт деактивирован Telegram"; break
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except asyncio.CancelledError:
                stop_reason = "⛔️ Остановлен командой /stopspam"; raise
            except RPCError as e:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    stop_reason = f"❌ Много ошибок: {e}"; break
                await asyncio.sleep(10)
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    stop_reason = f"❌ Неожиданная ошибка: {e}"; break
                await asyncio.sleep(10)
    except asyncio.CancelledError:
        pass
    finally:
        spam_tasks[acc_idx].pop(chat_id, None)
        await notify_me(client,
            f"⏹ Спам остановлен\n📍 {chat_title}\n"
            f"📌 {stop_reason or '⛔️ Остановлен командой /stopspam'}")

# ─── Handlers ───────────────────────────────────────────────────────────────────
def build_handlers(client: Client, acc_idx: int):
    global auto_msg1, auto_msg2

    @client.on_message(filters.command("spam", prefixes="/") & filters.outgoing)
    async def cmd_spam(c: Client, msg: Message):
        args = msg.text.split(maxsplit=1)
        if len(args) < 2:
            await msg.reply("Использование:\n  /spam текст\n  /spam 5с текст\n1с = 1 минута.")
            return
        rest = args[1]
        interval_min = 1
        parts = rest.split(maxsplit=1)
        if parts[0].endswith("с") and parts[0][:-1].isdigit():
            interval_min = int(parts[0][:-1])
            if len(parts) < 2:
                await msg.reply("Укажи текст: /spam 3с текст"); return
            spam_text = parts[1]
        else:
            spam_text = rest
        chat_id = msg.chat.id
        chat_title = getattr(msg.chat, "title", None) or getattr(msg.chat, "first_name", None) or str(chat_id)
        old = spam_tasks[acc_idx].get(chat_id)
        if old and not old.done():
            old.cancel(); await asyncio.sleep(0.3)
        spam_tasks[acc_idx][chat_id] = asyncio.create_task(
            spam_loop(c, chat_id, chat_title, spam_text, acc_idx, interval_min * 60))
        try: await msg.delete()
        except Exception: pass

    @client.on_message(filters.command("stopspam", prefixes="/") & filters.outgoing)
    async def cmd_stopspam(c: Client, msg: Message):
        task = spam_tasks[acc_idx].get(msg.chat.id)
        if task and not task.done():
            task.cancel()
        else:
            await msg.reply("Нет активного спама.")
            return
        try: await msg.delete()
        except Exception: pass

    @client.on_message(filters.command("stopmedia", prefixes="/") & filters.outgoing)
    async def cmd_stopmedia(c: Client, msg: Message):
        task = media_tasks.get(acc_idx)
        if task and not task.done():
            task.cancel()
            await msg.reply("⛔️ Скачивание остановлено.")
        else:
            await msg.reply("Нет активного скачивания.")
        try: await msg.delete()
        except Exception: pass

    @client.on_message(filters.command("setmsg1", prefixes="/") & filters.outgoing)
    async def cmd_setmsg1(c: Client, msg: Message):
        global auto_msg1
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply(f"1-е:\n{auto_msg1 or '(не задано)'}\n\nИзменить: /setmsg1 текст"); return
        auto_msg1 = parts[1]
        await msg.reply(f"✅ 1-е сообщение:\n{auto_msg1}")

    @client.on_message(filters.command("setmsg2", prefixes="/") & filters.outgoing)
    async def cmd_setmsg2(c: Client, msg: Message):
        global auto_msg2
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply(f"2-е:\n{auto_msg2 or '(не задано)'}\n\nИзменить: /setmsg2 текст"); return
        auto_msg2 = parts[1]
        await msg.reply(f"✅ 2-е сообщение:\n{auto_msg2}")

    @client.on_message(filters.command("msgs", prefixes="/") & filters.outgoing)
    async def cmd_msgs(c: Client, msg: Message):
        m1 = auto_msg1 or "_(не задано)_"
        m2 = auto_msg2 or "_(не задано)_"
        await msg.reply(f"📨 **Автоответчик:**\n\n**1-е:**\n{m1}\n\n**2-е:**\n{m2}")

    @client.on_message(filters.command("getmedia", prefixes="/") & filters.outgoing)
    async def cmd_getmedia(c: Client, msg: Message):
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply(
                "Использование: /getmedia <ссылка или @юзернейм>\n\n"
                "Примеры:\n"
                "  /getmedia https://t.me/channelname\n"
                "  /getmedia @channelname\n"
                "  /getmedia -1001234567890"
            )
            return
        channel = parse_channel(parts[1].strip())
        if not channel:
            await msg.reply("❌ Не могу распознать ссылку/username канала.")
            return
        existing = media_tasks.get(acc_idx)
        if existing and not existing.done():
            await msg.reply("⚠️ Уже идёт скачивание. Останови: /stopmedia")
            return
        media_tasks[acc_idx] = asyncio.create_task(
            download_channel_media(c, channel, acc_idx))
        try: await msg.delete()
        except Exception: pass

        # Saved Messages: detect channel link → start download
    _media_lock = asyncio.Lock()

    @client.on_message(filters.outgoing & filters.private)
    async def saved_msg_handler(c: Client, msg: Message):
        try:
            if msg.chat.id != me_ids.get(acc_idx):
                return
            if msg.text and msg.text.startswith("/"):
                return
            text = msg.text or msg.caption or ""
            channel = parse_channel(text)
            if not channel:
                return

            # Lock prevents two simultaneous downloads from double-tap
            if _media_lock.locked():
                await notify_me(c, "⚠️ Уже идёт скачивание. Останови: /stopmedia")
                return
            existing = media_tasks.get(acc_idx)
            if existing and not existing.done():
                await notify_me(c, "⚠️ Уже идёт скачивание. Останови: /stopmedia")
                return

            async def _run():
                async with _media_lock:
                    await download_channel_media(c, channel, acc_idx)

            media_tasks[acc_idx] = asyncio.create_task(_run())
        except Exception as e:
            logger.error(f"[acc{acc_idx}] saved_msg_handler error: {e}")

    # Private DM auto-responder — only real humans
    @client.on_message(
        filters.private & filters.incoming & ~filters.me & ~filters.bot & ~filters.service
    )
    async def auto_reply(c: Client, msg: Message):
        try:
            if not msg.from_user:
                return
            user_id = msg.from_user.id
            # Strict guard: only positive real-user IDs, not bots
            if user_id <= 0 or msg.from_user.is_bot:
                return

            # In-memory flood detection (no get_chat_history, no peer resolution)
            recent = user_recent[acc_idx]
            if user_id not in recent:
                recent[user_id] = []
            fp = ("__sticker__" if msg.sticker
                  else (msg.text or "").strip()
                  or f"__media__")
            history = recent[user_id]
            history.append(fp)
            if len(history) > FLOOD_THRESHOLD:
                history.pop(0)
            if len(history) >= FLOOD_THRESHOLD and len(set(history)) == 1:
                await safe_block_and_delete(c, user_id, acc_idx)
                recent.pop(user_id, None)
                return

            # Auto-reply only if both texts are set
            seen = first_message_seen[acc_idx]
            if user_id not in seen and auto_msg1 is not None and auto_msg2 is not None:
                seen.add(user_id)
                try:
                    await c.send_message(user_id, auto_msg1)
                    await c.send_chat_action(user_id, "typing")
                    await asyncio.sleep(5)
                    await c.send_message(user_id, auto_msg2)
                except (UserIsBlocked, PeerFlood, PeerIdInvalid,
                        InputUserDeactivated, UserDeactivated,
                        ValueError, KeyError) as e:
                    logger.debug(f"[acc{acc_idx}] auto-reply skip {user_id}: {e}")
                    return
                except Exception as e:
                    logger.warning(f"[acc{acc_idx}] auto-reply {user_id}: {e}")
                    return
                await safe_mute_and_archive(c, user_id, acc_idx)
        except Exception as e:
            logger.error(f"[acc{acc_idx}] unhandled in auto_reply: {e}")

# ─── Main ───────────────────────────────────────────────────────────────────────
async def main():
    sessions = load_sessions()
    if not sessions:
        logger.error("Не найдено SESSION_STRING! Добавь в переменные Railway.")
        return

    logger.info(f"Запускаю {len(sessions)} аккаунт(ов)...")
    clients = []
    for idx, session_string in enumerate(sessions):
        spam_tasks[idx]         = {}
        first_message_seen[idx] = set()
        user_recent[idx]        = {}
        media_tasks[idx]        = None
        c = make_client(session_string, idx)
        build_handlers(c, idx)
        clients.append(c)

    for idx, c in enumerate(clients):
        await c.start()
        me = await c.get_me()
        me_ids[idx] = me.id
        logger.info(f"✅ Подключён: {me.first_name} (@{me.username})")

    logger.info("Все аккаунты запущены.")
    await asyncio.gather(*[asyncio.Event().wait() for _ in clients])

if __name__ == "__main__":
    asyncio.run(main())
