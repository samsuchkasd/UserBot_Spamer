"""
Доп. команды для развлечения в группах и лички: игры, генераторы, картинки,
слежка за сменой имени/ника/фото ("сангмата").

Подключается из main.py:  fun.register(client, acc_idx, mine, my_id)
Ничего из main.py не меняет и не трогает существующие команды/логику.
"""

import ast
import asyncio
import hashlib
import io
import logging
import operator as op
import os
import random
import re
import time
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.errors import FloodWait

try:
    from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageChops
    _PIL_OK = True
except Exception:
    _PIL_OK = False

try:
    from art import text2art
    _ART_OK = True
except Exception:
    _ART_OK = False

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_FONT_BOLD = os.path.join(_BASE_DIR, "assets", "fonts", "PT_Sans-Web-Bold.ttf")
_FONT_REGULAR = os.path.join(_BASE_DIR, "assets", "fonts", "PT_Sans-Web-Regular.ttf")

_START_TIME = time.time()

# ── общее состояние слежки (на аккаунт) ────────────────────────────────────
watch_state: dict[int, dict[int, dict]] = {}
watch_tasks: dict[int, asyncio.Task] = {}

_DICE_EMOJI = {
    "dice": "🎲",
    "dart": "🎯",
    "bball": "🏀",
    "foot": "⚽",
    "bowl": "🎳",
    "slot": "🎰",
}

EIGHT_BALL = [
    "Да.", "Нет.", "Определённо да.", "Определённо нет.", "Возможно.",
    "Весьма вероятно.", "Маловероятно.", "Спроси позже.", "Даже не думай.",
    "100% да!", "Сложно сказать.", "Мой ответ — нет.", "Знаки говорят «да».",
    "Сконцентрируйся и спроси снова.", "Не рассчитывай на это.", "Бесспорно.",
    "Источники говорят нет.", "Как ни крути — да.", "Сомневаюсь.", "Однозначно!",
]

JOKES = [
    "— Официант, у меня в супе муха!\n— Не переживайте, больше она есть не будет.",
    "Программист — это устройство, превращающее кофе в баги.",
    "— Как дела?\n— Как в бинарном коде: то 0, то 1.",
    "Оптимист изучает английский, пессимист — китайский, а реалист — автомат Калашникова.",
    "— Почему программисты путают Хэллоуин и Рождество?\n— Потому что OCT 31 = DEC 25.",
    "Уверенность — это когда ты пишешь код без комментариев, а потом сам в нём разбираешься через полгода.",
    "Есть 10 типов людей: те, кто понимает двоичный код, и те, кто нет.",
    "— Доктор, у меня botox.\n— Может, badcode?",
    "Самый страшный сон разработчика: «работает на моей машине».",
    "Жизнь как git: иногда нужно просто сделать force push и забыть.",
    "— Что сказал 0 восьмёрке?\n— Классный пояс!",
    "Лучший баг — это тот, который воспроизводится только у заказчика.",
    "Если код работает с первого раза — не верь ему.",
    "Опытный админ спит спокойно, потому что бэкапы есть. Но не проверял.",
    "Всё, что можно сломать — сломается. Особенно в пятницу вечером перед релизом.",
]

FACTS = [
    "Осьминоги имеют три сердца.",
    "Мёд не портится — археологи находили съедобный мёд возрастом 3000 лет.",
    "Бананы — это ягоды, а клубника — нет.",
    "Один день на Венере длиннее, чем год на Венере.",
    "У улиток около 14 000 зубов.",
    "Эйфелева башня летом становится выше почти на 15 см из-за расширения металла.",
    "Сердце синего кита размером с небольшой автомобиль.",
    "Акулы существуют дольше деревьев.",
    "Человеческий нос способен различать более триллиона запахов.",
    "Wi-Fi не расшифровывается никак — это просто торговая марка.",
    "Скорлупа страусиного яйца выдерживает вес взрослого человека.",
    "В космосе нельзя плакать — слёзы не стекают из-за невесомости.",
    "Кузнечики слышат коленями.",
    "Первое сообщение по email отправили в 1971 году.",
    "На Земле больше деревьев, чем звёзд в Млечном Пути.",
]

SHIP_LOW = ["Не сложилось… 💔", "Скорее нет, чем да.", "Лучше остаться друзьями."]
SHIP_MID = ["Есть шанс!", "Может получиться, если постараться.", "50 на 50, как повезёт."]
SHIP_HIGH = ["Идеальная пара! 💞", "Это судьба!", "Хоть сейчас под венец!"]

FUN_HELP_TEXT = (
    "🎉 **Доп. команды:**\n\n"
    "🎲 **Игры:**\n"
    "/dice /dart /bball /bowl /slot /foot — анимированные кости Telegram\n"
    "/rolldice 2d6+3 — бросок кубиков по формуле NdM[+K]\n\n"
    "🔮 **Развлечения:**\n"
    "/8ball вопрос — магический шар\n"
    "/coin — орёл/решка\n"
    "/joke — случайный анекдот\n"
    "/fact — случайный факт\n"
    "/rate что-то — оценить что-то в %\n"
    "/ship Имя1 + Имя2 — совместимость (или ответом + 1 имя)\n"
    "/choose вариант1 | вариант2 — случайный выбор\n\n"
    "✍️ **Текст:**\n"
    "/mock текст — cHeReDoM\n"
    "/reverse текст — задом наперёд\n"
    "/clap текст — 👏 между словами\n"
    "/vaporwave текст — ｖａｐｏｒｗａｖｅ\n"
    "/ascii текст — ASCII-арт (латиница/цифры)\n"
    "/calc выражение — калькулятор\n\n"
    "🖼 **Картинки** (ответом на фото/сообщение):\n"
    "/meme Текст сверху | Текст снизу — мем из фото\n"
    "/fry — «зажарить» фото\n"
    "/glitch — глитч-эффект\n"
    "/quote — превратить текстовое сообщение в карточку-цитату\n\n"
    "👤 **Профиль:**\n"
    "/whois — инфо о пользователе (ответом или @username)\n"
    "/alive — статус юзербота, пинг, аптайм\n\n"
    "👁 **Слежка за пользователем:**\n"
    "/watch @user — следить за сменой имени/ника/фото\n"
    "/unwatch @user — снять слежку\n"
    "/watchlist — список отслеживаемых"
)


# ── мелкие утилиты ──────────────────────────────────────────────────────────
async def _notify_me(client: Client, text: str):
    try:
        await client.send_message("me", text)
    except Exception as e:
        logger.warning(f"fun notify_me: {e}")


def _cleanup(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


def _is_image_message(m) -> bool:
    if not m:
        return False
    if m.photo:
        return True
    if m.document and (m.document.mime_type or "").startswith("image/"):
        return True
    return False


def _deterministic_percent(seed_text: str, salt: str = "") -> int:
    h = hashlib.sha256((seed_text.strip().lower() + salt).encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 101


_DICE_NOTATION_RE = re.compile(r"^(\d{1,2})d(\d{1,4})([+-]\d{1,4})?$", re.IGNORECASE)


def _roll_dice_notation(expr: str):
    m = _DICE_NOTATION_RE.fullmatch(expr.strip())
    if not m:
        return None
    count = int(m.group(1))
    sides = int(m.group(2))
    mod = int(m.group(3)) if m.group(3) else 0
    if count < 1 or count > 50 or sides < 2 or sides > 1000:
        return None
    rolls = [random.randint(1, sides) for _ in range(count)]
    return rolls, mod, sum(rolls) + mod


def _to_fullwidth(text: str) -> str:
    result = []
    for ch in text:
        code = ord(ch)
        if 0x21 <= code <= 0x7E:
            result.append(chr(code - 0x21 + 0xFF01))
        elif ch == " ":
            result.append("\u3000")
        else:
            result.append(ch)
    return "".join(result)


# ── безопасный калькулятор (без eval) ───────────────────────────────────────
_ALLOWED_OPS = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv, ast.Mod: op.mod, ast.Pow: op.pow,
    ast.USub: op.neg, ast.UAdd: op.pos,
}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError("недопустимое значение")
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPS:
        if isinstance(node.op, ast.Pow):
            exponent = _safe_eval(node.right)
            if abs(exponent) > 1000:
                raise ValueError("слишком большая степень")
        left = _safe_eval(node.left)
        right = _safe_eval(node.right)
        return _ALLOWED_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_OPS:
        return _ALLOWED_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("недопустимое выражение")


def safe_calc(expr: str):
    tree = ast.parse(expr, mode="eval")
    return _safe_eval(tree)


# ── картинки (Pillow) ────────────────────────────────────────────────────────
def _font(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _wrap_text(draw, text, font, max_width):
    words = text.split()
    if not words:
        return [""]
    lines, current = [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines[:10]


def _fit_font(draw, text, max_width, start_size, min_size=18):
    size = start_size
    while size > min_size:
        font = _font(_FONT_BOLD, size)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 2
    return _font(_FONT_BOLD, min_size)


def _draw_outlined_text(draw, xy, text, font, fill=(255, 255, 255), outline=(0, 0, 0), outline_width=3):
    x, y = xy
    for dx in (-outline_width, 0, outline_width):
        for dy in (-outline_width, 0, outline_width):
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), text, font=font, fill=outline)
    draw.text((x, y), text, font=font, fill=fill)


def _draw_meme_text(img, top: str, bottom: str):
    W, H = img.size
    draw = ImageDraw.Draw(img)
    margin = int(W * 0.05)
    max_width = W - margin * 2
    if top:
        up = top.upper()
        font = _fit_font(draw, up, max_width, start_size=max(int(H * 0.12), 24))
        bbox = draw.textbbox((0, 0), up, font=font)
        tw = bbox[2] - bbox[0]
        _draw_outlined_text(draw, ((W - tw) / 2, int(H * 0.03) - bbox[1]), up, font)
    if bottom:
        down = bottom.upper()
        font = _fit_font(draw, down, max_width, start_size=max(int(H * 0.12), 24))
        bbox = draw.textbbox((0, 0), down, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        _draw_outlined_text(draw, ((W - tw) / 2, H - int(H * 0.03) - th - bbox[1]), down, font)
    return img


def _deep_fry(img):
    img = ImageEnhance.Color(img).enhance(2.6)
    img = ImageEnhance.Contrast(img).enhance(1.8)
    img = ImageEnhance.Brightness(img).enhance(1.15)
    img = ImageEnhance.Sharpness(img).enhance(3.0)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=8)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def _glitch_effect(img):
    w, h = img.size
    r, g, b = img.split()
    shift = max(4, w // 60)
    r = ImageChops.offset(r, random.randint(-shift, shift), random.randint(-2, 2))
    b = ImageChops.offset(b, random.randint(-shift, shift), random.randint(-2, 2))
    merged = Image.merge("RGB", (r, g, b))
    out = merged.copy()
    for _ in range(max(1, h // 20)):
        y = random.randint(0, max(h - 1, 0))
        slice_h = random.randint(2, 8)
        dx = random.randint(-shift * 2, shift * 2)
        box = (0, y, w, min(y + slice_h, h))
        region = merged.crop(box)
        out.paste(region, (dx, y))
    return out


def _build_quote_card(text: str, author: str, avatar_img):
    W = 900
    pad = 40
    avatar_size = 120
    text_x = pad + avatar_size + 30
    max_text_width = W - text_x - pad

    font_text = _font(_FONT_REGULAR, 34)
    font_author = _font(_FONT_BOLD, 30)
    font_mark = _font(_FONT_BOLD, 70)

    dummy = Image.new("RGB", (10, 10))
    ddraw = ImageDraw.Draw(dummy)
    display_text = text if len(text) <= 400 else text[:400].rstrip() + "…"
    lines = _wrap_text(ddraw, display_text, font_text, max_text_width)
    line_height = 44

    quote_mark_h = 55
    gap_after_text = 24
    author_h = 44
    text_block_height = line_height * len(lines)
    right_col_height = quote_mark_h + text_block_height + gap_after_text + author_h
    H = pad * 2 + max(avatar_size, right_col_height)

    img = Image.new("RGB", (W, H), color=(25, 26, 33))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        shade = 25 + int(15 * (y / max(H - 1, 1)))
        draw.line([(0, y), (W, y)], fill=(shade, shade + 1, shade + 8))

    if avatar_img:
        avatar_img = avatar_img.resize((avatar_size, avatar_size))
        mask = Image.new("L", (avatar_size, avatar_size), 0)
        mdraw = ImageDraw.Draw(mask)
        mdraw.ellipse((0, 0, avatar_size, avatar_size), fill=255)
        img.paste(avatar_img, (pad, pad), mask)
    else:
        draw.ellipse((pad, pad, pad + avatar_size, pad + avatar_size), fill=(90, 100, 140))
        initials = "".join([p[0] for p in author.split()[:2]]).upper() or "?"
        init_font = _font(_FONT_BOLD, 44)
        ibbox = draw.textbbox((0, 0), initials, font=init_font)
        iw, ih = ibbox[2] - ibbox[0], ibbox[3] - ibbox[1]
        draw.text(
            (pad + avatar_size / 2 - iw / 2, pad + avatar_size / 2 - ih / 2 - ibbox[1]),
            initials, font=init_font, fill=(255, 255, 255),
        )

    draw.text((text_x, pad - 12), "\u201c", font=font_mark, fill=(120, 130, 170))

    y = pad + quote_mark_h
    for line in lines:
        draw.text((text_x, y), line, font=font_text, fill=(235, 235, 240))
        y += line_height

    draw.text((text_x, y + gap_after_text - 10), f"— {author}", font=font_author, fill=(160, 170, 210))

    return img


# ── регистрация хендлеров для одного аккаунта ───────────────────────────────
def register(client: Client, acc_idx: int, mine, me_id: int):
    if acc_idx not in watch_state:
        watch_state[acc_idx] = {}

    # ---- игры (анимированные кости Telegram) ----
    for cmd_name, emoji in _DICE_EMOJI.items():
        @client.on_message(filters.command(cmd_name, prefixes="/") & mine)
        async def cmd_dice_game(c, msg, _emoji=emoji):
            try:
                await c.send_dice(msg.chat.id, emoji=_emoji)
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
            except Exception as e:
                logger.warning(f"[acc{acc_idx}] dice {_emoji}: {e}")
            try:
                await msg.delete()
            except Exception:
                pass

    @client.on_message(filters.command("rolldice", prefixes="/") & mine)
    async def cmd_rolldice(c, msg):
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply("Использование: /rolldice 2d6+3"); return
        result = _roll_dice_notation(parts[1])
        if result is None:
            await msg.reply("❌ Формат: NdM[+K], например 3d6+2 (до 50 костей, до 1000 граней)."); return
        rolls, mod, total = result
        text = f"🎲 Бросок {parts[1].strip()}:\n{', '.join(map(str, rolls))}"
        if mod:
            text += f" {'+' if mod > 0 else ''}{mod}"
        text += f"\n**Итого: {total}**"
        await msg.reply(text)

    # ---- развлечения ----
    @client.on_message(filters.command("8ball", prefixes="/") & mine)
    async def cmd_8ball(c, msg):
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply("Использование: /8ball твой вопрос"); return
        await msg.reply(f"🎱 {random.choice(EIGHT_BALL)}")

    @client.on_message(filters.command("coin", prefixes="/") & mine)
    async def cmd_coin(c, msg):
        await msg.reply(random.choice(["🪙 Орёл!", "🪙 Решка!"]))

    @client.on_message(filters.command("joke", prefixes="/") & mine)
    async def cmd_joke(c, msg):
        await msg.reply(random.choice(JOKES))

    @client.on_message(filters.command("fact", prefixes="/") & mine)
    async def cmd_fact(c, msg):
        await msg.reply(f"💡 {random.choice(FACTS)}")

    @client.on_message(filters.command("rate", prefixes="/") & mine)
    async def cmd_rate(c, msg):
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2:
            if msg.reply_to_message and msg.reply_to_message.text:
                subject = msg.reply_to_message.text
            else:
                await msg.reply("Использование: /rate что-то оценить"); return
        else:
            subject = parts[1].strip()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        percent = _deterministic_percent(subject, today)
        bar = "🟩" * (percent // 10) + "⬜" * (10 - percent // 10)
        await msg.reply(f"📊 Оценка «{subject}»: **{percent}%**\n{bar}")

    @client.on_message(filters.command("ship", prefixes="/") & mine)
    async def cmd_ship(c, msg):
        names = []
        if msg.reply_to_message and msg.reply_to_message.from_user:
            names.append(msg.reply_to_message.from_user.first_name)
        parts = msg.text.split(maxsplit=1)
        if len(parts) > 1:
            extra = [p.strip() for p in re.split(r"[+&]| и ", parts[1]) if p.strip()]
            names.extend(extra)
        if len(names) < 2:
            await msg.reply("Использование: /ship Имя1 + Имя2 (или ответом на сообщение + одно имя)"); return
        a, b = names[0], names[1]
        percent = _deterministic_percent(f"{a.lower()}|{b.lower()}")
        if percent < 34:
            phrase = random.choice(SHIP_LOW)
        elif percent < 67:
            phrase = random.choice(SHIP_MID)
        else:
            phrase = random.choice(SHIP_HIGH)
        bar = "🟩" * (percent // 10) + "⬜" * (10 - percent // 10)
        await msg.reply(f"💘 {a} + {b} = **{percent}%**\n{bar}\n{phrase}")

    @client.on_message(filters.command("choose", prefixes="/") & mine)
    async def cmd_choose(c, msg):
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply("Использование: /choose вариант1 | вариант2 | вариант3"); return
        options = [o.strip() for o in re.split(r"\||,", parts[1]) if o.strip()]
        if len(options) < 2:
            await msg.reply("Укажи минимум 2 варианта через | или запятую."); return
        await msg.reply(f"🎯 Мой выбор: **{random.choice(options)}**")

    # ---- текстовые приколы ----
    def _text_source(msg):
        parts = msg.text.split(maxsplit=1)
        if len(parts) > 1:
            return parts[1]
        if msg.reply_to_message and msg.reply_to_message.text:
            return msg.reply_to_message.text
        return None

    @client.on_message(filters.command("mock", prefixes="/") & mine)
    async def cmd_mock(c, msg):
        source = _text_source(msg)
        if not source:
            await msg.reply("Использование: /mock текст (или ответом на сообщение)"); return
        await msg.reply("".join(ch.upper() if i % 2 else ch.lower() for i, ch in enumerate(source)))

    @client.on_message(filters.command("reverse", prefixes="/") & mine)
    async def cmd_reverse(c, msg):
        source = _text_source(msg)
        if not source:
            await msg.reply("Использование: /reverse текст"); return
        await msg.reply(source[::-1])

    @client.on_message(filters.command("clap", prefixes="/") & mine)
    async def cmd_clap(c, msg):
        source = _text_source(msg)
        if not source:
            await msg.reply("Использование: /clap текст"); return
        await msg.reply(" 👏 ".join(source.split()))

    @client.on_message(filters.command("vaporwave", prefixes="/") & mine)
    async def cmd_vaporwave(c, msg):
        source = _text_source(msg)
        if not source:
            await msg.reply("Использование: /vaporwave текст"); return
        await msg.reply(_to_fullwidth(source))

    @client.on_message(filters.command("ascii", prefixes="/") & mine)
    async def cmd_ascii(c, msg):
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply("Использование: /ascii текст (латиница/цифры)"); return
        if not _ART_OK:
            await msg.reply("❌ ASCII-арт временно недоступен."); return
        try:
            art_text = text2art(parts[1].strip()[:20])
        except Exception:
            await msg.reply("❌ Не удалось сгенерировать (используй латинские буквы/цифры)."); return
        if len(art_text) > 4000:
            await msg.reply("❌ Слишком длинный текст."); return
        await msg.reply(f"```\n{art_text}\n```")

    @client.on_message(filters.command("calc", prefixes="/") & mine)
    async def cmd_calc(c, msg):
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply("Использование: /calc 2*(3+4)/7"); return
        expr = parts[1].strip()
        try:
            result = safe_calc(expr)
        except ZeroDivisionError:
            await msg.reply("❌ Деление на ноль."); return
        except Exception:
            await msg.reply("❌ Не удалось вычислить выражение."); return
        await msg.reply(f"🧮 {expr} = **{result}**")

    # ---- картинки ----
    @client.on_message(filters.command("meme", prefixes="/") & mine)
    async def cmd_meme(c, msg):
        if not _PIL_OK:
            await msg.reply("❌ Модуль изображений недоступен."); return
        rep = msg.reply_to_message
        if not _is_image_message(rep):
            await msg.reply("Ответь на фото командой /meme Текст сверху | Текст снизу"); return
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply("Укажи текст: /meme Текст сверху | Текст снизу"); return
        raw = parts[1]
        if "|" in raw:
            top, bottom = [p.strip() for p in raw.split("|", 1)]
        else:
            top, bottom = raw.strip(), ""
        path = await c.download_media(rep, file_name="/tmp/")
        if not path:
            await msg.reply("❌ Не удалось скачать фото."); return
        try:
            img = Image.open(path).convert("RGB")
            img = _draw_meme_text(img, top, bottom)
            buf = io.BytesIO(); buf.name = "meme.jpg"
            img.save(buf, format="JPEG", quality=92); buf.seek(0)
            await c.send_photo(msg.chat.id, buf, reply_to_message_id=rep.id)
        except Exception as e:
            logger.warning(f"[acc{acc_idx}] meme: {e}")
            await msg.reply("❌ Не удалось создать мем.")
        finally:
            _cleanup(path)

    @client.on_message(filters.command("fry", prefixes="/") & mine)
    async def cmd_fry(c, msg):
        if not _PIL_OK:
            await msg.reply("❌ Модуль изображений недоступен."); return
        rep = msg.reply_to_message
        if not _is_image_message(rep):
            await msg.reply("Ответь на фото командой /fry."); return
        path = await c.download_media(rep, file_name="/tmp/")
        if not path:
            await msg.reply("❌ Не удалось скачать фото."); return
        try:
            img = Image.open(path).convert("RGB")
            img = _deep_fry(img)
            buf = io.BytesIO(); buf.name = "fried.jpg"
            img.save(buf, format="JPEG", quality=25); buf.seek(0)
            await c.send_photo(msg.chat.id, buf, reply_to_message_id=rep.id)
        except Exception as e:
            logger.warning(f"[acc{acc_idx}] fry: {e}")
            await msg.reply("❌ Не удалось обработать фото.")
        finally:
            _cleanup(path)

    @client.on_message(filters.command("glitch", prefixes="/") & mine)
    async def cmd_glitch(c, msg):
        if not _PIL_OK:
            await msg.reply("❌ Модуль изображений недоступен."); return
        rep = msg.reply_to_message
        if not _is_image_message(rep):
            await msg.reply("Ответь на фото командой /glitch."); return
        path = await c.download_media(rep, file_name="/tmp/")
        if not path:
            await msg.reply("❌ Не удалось скачать фото."); return
        try:
            img = Image.open(path).convert("RGB")
            img = _glitch_effect(img)
            buf = io.BytesIO(); buf.name = "glitch.jpg"
            img.save(buf, format="JPEG", quality=90); buf.seek(0)
            await c.send_photo(msg.chat.id, buf, reply_to_message_id=rep.id)
        except Exception as e:
            logger.warning(f"[acc{acc_idx}] glitch: {e}")
            await msg.reply("❌ Не удалось обработать фото.")
        finally:
            _cleanup(path)

    @client.on_message(filters.command("quote", prefixes="/") & mine)
    async def cmd_quote(c, msg):
        if not _PIL_OK:
            await msg.reply("❌ Модуль изображений недоступен."); return
        rep = msg.reply_to_message
        if not rep or not (rep.text or rep.caption):
            await msg.reply("Ответь командой /quote на текстовое сообщение."); return
        text = (rep.text or rep.caption or "").strip()
        if not text:
            await msg.reply("В этом сообщении нет текста для цитаты."); return
        author = "Аноним"
        if rep.from_user:
            author = " ".join(filter(None, [rep.from_user.first_name, rep.from_user.last_name])) or "Аноним"
        elif rep.sender_chat:
            author = rep.sender_chat.title or "Канал"

        avatar_img = None
        avatar_path = None
        try:
            if rep.from_user and rep.from_user.photo:
                avatar_path = await c.download_media(rep.from_user.photo.small_file_id, file_name="/tmp/")
                if avatar_path:
                    avatar_img = Image.open(avatar_path).convert("RGB")
        except Exception:
            avatar_img = None

        try:
            img = _build_quote_card(text, author, avatar_img)
            buf = io.BytesIO(); buf.name = "quote.png"
            img.save(buf, format="PNG"); buf.seek(0)
            await c.send_photo(msg.chat.id, buf, reply_to_message_id=rep.id)
        except Exception as e:
            logger.warning(f"[acc{acc_idx}] quote: {e}")
            await msg.reply("❌ Не удалось создать карточку-цитату.")
        finally:
            _cleanup(avatar_path)

    # ---- профиль ----
    @client.on_message(filters.command("whois", prefixes="/") & mine)
    async def cmd_whois(c, msg):
        target = "me"
        if msg.reply_to_message and msg.reply_to_message.from_user:
            target = msg.reply_to_message.from_user.id
        else:
            parts = msg.text.split(maxsplit=1)
            if len(parts) > 1:
                target = parts[1].strip()
        try:
            user = await c.get_users(target)
        except Exception as e:
            await msg.reply(f"❌ Не удалось найти пользователя: {e}"); return
        if isinstance(user, list):
            if not user:
                await msg.reply("❌ Пользователь не найден."); return
            user = user[0]
        full_name = " ".join(filter(None, [user.first_name, user.last_name])) or "—"
        username = f"@{user.username}" if user.username else "—"
        premium = "✨ Да" if getattr(user, "is_premium", False) else "Нет"
        verified = "✅ Да" if getattr(user, "is_verified", False) else "Нет"
        bot_flag = "🤖 Да" if user.is_bot else "Нет"
        status = str(user.status) if getattr(user, "status", None) else "неизвестен"
        text = (
            f"👤 **{full_name}**\n"
            f"🆔 ID: `{user.id}`\n"
            f"🔗 Юзернейм: {username}\n"
            f"🤖 Бот: {bot_flag}\n"
            f"✨ Premium: {premium}\n"
            f"✅ Верифицирован: {verified}\n"
            f"📶 Статус: {status}\n"
            f"🌐 DC: {getattr(user, 'dc_id', '—')}"
        )
        await msg.reply(text)

    @client.on_message(filters.command("alive", prefixes="/") & mine)
    async def cmd_alive(c, msg):
        start = time.monotonic()
        sent = await msg.reply("🏓 Пинг...")
        ping_ms = (time.monotonic() - start) * 1000
        uptime_sec = int(time.time() - _START_TIME)
        days, rem = divmod(uptime_sec, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        uptime_str = f"{days}д {hours}ч {minutes}м {seconds}с" if days else f"{hours}ч {minutes}м {seconds}с"
        try:
            me = await c.get_me()
            name = me.first_name
        except Exception:
            name = "Юзербот"
        await sent.edit(
            f"🟢 **{name} на связи!**\n\n"
            f"🏓 Пинг: `{ping_ms:.0f} мс`\n"
            f"⏱ Аптайм: {uptime_str}\n"
            f"🐍 Pyrogram userbot"
        )

    @client.on_message(filters.command("funhelp", prefixes="/") & mine)
    async def cmd_funhelp(c, msg):
        await msg.reply(FUN_HELP_TEXT)

    # ---- слежка за сменой имени/ника/фото ("сангмата") ----
    async def _resolve_watch_target(c, msg):
        if msg.reply_to_message and msg.reply_to_message.from_user:
            return msg.reply_to_message.from_user.id
        parts = msg.text.split(maxsplit=1)
        if len(parts) > 1:
            return parts[1].strip()
        return None

    @client.on_message(filters.command("watch", prefixes="/") & mine)
    async def cmd_watch(c, msg):
        target = await _resolve_watch_target(c, msg)
        if not target:
            await msg.reply("Использование: /watch @username (или ответом на сообщение пользователя)"); return
        try:
            user = await c.get_users(target)
        except Exception as e:
            await msg.reply(f"❌ Не удалось найти пользователя: {e}"); return
        if isinstance(user, list):
            if not user:
                await msg.reply("❌ Пользователь не найден."); return
            user = user[0]
        photo_id = user.photo.big_file_id if user.photo else None
        watch_state[acc_idx][user.id] = {
            "first_name": user.first_name, "last_name": user.last_name,
            "username": user.username, "photo_id": photo_id,
        }
        display = " ".join(filter(None, [user.first_name, user.last_name])) or str(user.id)
        await msg.reply(f"👁 Слежу за изменениями: **{display}**\nСообщу сюда при смене имени, юзернейма или фото.")

    @client.on_message(filters.command("unwatch", prefixes="/") & mine)
    async def cmd_unwatch(c, msg):
        target = await _resolve_watch_target(c, msg)
        if not target:
            await msg.reply("Использование: /unwatch @username (или ответом на сообщение пользователя)"); return
        try:
            user = await c.get_users(target)
        except Exception as e:
            await msg.reply(f"❌ Не удалось найти пользователя: {e}"); return
        if isinstance(user, list):
            if not user:
                await msg.reply("❌ Пользователь не найден."); return
            user = user[0]
        if watch_state[acc_idx].pop(user.id, None) is not None:
            await msg.reply("✅ Слежка снята.")
        else:
            await msg.reply("Этот пользователь не отслеживается.")

    @client.on_message(filters.command("watchlist", prefixes="/") & mine)
    async def cmd_watchlist(c, msg):
        entries = watch_state.get(acc_idx, {})
        if not entries:
            await msg.reply("Список слежки пуст."); return
        lines = ["👁 **Список слежки:**"]
        for uid, data in entries.items():
            name = " ".join(filter(None, [data.get("first_name"), data.get("last_name")])) or str(uid)
            uname = f" (@{data['username']})" if data.get("username") else ""
            lines.append(f"• {name}{uname} — `{uid}`")
        await msg.reply("\n".join(lines))

    if acc_idx not in watch_tasks or watch_tasks[acc_idx].done():
        watch_tasks[acc_idx] = asyncio.create_task(_watch_loop(client, acc_idx))


async def _watch_loop(client: Client, acc_idx: int, interval: int = 300):
    await asyncio.sleep(15)
    while True:
        try:
            entries = watch_state.get(acc_idx, {})
            for uid, baseline in list(entries.items()):
                try:
                    user = await client.get_users(uid)
                except Exception:
                    continue
                if isinstance(user, list):
                    if not user:
                        continue
                    user = user[0]
                changes = []
                if user.first_name != baseline.get("first_name") or user.last_name != baseline.get("last_name"):
                    old_name = " ".join(filter(None, [baseline.get("first_name"), baseline.get("last_name")])) or "—"
                    new_name = " ".join(filter(None, [user.first_name, user.last_name])) or "—"
                    changes.append(f"✏️ Имя: {old_name} → {new_name}")
                if user.username != baseline.get("username"):
                    old_u = f"@{baseline['username']}" if baseline.get("username") else "—"
                    new_u = f"@{user.username}" if user.username else "—"
                    changes.append(f"🔗 Юзернейм: {old_u} → {new_u}")
                new_photo_id = user.photo.big_file_id if user.photo else None
                if new_photo_id != baseline.get("photo_id"):
                    changes.append("🖼 Сменил(а) фото профиля")
                if changes:
                    display = " ".join(filter(None, [user.first_name, user.last_name])) or str(uid)
                    await _notify_me(client, f"👁 **{display}** (`{uid}`) изменил(а) данные:\n" + "\n".join(changes))
                baseline["first_name"] = user.first_name
                baseline["last_name"] = user.last_name
                baseline["username"] = user.username
                baseline["photo_id"] = new_photo_id
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[acc{acc_idx}] watch loop: {e}")
        await asyncio.sleep(interval)
