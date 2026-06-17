import asyncio
import os
import re
import logging
from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message, InputMediaPhoto, InputMediaVideo,
    InputMediaDocument, InputMediaAudio, InputMediaAnimation
)
from pyrogram.errors import (
    FloodWait, ChatWriteForbidden, SlowmodeWait,
    UserIsBlocked, PeerFlood, ChatAdminRequired,
    UserBannedInChannel, RPCError, PeerIdInvalid,
    InputUserDeactivated, UserDeactivated,
    ChannelInvalid, UsernameInvalid, UsernameNotOccupied,
    ChatAdminInviteRequired, ChatForwardsRestricted
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

API_ID   = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
FLOOD_THRESHOLD = 5

def load_sessions() -> list[str]:
    sessions = []
    if s := os.environ.get("SESSION_STRING"):
        sessions.append(s)
    i = 1
    while s := os.environ.get(f"SESSION_STRING{i}"):
        sessions.append(s); i += 1
    return sessions

spam_tasks:         dict[int, dict[int, asyncio.Task]] = {}
first_message_seen: dict[int, set[int]]               = {}
user_recent:        dict[int, dict[int, list[str]]]   = {}
media_tasks:        dict[int, asyncio.Task | None]    = {}
me_ids:             dict[int, int]                    = {}
auto_msg1: str | None = None
auto_msg2: str | None = None

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

def make_client(session_string: str, idx: int) -> Client:
    return Client(
        name=f"account_{idx}", api_id=API_ID, api_hash=API_HASH,
        session_string=session_string, in_memory=True,
    )

MEDIA_TYPES = {
    enums.MessageMediaType.PHOTO, enums.MessageMediaType.VIDEO,
    enums.MessageMediaType.DOCUMENT, enums.MessageMediaType.ANIMATION,
    enums.MessageMediaType.AUDIO, enums.MessageMediaType.VOICE,
    enums.MessageMediaType.VIDEO_NOTE, enums.MessageMediaType.STICKER,
}

async def notify_me(client: Client, text: str):
    try:
        await client.send_message("me", text)
    except Exception as e:
        logger.warning(f"notify_me: {e}")

async def safe_block_and_delete(client: Client, user_id: int, acc_idx: int):
    for fn in [client.block_user, client.delete_chat_history]:
        try: await fn(user_id)
        except Exception as e: logger.debug(f"[acc{acc_idx}] {fn.__name__} {user_id}: {e}")

async def safe_mute_and_archive(client: Client, user_id: int, acc_idx: int):
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
    except Exception as e:
        logger.debug(f"[acc{acc_idx}] mute {user_id}: {e}")
    try:
        await client.archive_chats([user_id])
    except Exception as e:
        logger.debug(f"[acc{acc_idx}] archive {user_id}: {e}")

# ─── Download single file → local path or None ───────────────────────────────────
async def download_one(client: Client, msg: Message) -> str | None:
    """file_name="/tmp/" → Pyrogram дописывает правильное расширение (.jpg/.mp4/etc.)"""
    for attempt in range(3):
        try:
            path = await asyncio.wait_for(
                client.download_media(msg, file_name="/tmp/"),
                timeout=180
            )
            if path and os.path.exists(path):
                return path
        except asyncio.TimeoutError:
            logger.warning(f"download timeout msg {msg.id} attempt {attempt+1}")
            await asyncio.sleep(5)
        except FloodWait as e:
            await asyncio.sleep(e.value + 2)
        except Exception as e:
            logger.warning(f"download_one {msg.id} attempt {attempt+1}: {e}")
            await asyncio.sleep(3)
    return None

def cleanup(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try: os.remove(p)
            except Exception: pass

async def send_one_file(client: Client, msg: Message, path: str, dst: int) -> bool:
    try:
        if msg.photo:           await client.send_photo(dst, path, caption="")
        elif msg.video:         await client.send_video(dst, path, caption="",
                                    duration=msg.video.duration,
                                    width=msg.video.width, height=msg.video.height)
        elif msg.animation:     await client.send_animation(dst, path, caption="")
        elif msg.document:      await client.send_document(dst, path, caption="")
        elif msg.audio:         await client.send_audio(dst, path, caption="",
                                    duration=msg.audio.duration)
        elif msg.voice:         await client.send_voice(dst, path, duration=msg.voice.duration)
        elif msg.video_note:    await client.send_video_note(dst, path, duration=msg.video_note.duration)
        elif msg.sticker:       await client.send_sticker(dst, path)
        else:                   return False
        return True
    except FloodWait as e:
        await asyncio.sleep(e.value + 2); return False
    except Exception as e:
        logger.warning(f"send_one_file {msg.id}: {e}"); return False

async def transfer_single(client: Client, src: int, msg: Message, dst: int) -> bool:
    try:
        await client.copy_message(chat_id=dst, from_chat_id=src, message_id=msg.id, caption="")
        return True
    except ChatForwardsRestricted:
        pass
    except FloodWait as e:
        await asyncio.sleep(e.value + 2); return False
    except Exception:
        pass
    path = await download_one(client, msg)
    if not path: return False
    try:    return await send_one_file(client, msg, path, dst)
    finally: cleanup(path)

async def transfer_album(client: Client, src: int, msgs: list[Message], dst: int) -> tuple[int, int]:
    if not msgs: return 0, 0
    msgs = sorted(msgs, key=lambda m: m.id)

    downloaded: list[tuple[Message, str]] = []
    for msg in msgs:
        path = await download_one(client, msg)
        if path:
            downloaded.append((msg, path))
        else:
            logger.warning(f"album: skip msg {msg.id}")
        await asyncio.sleep(0.5)

    if not downloaded: return 0, len(msgs)

    if len(downloaded) == 1:
        msg, path = downloaded[0]
        try:    ok = await send_one_file(client, msg, path, dst); return (1 if ok else 0), len(msgs) - (1 if ok else 0)
        finally: cleanup(path)

    media_list, paths_to_clean = [], []
    for msg, path in downloaded:
        paths_to_clean.append(path)
        if msg.photo:       media_list.append(InputMediaPhoto(path))
        elif msg.video:     media_list.append(InputMediaVideo(path))
        elif msg.animation: media_list.append(InputMediaAnimation(path))
        elif msg.audio:     media_list.append(InputMediaAudio(path))
        else:               media_list.append(InputMediaDocument(path))

    sent_total = 0
    try:
        for i in range(0, len(media_list), 10):
            chunk = media_list[i:i+10]
            chunk_msgs = downloaded[i:i+len(chunk)]
            for attempt in range(3):
                try:
                    await client.send_media_group(dst, chunk)
                    sent_total += len(chunk); break
                except FloodWait as e:
                    await asyncio.sleep(e.value + 2)
                except Exception as e:
                    logger.warning(f"send_media_group attempt {attempt+1}: {e}")
                    if attempt == 2:
                        for j, (m_obj, _) in enumerate(chunk_msgs):
                            ok = await send_one_file(client, m_obj, paths_to_clean[i+j], dst)
                            if ok: sent_total += 1
                    await asyncio.sleep(3)
    finally:
        cleanup(*paths_to_clean)
    return sent_total, len(msgs) - sent_total

# ─── Channel downloader with auto-resume on disconnect ───────────────────────────
async def download_channel_media(client: Client, channel: str, acc_idx: int):
    try:
        chat = await client.get_chat(channel)
        chat_title = getattr(chat, "title", str(channel))
        chat_id = chat.id
    except (UsernameInvalid, UsernameNotOccupied, ChannelInvalid, PeerIdInvalid, ValueError, KeyError) as e:
        await notify_me(client, f"❌ Канал не найден: {channel}\n{e}"); return
    except ChatAdminInviteRequired:
        await notify_me(client, f"❌ Нет доступа: {channel}"); return
    except Exception as e:
        await notify_me(client, f"❌ Ошибка: {e}"); return

    me_id = me_ids[acc_idx]
    await notify_me(client,
        f"📥 Качаю медиа из «{chat_title}»\n"
        f"Без подписей. Альбомы целиком.\n"
        f"При обрыве — автоматически продолжу.\n"
        f"Остановить: /stopmedia")

    total = 0
    failed = 0
    resume_offset = 0  # 0 = с самого нового; потом = msg.id последнего обработанного

    for retry in range(30):
        if retry > 0:
            wait = min(30 * retry, 300)
            await notify_me(client,
                f"⚠️ Обрыв #{retry}, жду {wait}с…\n"
                f"Продолжу с позиции {total} файлов.")
            await asyncio.sleep(wait)
            try:
                await client.get_me()  # проверяем что соединение живо
            except Exception:
                continue

        cur_gid: int | None = None
        cur_album: list[Message] = []
        last_id = resume_offset

        async def flush_album():
            nonlocal total, failed, cur_gid, cur_album
            if cur_album:
                ok, err = await transfer_album(client, chat_id, cur_album, me_id)
                total += ok; failed += err
                if total > 0 and total % 20 == 0:
                    await notify_me(client, f"📥 «{chat_title}»: {total} файлов…")
            cur_gid = None; cur_album = []

        try:
            async for msg in client.get_chat_history(chat_id, offset_id=resume_offset):
                last_id = msg.id

                if not msg.media or msg.media not in MEDIA_TYPES:
                    await flush_album(); continue

                gid = getattr(msg, "media_group_id", None)
                if gid:
                    if gid == cur_gid: cur_album.append(msg)
                    else:
                        await flush_album()
                        cur_gid = gid; cur_album = [msg]
                    continue
                else:
                    await flush_album()
                    ok = await transfer_single(client, chat_id, msg, me_id)
                    if ok:
                        total += 1
                        if total % 20 == 0:
                            await notify_me(client, f"📥 «{chat_title}»: {total} файлов…")
                    else:
                        failed += 1

                await asyncio.sleep(0.8)  # 0.8с между файлами — не перегружаем DC

            await flush_album()
            break  # завершили без ошибок

        except asyncio.CancelledError:
            await flush_album()
            await notify_me(client, f"⛔️ Остановлено.\nСкопировано: {total} | Ошибок: {failed}")
            media_tasks[acc_idx] = None; return
        except Exception as e:
            logger.error(f"[acc{acc_idx}] download loop error: {type(e).__name__}: {e}")
            resume_offset = last_id  # продолжим со следующего за последним обработанным
            continue
    else:
        await notify_me(client, f"❌ Слишком много обрывов, остановил.\nСкопировано: {total} | Ошибок: {failed}")
        media_tasks[acc_idx] = None; return

    media_tasks[acc_idx] = None
    await notify_me(client, f"✅ Готово! «{chat_title}»\n📁 Файлов: {total} | ❌ Ошибок: {failed}")

# ─── Spam loop ───────────────────────────────────────────────────────────────────
async def spam_loop(client, chat_id, chat_title, text, acc_idx, interval_sec):
    await notify_me(client,
        f"▶️ Спам запущен\n📍 {chat_title}\n"
        f"⏱ Каждые {interval_sec//60} мин.\n"
        f"💬 {text[:100]}{'…' if len(text)>100 else ''}")
    consecutive_errors = 0; current_interval = interval_sec; stop_reason = None
    try:
        while True:
            try:
                await client.send_message(chat_id, text)
                consecutive_errors = 0; await asyncio.sleep(current_interval)
            except SlowmodeWait as e:
                wait = e.value + 1
                if wait > current_interval: current_interval = wait
                await asyncio.sleep(wait)
            except UserBannedInChannel: stop_reason = "🚫 Аккаунт забанен"; break
            except ChatWriteForbidden:  stop_reason = "🔇 Нет прав"; break
            except ChatAdminRequired:   stop_reason = "👮 Нужны права админа"; break
            except UserDeactivated:     stop_reason = "💀 Аккаунт деактивирован"; break
            except FloodWait as e:      await asyncio.sleep(e.value)
            except asyncio.CancelledError: stop_reason = "⛔️ /stopspam"; raise
            except RPCError as e:
                consecutive_errors += 1
                if consecutive_errors >= 5: stop_reason = f"❌ {e}"; break
                await asyncio.sleep(10)
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= 5: stop_reason = f"❌ {e}"; break
                await asyncio.sleep(10)
    except asyncio.CancelledError: pass
    finally:
        spam_tasks[acc_idx].pop(chat_id, None)
        await notify_me(client, f"⏹ Спам остановлен\n📍 {chat_title}\n{stop_reason or '⛔️ /stopspam'}")

# ─── Handlers ────────────────────────────────────────────────────────────────────
def build_handlers(client: Client, acc_idx: int):
    global auto_msg1, auto_msg2
    my_id = me_ids[acc_idx]

    def _is_mine(_, __, msg: Message) -> bool:
        return bool(msg.outgoing) or bool(msg.from_user and msg.from_user.id == my_id)
    mine = filters.create(_is_mine)

    @client.on_message(filters.command("spam", prefixes="/") & mine)
    async def cmd_spam(c, msg):
        args = msg.text.split(maxsplit=1)
        if len(args) < 2: await msg.reply("Использование:\n  /spam текст\n  /spam 5с текст"); return
        rest = args[1]; interval_min = 1
        parts = rest.split(maxsplit=1)
        if parts[0].endswith("с") and parts[0][:-1].isdigit():
            interval_min = int(parts[0][:-1])
            if len(parts) < 2: await msg.reply("Укажи текст: /spam 3с текст"); return
            spam_text = parts[1]
        else: spam_text = rest
        chat_id = msg.chat.id
        chat_title = getattr(msg.chat, "title", None) or getattr(msg.chat, "first_name", None) or str(chat_id)
        old = spam_tasks[acc_idx].get(chat_id)
        if old and not old.done(): old.cancel(); await asyncio.sleep(0.3)
        spam_tasks[acc_idx][chat_id] = asyncio.create_task(
            spam_loop(c, chat_id, chat_title, spam_text, acc_idx, interval_min * 60))
        try: await msg.delete()
        except Exception: pass

    @client.on_message(filters.command("stopspam", prefixes="/") & mine)
    async def cmd_stopspam(c, msg):
        task = spam_tasks[acc_idx].get(msg.chat.id)
        if task and not task.done(): task.cancel()
        else: await msg.reply("Нет активного спама."); return
        try: await msg.delete()
        except Exception: pass

    @client.on_message(filters.command("getmedia", prefixes="/") & mine)
    async def cmd_getmedia(c, msg):
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2: await msg.reply("Использование:\n  /getmedia @channel\n  /getmedia https://t.me/channel"); return
        channel = parse_channel(parts[1].strip())
        if not channel: await msg.reply("❌ Не могу распознать ссылку."); return
        existing = media_tasks.get(acc_idx)
        if existing and not existing.done(): await msg.reply("⚠️ Уже идёт. Останови: /stopmedia"); return
        media_tasks[acc_idx] = asyncio.create_task(download_channel_media(c, channel, acc_idx))
        try: await msg.delete()
        except Exception: pass

    @client.on_message(filters.command("stopmedia", prefixes="/") & mine)
    async def cmd_stopmedia(c, msg):
        task = media_tasks.get(acc_idx)
        if task and not task.done():
            task.cancel(); await msg.reply("⛔️ Скачивание остановлено.")
        else: await msg.reply("Нет активного скачивания.")
        try: await msg.delete()
        except Exception: pass

    @client.on_message(filters.command("setmsg1", prefixes="/") & mine)
    async def cmd_setmsg1(c, msg):
        global auto_msg1
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2: await msg.reply(f"1-е:\n{auto_msg1 or '(не задано)'}\n\nИзменить: /setmsg1 текст"); return
        auto_msg1 = parts[1]; await msg.reply(f"✅ 1-е:\n{auto_msg1}")

    @client.on_message(filters.command("setmsg2", prefixes="/") & mine)
    async def cmd_setmsg2(c, msg):
        global auto_msg2
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2: await msg.reply(f"2-е:\n{auto_msg2 or '(не задано)'}\n\nИзменить: /setmsg2 текст"); return
        auto_msg2 = parts[1]; await msg.reply(f"✅ 2-е:\n{auto_msg2}")

    @client.on_message(filters.command("msgs", prefixes="/") & mine)
    async def cmd_msgs(c, msg):
        m1 = auto_msg1 or "_(не задано)_"; m2 = auto_msg2 or "_(не задано)_"
        await msg.reply(f"📨 **Автоответчик:**\n\n**1-е:**\n{m1}\n\n**2-е:**\n{m2}")

    @client.on_message(filters.private & filters.incoming & ~filters.me & ~filters.bot & ~filters.service)
    async def auto_reply(c, msg):
        try:
            if not msg.from_user: return
            user_id = msg.from_user.id
            if user_id <= 0 or user_id == my_id or msg.from_user.is_bot: return
            recent = user_recent[acc_idx]
            if user_id not in recent: recent[user_id] = []
            fp = "__sticker__" if msg.sticker else (msg.text or "").strip() or "__media__"
            history = recent[user_id]
            history.append(fp)
            if len(history) > FLOOD_THRESHOLD: history.pop(0)
            if len(history) >= FLOOD_THRESHOLD and len(set(history)) == 1:
                await safe_block_and_delete(c, user_id, acc_idx); recent.pop(user_id, None); return
            seen = first_message_seen[acc_idx]
            if user_id not in seen and auto_msg1 is not None and auto_msg2 is not None:
                seen.add(user_id)
                try:
                    await c.send_message(user_id, auto_msg1)
                    await c.send_chat_action(user_id, "typing")
                    await asyncio.sleep(5)
                    await c.send_message(user_id, auto_msg2)
                except (UserIsBlocked, PeerFlood, PeerIdInvalid, InputUserDeactivated,
                        UserDeactivated, ValueError, KeyError) as e:
                    logger.debug(f"[acc{acc_idx}] auto-reply skip {user_id}: {e}"); return
                except Exception as e:
                    logger.warning(f"[acc{acc_idx}] auto-reply {user_id}: {e}"); return
                await safe_mute_and_archive(c, user_id, acc_idx)
        except Exception as e:
            logger.error(f"[acc{acc_idx}] auto_reply crash: {e}")

# ─── Main ────────────────────────────────────────────────────────────────────────
async def main():
    sessions = load_sessions()
    if not sessions: logger.error("SESSION_STRING не найден!"); return
    logger.info(f"Запускаю {len(sessions)} аккаунт(ов)...")
    clients = []
    for idx, session_string in enumerate(sessions):
        spam_tasks[idx] = {}; first_message_seen[idx] = set()
        user_recent[idx] = {}; media_tasks[idx] = None
        c = make_client(session_string, idx)
        await c.start()
        me = await c.get_me()
        me_ids[idx] = me.id
        logger.info(f"✅ {me.first_name} (@{me.username}) | id={me.id}")
        build_handlers(c, idx)
        clients.append(c)
    logger.info("Все аккаунты запущены.")
    await asyncio.gather(*[asyncio.Event().wait() for _ in clients])

if __name__ == "__main__":
    asyncio.run(main())
