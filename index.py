import os
import re
import io
import csv
import json
import html as html_lib
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import urlparse, parse_qsl, urlencode

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

PANEL_PATHS = ["clients", "users", "devices", "online"]

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
def split_url(raw: str):
    u = urlparse(raw.strip())
    query = dict(parse_qsl(u.query))
    path = (u.path or "").rstrip("/")
    if path.endswith(".json"):
        path = path[:-5]
    for known in ("/clients", "/users", "/devices", "/online"):
        if path.endswith(known):
            path = path[: -len(known)]
            break
    base = f"{u.scheme}://{u.netloc}{path}".rstrip("/")
    return base, query


def build_endpoint(base: str, path: str, query: dict):
    url = f"{base}/{path}.json" if path else f"{base}/.json"
    if query:
        url += "?" + urlencode(query)
    return url

# ─────────────────────────────────────────────────────────────────────────────
def normalize_indian(raw) -> str | None:
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


def is_online(device) -> bool:
    if not isinstance(device, dict):
        return False
    status = device.get("status")
    if status is True or status == 1:
        return True
    if isinstance(status, str) and status.lower().strip() in ("true", "1", "online"):
        return True
    return False


def get_device_phone(device) -> str | None:
    phone = device.get("mobNo")
    if phone and str(phone).strip():
        return str(phone).strip()

    sims = device.get("sims")
    if isinstance(sims, list) and sims:
        s0 = sims[0]
        if isinstance(s0, dict):
            pn = s0.get("phoneNumber")
            if pn and str(pn).strip():
                return str(pn).strip()
    elif isinstance(sims, dict):
        for sim in sims.values():
            if isinstance(sim, dict):
                pn = sim.get("phoneNumber")
                if pn and str(pn).strip():
                    return str(pn).strip()

    return None

# ─────────────────────────────────────────────────────────────────────────────
async def fetch_json(session, url: str):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return None, f"HTTP {r.status}"
            text = await r.text()
            if not text or text == "null":
                return None, "empty"
            try:
                return json.loads(text), None
            except json.JSONDecodeError:
                return None, "invalid json"
    except asyncio.TimeoutError:
        return None, "timeout"
    except Exception as e:
        return None, str(e)


async def scan_panel(session, raw_url: str):
    base, query = split_url(raw_url)

    data = None
    used_path = None
    last_err = None

    for path in PANEL_PATHS:
        endpoint = build_endpoint(base, path, query)
        d, err = await fetch_json(session, endpoint)
        if isinstance(d, dict) and len(d) > 0:
            data = d
            used_path = path
            break
        last_err = err

    if data is None:
        return {
            "url": raw_url,
            "phones": [],
            "count": 0,
            "total": 0,
            "online": 0,
            "error": last_err or "no data",
            "path": None,
        }

    online_phones = set()
    total = 0
    online = 0

    for device_id, device in data.items():
        if not isinstance(device, dict):
            continue
        total += 1
        if not is_online(device):
            continue
        online += 1
        phone = get_device_phone(device)
        if phone:
            n = normalize_indian(phone)
            if n:
                online_phones.add(n)

    return {
        "url": raw_url,
        "phones": sorted(online_phones),
        "count": len(online_phones),
        "total": total,
        "online": online,
        "error": None,
        "path": used_path,
    }


async def scan_all(urls: list[str]):
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*[scan_panel(session, u) for u in urls])
    per_url = {r["url"]: r for r in results}
    global_unique = set()
    for r in results:
        global_unique.update(r["phones"])
    return {"per_url": per_url, "unique": sorted(global_unique), "count": len(global_unique)}

# ─────────────────────────────────────────────────────────────────────────────
def menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📖 How to scan", callback_data="help")]])


def esc(t) -> str:
    return html_lib.escape(str(t), quote=False)

# ─────────────────────────────────────────────────────────────────────────────
# REPORT — classic HTML + <blockquote expandable> for collapsible
# ─────────────────────────────────────────────────────────────────────────────
def build_report(result) -> str:
    parts = [
        "<b>📊 SCAN COMPLETE</b>",
        f"<b>Panels:</b> {len(result['per_url'])}",
        f"<b>Total unique numbers:</b> {result['count']}",
        "",
    ]

    for url, r in result["per_url"].items():
        short = url.replace("https://", "").split("?")[0].replace(".firebaseio.com", "")
        parts.append(f"<b>🔗 {esc(short)}</b>")
        parts.append(f"<i>{r['count']} numbers · {r['online']} online · {r['total']} devices</i>")

        if r["error"]:
            parts.append(f"<i>⚠️ {esc(r['error'])}</i>")
        elif r["phones"]:
            header = f"📱 {r['count']} numbers (tap to expand)"
            body_lines = [esc(n) for n in r["phones"]]
            body = "\n".join(body_lines)
            parts.append(f"<blockquote expandable>{header}\n{body}</blockquote>")
        else:
            parts.append("<i>No valid numbers found.</i>")

        parts.append("")

    return "\n".join(parts)


async def send_html(context, chat_id, html: str, keyboard=None):
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=html,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception("send_html failed: %s", exc)
        # last-ditch plain text fallback
        stripped = re.sub(r"<[^>]+>", "", html)
        await context.bot.send_message(chat_id=chat_id, text=stripped[:4000])

# ─────────────────────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    html = (
        "<b>📡 Panel Number Extractor</b>\n"
        "<i>collapsible lists · +91 only</i>\n\n"
        "Paste Firebase panel URL(s), one per line.\n"
        "Include <code>?auth=KEY</code> if needed.\n\n"
        "I read <code>clients</code> → keep <code>status: true</code> → "
        "extract <code>mobNo</code> → filter valid +91.\n\n"
        "Numbers appear <b>under each link</b> in a tap-to-expand block."
    )
    await send_html(context, update.effective_chat.id, html, menu_keyboard())


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
        await context.bot.send_message(chat_id, "No URLs found. Send panel URL(s), one per line.")
        return

    ack = await context.bot.send_message(
        chat_id, f"Scanning <b>{len(urls)}</b> panel(s)…", parse_mode=ParseMode.HTML,
    )

    try:
        result = await scan_all(urls)
    except Exception as exc:
        logger.exception("scan_all failed: %s", exc)
        await ack.edit_text("Scan failed. Check URL format and auth key.")
        return

    try:
        await ack.delete()
    except Exception:
        pass

    report = build_report(result)
    await send_html(context, chat_id, report)

    if result["count"]:
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["phone_number", "scanned_at"])
        scanned_at = datetime.utcnow().isoformat()
        for n in result["unique"]:
            w.writerow([n, scanned_at])
        filename = f"online_indian_{ts}.csv"
        doc = InputFile(io.BytesIO(buf.getvalue().encode("utf-8")), filename=filename, mime_type="text/csv")
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
    return {"status": "ok", "bot": "blockquote-panel", "alias": VERCEL_URL}


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
