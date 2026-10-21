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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

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

PANEL_PATHS = ["", "All_User", "All_Users", "users", "online", "active", "devices", "clients"]

# Phone field names — case-insensitive, underscore/space stripped
PHONE_KEYS = {
    "number", "phone", "phonenumber", "mobile", "mobilenumber",
    "phonenumber", "mobilenumber", "contact", "msisdn", "tel", "simnumber",
}

# STRICT: only these exact key names (lowercased, stripped) count as online flag
STRICT_ONLINE_KEYS = {"online", "isonline"}

# STRICT: only these values count as "true online"
STRICT_ONLINE_VALUES = {"true", "1", "yes", "online"}

# Skip these as user containers during walk
SKIP_KEYS = {"alluser", "allusers", "count", "total", "config", "settings", "stats", "meta"}

tg_app = Application.builder().token(BOT_TOKEN).build()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg_app.initialize()
    await tg_app.start()
    yield
    await tg_app.stop()
    await tg_app.shutdown()

app = FastAPI(lifespan=lifespan)

# ─────────────────────────────────────────────────────────────────────────────
# STRICT online check — top-level only, exact key + value
# ─────────────────────────────────────────────────────────────────────────────
def is_online_strict(node) -> bool:
    """Only true when node has key `online`/`isOnline` at TOP LEVEL with truthy value."""
    if not isinstance(node, dict):
        return False
    for k, v in node.items():
        kl = str(k).lower().replace("_", "").replace("-", "").replace(" ", "")
        if kl in STRICT_ONLINE_KEYS:
            if v is True or v == 1:
                return True
            if isinstance(v, str) and v.lower().strip() in STRICT_ONLINE_VALUES:
                return True
            if isinstance(v, int) and v > 0:
                return True
    return False

# ─────────────────────────────────────────────────────────────────────────────
# Phone extraction from a device node — recursive inside the node only
# ─────────────────────────────────────────────────────────────────────────────
def extract_phones(node, found, depth=0):
    if depth > 6:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            kl = str(k).lower().replace("_", "").replace("-", "").replace(" ", "")
            if kl in PHONE_KEYS and isinstance(v, (str, int)) and not isinstance(v, bool):
                n = normalize_indian(v)
                if n:
                    found.add(n)
            elif isinstance(v, (dict, list)):
                extract_phones(v, found, depth + 1)
    elif isinstance(node, list):
        for item in node:
            extract_phones(item, found, depth + 1)

def normalize_indian(raw):
    raw = str(raw).strip()
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if raw.startswith("+"):
        if not raw.startswith("+91"):
            return None
        if len(digits) == 12:
            local = digits[2:]
            if len(local) == 10 and local[0] in "6789":
                return "+91" + local
        return None
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        local = digits[2:]
        if local[0] in "6789":
            return "+91" + local
    if len(digits) == 11 and digits.startswith("0"):
        local = digits[1:]
        if local[0] in "6789":
            return "+91" + local
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Walk users at one level of the tree
# ─────────────────────────────────────────────────────────────────────────────
def walk_users(data, phones, samples, stats):
    """For each direct child dict that looks like a user, gate on strict online."""
    if not isinstance(data, dict):
        return
    for uid, node in data.items():
        if not isinstance(node, dict):
            continue
        ukl = str(uid).lower().replace("_", "").replace(" ", "")
        if ukl in SKIP_KEYS:
            continue

        stats["total_devices"] += 1
        online = is_online_strict(node)
        if online:
            stats["online_devices"] += 1
            before = len(phones)
            extract_phones(node, phones)
            if len(phones) > before:
                stats["devices_with_number"] += 1
            if len(samples) < 2:
                samples.append((uid, dict(node)))

# ─────────────────────────────────────────────────────────────────────────────
# Firebase fetch
# ─────────────────────────────────────────────────────────────────────────────
async def fetch_path(session, base, path):
    url = f"{base}/{path}.json" if path else f"{base}/.json"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None)
    except Exception:
        return None

async def scan_panel(session, url):
    base = url.rstrip("/").replace(".json", "")
    results = await asyncio.gather(*[fetch_path(session, base, p) for p in PANEL_PATHS])

    phones = set()
    samples = []
    stats = {"total_devices": 0, "online_devices": 0, "devices_with_number": 0}
    data_present = False

    for data in results:
        if not data:
            continue
        data_present = True
        walk_users(data, phones, samples, stats)

    return {
        "url": url,
        "phones": sorted(phones),
        "count": len(phones),
        "samples": samples,
        "stats": stats,
        "data_present": data_present,
    }

async def scan_all(urls):
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*[scan_panel(session, u) for u in urls])
    per_url = {r["url"]: r for r in results}
    global_unique = set()
    for r in results:
        global_unique.update(r["phones"])
    return {"per_url": per_url, "unique": sorted(global_unique), "count": len(global_unique)}

# ─────────────────────────────────────────────────────────────────────────────
# Telegram helpers
# ─────────────────────────────────────────────────────────────────────────────
def menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📖 How to scan", callback_data="help")]])

async def send_rich(context, chat_id, html, keyboard=None):
    try:
        await context.bot.send_rich_message(chat_id=chat_id, rich_message={"html": html}, reply_markup=keyboard)
    except Exception:
        await context.bot.send_message(chat_id=chat_id, text=html, parse_mode=ParseMode.HTML, reply_markup=keyboard)

def escape(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ─────────────────────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    html = (
        "<b>ONLINE INDIAN EXTRACTOR (STRICT)</b>\n"
        "<i>only devices with explicit Online flag · +91 only</i>\n\n"
        "Paste Firebase panel URL(s), one per line.\n"
        "I check each device for a literal <code>Online: true</code> field.\n\n"
        "Debug dump shows how many devices / how many online / how many with numbers."
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
        await context.bot.send_message(chat_id, "No URLs found. Send Firebase panel URL(s), one per line.")
        return

    ack = await context.bot.send_message(chat_id, f"Strict scan: <b>{len(urls)}</b> panel(s)…", parse_mode=ParseMode.HTML)
    result = await scan_all(urls)

    lines = [
        "<b>SCAN COMPLETE (STRICT)</b>",
        f"<b>Panels:</b> {len(urls)}",
        f"<b>Online Indian numbers:</b> <b>{result['count']}</b>",
        "",
    ]
    for u, r in result["per_url"].items():
        short = u.replace("https://", "").replace(".firebaseio.com", "")
        lines.append(f"• <code>{escape(short)}</code> → <b>{r['count']}</b>")
        s = r["stats"]
        lines.append(
            f"   <i>devices: {s['total_devices']} · online: {s['online_devices']} · with number: {s['devices_with_number']}</i>"
        )

    if result["count"]:
        lines.append("")
        lines.append("<b>Sample (first 10):</b>")
        for n in result["unique"][:10]:
            lines.append(f"• <code>{escape(n)}</code>")

    # Debug: show first matched device
    for u, r in result["per_url"].items():
        if r["samples"]:
            uid, node = r["samples"][0]
            lines.append("")
            lines.append(f"<b>First matched device</b> <code>{escape(uid)}</code>:")
            for kk, vv in list(node.items())[:15]:
                sv = str(vv)[:50]
                lines.append(f"  <code>{escape(kk)}</code> = <i>{escape(sv)}</i>")
            break

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
        filename = f"online_indian_strict_{ts}.csv"
        doc = InputFile(io.BytesIO(buf.getvalue().encode("utf-8")), filename=filename, mime_type="text/csv")
        await context.bot.send_document(chat_id=chat_id, document=doc, caption=f"<b>{result['count']}</b> online Indian numbers · {filename}", parse_mode=ParseMode.HTML)

tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("help", cmd_help))
tg_app.add_handler(CallbackQueryHandler(help_cb, pattern="^help$"))
tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

# ─────────────────────────────────────────────────────────────────────────────
@app.get("/")
async def health():
    return {"status": "ok", "bot": "strict-extractor", "alias": VERCEL_URL}

@app.get("/setwebhook")
async def set_webhook(secret: str = ""):
    if secret != WEBHOOK_SECRET:
        return Response(status_code=403)
    url = f"https://{VERCEL_URL}/{WEBHOOK_SECRET}"
    if BYPASS_SECRET:
        url += f"?x-vercel-protection-bypass={BYPASS_SECRET}"
    await tg_app.bot.set_webhook(url)
    return {"webhook": url, "status": "set"}

@app.post("/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        return Response(status_code=403)
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    try:
        await tg_app.process_update(update)
    except Exception as exc:
        logger.exception("process_update failed: %s", exc)
    return Response(status_code=200)
