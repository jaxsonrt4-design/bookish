import os
import re
import io
import csv
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime

import aiohttp
from fastapi import FastAPI, Request, Response
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "changeme-now")
BYPASS_SECRET = os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET", "")

VERCEL_URL = (
    os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")
    or os.environ.get("VERCEL_BRANCH_URL")
    or "bookish-theta-two.vercel.app"
).replace("https://", "").replace("http://", "").rstrip("/")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("panel-bot")

PANEL_PATHS = [
    "", "users", "numbers", "phone", "phones",
    "sms", "otps", "allattk", "data", "victims",
    "clients", "accounts", "All_Users", "All_User",
    "online", "online_users", "onlineUsers", "active", "activeUsers",
]

PHONE_FIELDS = {
    "number", "phone", "phonenumber", "mobile", "mobilenumber",
    "phone_number", "mobile_number", "contact", "msisdn", "tel",
}

tg_app = Application.builder().token(BOT_TOKEN).build()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg_app.initialize()
    await tg_app.start()
    logger.info("PTB started. Alias: %s", VERCEL_URL)
    yield
    await tg_app.stop()
    await tg_app.shutdown()


app = FastAPI(lifespan=lifespan)


# ─────────────────────────────────────────────────────────────────────────────
# Online detection
# ─────────────────────────────────────────────────────────────────────────────
def is_online(node) -> bool:
    """Return True if this node explicitly marks itself online."""
    if not isinstance(node, dict):
        return False
    for k, v in node.items():
        kl = str(k).lower().replace("_", "").replace(" ", "")

        # boolean-style online fields
        if kl in ("online", "isonline", "isactive", "active"):
            if v is True or v == 1:
                return True
            if isinstance(v, str) and v.lower() in ("true", "1", "yes", "online"):
                return True

        # string-style status fields
        if kl in ("status", "connection", "connectionstatus", "state", "presence"):
            if isinstance(v, str) and v.lower() in ("online", "connected"):
                return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# Indian phone normalization
# ─────────────────────────────────────────────────────────────────────────────
def normalize_indian(raw: str) -> str | None:
    """
    Accept only valid Indian mobile numbers. Return +91XXXXXXXXXX or None.
    Valid: +91 + 10 digits starting 6-9, or bare 10 digits, or 12-digit 91-prefixed.
    """
    raw = str(raw).strip()
    if not raw:
        return None

    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None

    # Explicit + prefix: MUST be +91 followed by exactly 10 valid digits
    if raw.startswith("+"):
        if not raw.startswith("+91"):
            return None
        if len(digits) == 12:
            local = digits[2:]
            if len(local) == 10 and local[0] in "6789":
                return "+91" + local
        return None

    # Bare 10-digit Indian mobile
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits

    # 12 digits starting with 91
    if len(digits) == 12 and digits.startswith("91"):
        local = digits[2:]
        if local[0] in "6789":
            return "+91" + local

    # 11 digits starting with 0 (trunk prefix)
    if len(digits) == 11 and digits.startswith("0"):
        local = digits[1:]
        if local[0] in "6789":
            return "+91" + local

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Extraction — online + Indian only
# ─────────────────────────────────────────────────────────────────────────────
def extract_online_indian(node, found, online_ctx=False):
    """
    Walk JSON tree. Track online status via parent inheritance.
    Only extract phone numbers when the node context is online.
    """
    if isinstance(node, dict):
        node_online = online_ctx or is_online(node)

        for k, v in node.items():
            if isinstance(v, (str, int)) and not isinstance(v, bool):
                kl = str(k).lower().replace("_", "").replace(" ", "")
                if kl in PHONE_FIELDS and node_online:
                    normalized = normalize_indian(str(v))
                    if normalized:
                        found.add(normalized)
            elif isinstance(v, (dict, list)):
                extract_online_indian(v, found, node_online)

    elif isinstance(node, list):
        for item in node:
            extract_online_indian(item, found, online_ctx)


# ─────────────────────────────────────────────────────────────────────────────
# Firebase fetch
# ─────────────────────────────────────────────────────────────────────────────
async def fetch_path(session, base, path):
    url = f"{base}/{path}.json" if path else f"{base}/.json"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None)
    except Exception:
        return None


async def scan_panel(session, url):
    base = url.rstrip("/")
    if base.endswith(".json"):
        base = base[:-5]

    results = await asyncio.gather(*[fetch_path(session, base, p) for p in PANEL_PATHS])

    phones = set()
    for data in results:
        if data:
            extract_online_indian(data, phones)

    return url, sorted(phones)


async def scan_all(urls):
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*[scan_panel(session, u) for u in urls])
    per_url = {u: phones for u, phones in results}
    global_unique = set()
    for phones in per_url.values():
        global_unique.update(phones)
    return {"per_url": per_url, "unique": sorted(global_unique), "count": len(global_unique)}


# ─────────────────────────────────────────────────────────────────────────────
# Telegram helpers
# ─────────────────────────────────────────────────────────────────────────────
def menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 How to scan", callback_data="help")],
    ])


async def send_rich(context, chat_id, html, keyboard=None):
    try:
        await context.bot.send_rich_message(
            chat_id=chat_id, rich_message={"html": html}, reply_markup=keyboard,
        )
    except Exception:
        await context.bot.send_message(
            chat_id=chat_id, text=html, parse_mode=ParseMode.HTML, reply_markup=keyboard,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    html = (
        "<b>ONLINE INDIAN NUMBER EXTRACTOR</b>\n"
        "<i>online users only · Indian numbers only</i>\n\n"
        "Paste one or more Firebase panel URLs.\n"
        "One per line. I pull only <b>online</b> users\n"
        "with valid <b>+91</b> phone numbers.\n\n"
        "<b>Example:</b>\n"
        "<code>https://panel-wala-v27-default-rtdb.firebaseio.com</code>"
    )
    await send_rich(context, update.effective_chat.id, html, menu_keyboard())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def help_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await cmd_start(update, context)


URL_RE = re.compile(r"https?://[^\s\)\]\>,]+", re.IGNORECASE)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    urls = URL_RE.findall(text)

    if not urls:
        await context.bot.send_message(
            chat_id, "No URLs found. Send Firebase panel URL(s), one per line.",
        )
        return

    ack = await context.bot.send_message(
        chat_id,
        f"Scanning <b>{len(urls)}</b> panel(s) for online Indian numbers…",
        parse_mode=ParseMode.HTML,
    )

    result = await scan_all(urls)

    lines = [
        "<b>SCAN COMPLETE</b>",
        f"<b>Panels:</b> {len(urls)}",
        f"<b>Online Indian numbers:</b> <b>{result['count']}</b>",
        "",
    ]
    for u, phones in result["per_url"].items():
        short = u.replace("https://", "").replace(".firebaseio.com", "")
        lines.append(f"• <code>{short}</code> → <b>{len(phones)}</b>")

    if result["count"]:
        lines.append("")
        lines.append("<b>Sample (first 10):</b>")
        for n in result["unique"][:10]:
            lines.append(f"• <code>{n}</code>")

    try:
        await ack.delete()
    except Exception:
        pass
    await send_rich(context, chat_id, "\n".join(lines))

    if result["count"]:
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["phone_number", "scanned_at"])
        scanned_at = datetime.utcnow().isoformat()
        for n in result["unique"]:
            w.writerow([n, scanned_at])

        filename = f"online_indian_{ts}.csv"
        doc = InputFile(
            io.BytesIO(buf.getvalue().encode("utf-8")),
            filename=filename, mime_type="text/csv",
        )
        await context.bot.send_document(
            chat_id=chat_id, document=doc,
            caption=f"<b>{result['count']}</b> online Indian numbers · {filename}",
            parse_mode=ParseMode.HTML,
        )


tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("help", cmd_help))
tg_app.add_handler(CallbackQueryHandler(help_cb, pattern="^help$"))
tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))


# ─────────────────────────────────────────────────────────────────────────────
# Webhook endpoints
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/")
async def health():
    return {"status": "ok", "bot": "online-indian-extractor", "alias": VERCEL_URL}


@app.get("/setwebhook")
async def set_webhook(secret: str = ""):
    if secret != WEBHOOK_SECRET:
        return Response(status_code=403)
    webhook_url = f"https://{VERCEL_URL}/{WEBHOOK_SECRET}"
    if BYPASS_SECRET:
        webhook_url += f"?x-vercel-protection-bypass={BYPASS_SECRET}"
    await tg_app.bot.set_webhook(webhook_url)
    return {"webhook": webhook_url, "status": "set"}


@app.post("/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        return Response(status_code=403)
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    logger.info("update received: %s", update.update_id)
    try:
        await tg_app.process_update(update)
    except Exception as exc:
        logger.exception("process_update failed: %s", exc)
    return Response(status_code=200)
