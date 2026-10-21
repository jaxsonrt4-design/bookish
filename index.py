import os
import re
import io
import csv
import asyncio
import logging
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
VERCEL_URL = os.environ.get("VERCEL_URL", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("panel-bot")

PANEL_PATHS = [
    "", "users", "numbers", "phone", "phones",
    "sms", "otps", "allattk", "data", "victims",
]

app = FastAPI()
tg_app = Application.builder().token(BOT_TOKEN).build()

_initialized = False
_init_lock = asyncio.Lock()

async def ensure_init():
    global _initialized
    if _initialized:
        return
    async with _init_lock:
        if _initialized:
            return
        await tg_app.initialize()
        _initialized = True

# ─────────────────────────────────────────────────────────────────────────────
async def probe_path(session, base, path):
    url = f"{base}/{path}.json?shallow=true" if path else f"{base}/.json?shallow=true"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as r:
            if r.status != 200:
                return path, []
            data = await r.json(content_type=None)
            if isinstance(data, dict):
                return path, list(data.keys())
            if isinstance(data, list):
                return path, [str(x) for x in data if x is not None]
    except Exception:
        pass
    return path, []

async def scan_panel(session, url):
    base = url.rstrip("/")
    if base.endswith(".json"):
        base = base[:-5]
    results = await asyncio.gather(*[probe_path(session, base, p) for p in PANEL_PATHS])
    unique = set()
    for _, keys in results:
        unique.update(keys)
    return url, sorted(unique)

async def scan_all(urls):
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*[scan_panel(session, u) for u in urls])
    per_url = {u: ids for u, ids in results}
    global_unique = set()
    for ids in per_url.values():
        global_unique.update(ids)
    return {"per_url": per_url, "unique": sorted(global_unique), "count": len(global_unique)}

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
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    html = (
        "<b>PANEL FIREBASE SCANNER</b>\n"
        "<i>online number counter + CSV export</i>\n\n"
        "Paste one or more Firebase panel URLs.\n"
        "One per line. I scan every path, count unique numbers,\n"
        "and send back a CSV instantly.\n\n"
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
        chat_id, f"Scanning <b>{len(urls)}</b> panel(s)…", parse_mode=ParseMode.HTML,
    )

    result = await scan_all(urls)

    lines = [
        "<b>SCAN COMPLETE</b>",
        f"<b>Panels:</b> {len(urls)}",
        f"<b>Total online numbers:</b> <b>{result['count']}</b>",
        "",
    ]
    for u, ids in result["per_url"].items():
        short = u.replace("https://", "").replace(".firebaseio.com", "")
        lines.append(f"• <code>{short}</code> → <b>{len(ids)}</b>")

    if result["count"]:
        lines.append("")
        lines.append("<b>Sample (first 10):</b>")
        for n in result["unique"][:10]:
            lines.append(f"• <code>{n}</code>")

    await ack.delete()
    await send_rich(context, chat_id, "\n".join(lines))

    if result["count"]:
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["number", "scanned_at"])
        scanned_at = datetime.utcnow().isoformat()
        for n in result["unique"]:
            w.writerow([n, scanned_at])

        filename = f"online_numbers_{ts}.csv"
        doc = InputFile(
            io.BytesIO(buf.getvalue().encode("utf-8")),
            filename=filename, mime_type="text/csv",
        )
        await context.bot.send_document(
            chat_id=chat_id, document=doc,
            caption=f"<b>{result['count']}</b> numbers · {filename}",
            parse_mode=ParseMode.HTML,
        )

tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("help", cmd_help))
tg_app.add_handler(CallbackQueryHandler(help_cb, pattern="^help$"))
tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

# ─────────────────────────────────────────────────────────────────────────────
@app.get("/")
async def health():
    return {"status": "ok", "bot": "panel-scanner"}

@app.get("/setwebhook")
async def set_webhook(secret: str = ""):
    if secret != WEBHOOK_SECRET:
        return Response(status_code=403)
    if not VERCEL_URL:
        return {"error": "VERCEL_URL not set in env"}
    webhook_url = f"https://{VERCEL_URL}/{WEBHOOK_SECRET}"
    await ensure_init()
    await tg_app.bot.set_webhook(webhook_url)
    return {"webhook": webhook_url, "status": "set"}

@app.post("/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        return Response(status_code=403)
    await ensure_init()
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return Response(status_code=200)
