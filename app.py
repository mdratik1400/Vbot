import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
import httpx

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("agentphone-bot")

# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------
BRAND_NAME         = "KSTM"
BRAND_HANDLE       = "@KSTM_0"
BRAND_YOUTUBE_URL  = "https://youtube.com/@X·AI-Hindi"
BRAND_TELEGRAM_URL = "https://t.me/Hhhh_88998889_bot"

def _brand_footer() -> str:
    return (
        f"\n\n━━━━━━━━━━━━━━\n"
        f"<b>{BRAND_NAME}</b>\n"
        f'📺 <a href="{BRAND_YOUTUBE_URL}">YouTube</a> '
        f'• 💬 <a href="{BRAND_TELEGRAM_URL}">Telegram</a>\n'
        f"{BRAND_HANDLE}"
    )

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR              = Path(__file__).parent
STATE_FILE            = BASE_DIR / "state.json"
LEGACY_PROCESSED_FILE = BASE_DIR / "processed_calls.json"
KEY_FILE              = BASE_DIR / "agentphone_key.json"

TELEGRAM_BOT_TOKEN = "9082830380:AAGJH4x0sz6djhFfDpUuqNaMHwtr9AXxaa4"
TELEGRAM_USER_ID   = "8802078961"

PHONETIC_DECODER = {
    "on": "1", "to": "2", "the": "3", "for": "4", "fine": "5",
    "sip": "6", "seen": "7", "weight": "8", "line": "9", "hero": "0",
}

MAX_PROCESSED = 500
START_TIME    = time.time()

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
def _valid_api_key(key: str) -> bool:
    return bool(re.fullmatch(r"sk_[A-Za-z0-9_\-]{10,}", key or ""))

def _load_api_key() -> str:
    if not KEY_FILE.exists():
        return ""
    try:
        return json.loads(KEY_FILE.read_text()).get("key", "").strip()
    except Exception as e:
        log.warning("Could not read key file: %s", e)
        return ""

def _save_api_key(key: str) -> None:
    KEY_FILE.write_text(json.dumps({"key": key}))
    try:
        KEY_FILE.chmod(0o600)
    except Exception:
        pass

def _load_state() -> dict:
    state = {"last_update_id": 0, "processed_calls": []}
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text())
            if isinstance(loaded, dict):
                state.update(loaded)
        except Exception as e:
            log.warning("Could not read state file: %s", e)
    elif LEGACY_PROCESSED_FILE.exists():
        try:
            legacy = json.loads(LEGACY_PROCESSED_FILE.read_text())
            if isinstance(legacy, list):
                state["processed_calls"] = legacy
            LEGACY_PROCESSED_FILE.rename(LEGACY_PROCESSED_FILE.with_suffix(".json.bak"))
            log.info("Migrated %d processed calls from legacy file", len(state["processed_calls"]))
        except Exception as e:
            log.warning("Could not migrate legacy state: %s", e)
    return state

def _write_state() -> None:
    try:
        payload = {
            "last_update_id": last_update_id,
            "processed_calls": list(processed_calls.keys()),
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(STATE_FILE)
    except Exception as e:
        log.error("Could not save state: %s", e)

_initial_state     = _load_state()
AGENTPHONE_API_KEY = _load_api_key()
last_update_id: int = int(_initial_state.get("last_update_id") or 0)
_raw_processed      = _initial_state.get("processed_calls") or []
processed_calls: dict[str, bool] = (
    {cid: True for cid in _raw_processed if isinstance(cid, str)}
    if isinstance(_raw_processed, list) else {}
)

def _mark_processed(call_id: str) -> None:
    processed_calls[call_id] = True
    while len(processed_calls) > MAX_PROCESSED:
        processed_calls.pop(next(iter(processed_calls)))
    _write_state()

# ---------------------------------------------------------------------------
# Shared HTTP client + background task tracking
# ---------------------------------------------------------------------------
_http: httpx.AsyncClient | None = None
_tasks: set[asyncio.Task] = set()

def _client() -> httpx.AsyncClient:
    if _http is None:
        raise RuntimeError("HTTP client not initialised")
    return _http

def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task

# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------
def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

async def send_telegram(text: str, retries: int = 1, with_footer: bool = False) -> bool:
    if with_footer:
        text = text + _brand_footer()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_USER_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(retries + 1):
        try:
            r = await _client().post(url, json=payload)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                retry_after = 5
                try:
                    retry_after = r.json().get("parameters", {}).get("retry_after", 5)
                except Exception:
                    pass
                log.warning("Telegram rate-limited, sleeping %ss", retry_after)
                await asyncio.sleep(retry_after)
                continue
            log.error("Telegram sendMessage HTTP %s: %s", r.status_code, r.text[:200])
            return False
        except Exception as e:
            log.error("Telegram send failed: %s: %r", type(e).__name__, e)
            if attempt < retries:
                await asyncio.sleep(2 ** attempt)
    return False

async def delete_telegram_message(message_id: int) -> None:
    try:
        await _client().post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage",
            json={"chat_id": TELEGRAM_USER_ID, "message_id": message_id},
        )
    except Exception as e:
        log.debug("deleteMessage failed: %s", e)

# ---------------------------------------------------------------------------
# Phonetic decoder
# ---------------------------------------------------------------------------
def decode_phonetic(text: str) -> str | None:
    if not text:
        return None
    seqs, current = [], []
    for w in text.lower().split():
        clean = re.sub(r"[^a-z]", "", w)
        if clean in PHONETIC_DECODER:
            current.append(PHONETIC_DECODER[clean])
        else:
            if len(current) >= 4:
                seqs.append("".join(current))
            current = []
    if len(current) >= 4:
        seqs.append("".join(current))
    return max(seqs, key=len) if seqs else None

# ---------------------------------------------------------------------------
# AgentPhone API
# ---------------------------------------------------------------------------
async def fetch_call_details(call_id: str) -> dict | None:
    if not AGENTPHONE_API_KEY:
        return None
    try:
        r = await _client().get(
            f"https://api.agentphone.ai/v1/calls/{call_id}",
            headers={"Authorization": f"Bearer {AGENTPHONE_API_KEY}"},
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 401:
            log.warning("AgentPhone 401: API key invalid or expired")
        elif r.status_code == 404:
            log.info("Call %s not found", call_id)
        else:
            log.warning("AgentPhone HTTP %s for %s", r.status_code, call_id)
    except Exception as e:
        log.error("fetch_call_details error: %s: %r", type(e).__name__, e)
    return None

async def process_call(call_id: str, from_number: str, to_number: str) -> None:
    data = await fetch_call_details(call_id)
    if data is None:
        return
    if "transcripts" not in data:
        await send_telegram(
            f"❌ Call <code>{_esc(call_id)}</code> ended, but transcript was empty.",
            with_footer=True,
        )
        return

    from_number = data.get("fromNumber") or from_number
    to_number   = data.get("toNumber") or data.get("phoneNumber") or to_number
    duration    = data.get("durationSeconds")

    agent_spoken, full_text = [], []
    for t in data.get("transcripts") or []:
        if (c := (t.get("transcript") or "").strip()):
            full_text.append(c)
        if (a := (t.get("response") or "").strip()):
            full_text.append(a)
            agent_spoken.append(a)

    joined = " ".join(full_text).lower()
    if "whatsapp" in joined:
        service = "WhatsApp"
    elif "telegram" in joined:
        service = "Telegram"
    elif "sms" in joined or "text message" in joined:
        service = "SMS"
    else:
        service = "WhatsApp"

    decoded = decode_phonetic(" ".join(agent_spoken))

    lines = [
        f"📱 <b>My Number:</b> <code>{_esc(to_number)}</code>",
        f"👤 <b>Caller:</b> <code>{_esc(from_number)}</code>",
        f"❤ <b>Service:</b> {service}",
    ]
    if duration:
        try:
            lines.append(f"⏱ <b>Duration:</b> {int(duration)}s")
        except Exception:
            pass
    if decoded:
        lines.append(f"🔑 <b>Code Detected:</b> <code>{decoded}</code>")
    else:
        lines.append("❌ <b>No Code Detected</b>")

    await send_telegram("\n".join(lines), with_footer=True)

# ---------------------------------------------------------------------------
# API poller
# ---------------------------------------------------------------------------
async def api_poller_loop() -> None:
    log.info("API poller started (every 10s)")
    warned_no_key = False
    backoff = 0
    while True:
        try:
            await asyncio.sleep(10)
            if not AGENTPHONE_API_KEY:
                if not warned_no_key:
                    log.info("Waiting for API key via /setapikey...")
                    warned_no_key = True
                continue
            warned_no_key = False

            r = await _client().get(
                "https://api.agentphone.ai/v1/calls?limit=5",
                headers={"Authorization": f"Bearer {AGENTPHONE_API_KEY}"},
            )
            if r.status_code != 200:
                log.warning("Poll HTTP %s", r.status_code)
                backoff = min(backoff + 1, 6)
                await asyncio.sleep(min(60, 2 ** backoff))
                continue
            backoff = 0

            for c in r.json().get("data", []):
                call_id = c.get("id")
                if not call_id or c.get("status") != "completed":
                    continue
                if call_id in processed_calls:
                    continue

                log.info("New completed call: %s", call_id)
                _mark_processed(call_id)
                from_num = c.get("fromNumber", "Unknown")
                to_num   = c.get("toNumber") or c.get("phoneNumber") or "My Number"
                _spawn(process_call(call_id, from_num, to_num))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("api_poller error: %s: %r", type(e).__name__, e)
            await asyncio.sleep(5)

# ---------------------------------------------------------------------------
# Telegram command handlers
# ---------------------------------------------------------------------------
async def cmd_setapikey(msg_id: int | None, parts: list[str]) -> None:
    global AGENTPHONE_API_KEY
    if len(parts) != 2 or not _valid_api_key(parts[1]):
        await send_telegram(
            "⚠️ Usage: <code>/setapikey sk_live_...</code>\n"
            "Key must start with <code>sk_</code>."
        )
        return
    AGENTPHONE_API_KEY = parts[1].strip()
    _save_api_key(AGENTPHONE_API_KEY)
    if msg_id:
        _spawn(delete_telegram_message(msg_id))
    await send_telegram(
        "✅ <b>API key updated and saved.</b>\nIt persists across restarts.",
        with_footer=True,
    )

async def cmd_transcript(parts: list[str]) -> None:
    if len(parts) < 2:
        await send_telegram("⚠️ Usage: <code>/transcript &lt;call_id&gt;</code>")
        return
    call_id = parts[1].strip()
    await send_telegram(f"⏳ Fetching transcript for <code>{_esc(call_id)}</code>...")
    await process_call(call_id, "Unknown", "Unknown")

async def cmd_status() -> None:
    uptime = time.time() - START_TIME
    h, rem = divmod(int(uptime), 3600)
    m, s   = divmod(rem, 60)
    duration_str = f"{h}h {m}m" if h else f"{m}m {s}s"
    key_status = "✅ loaded" if AGENTPHONE_API_KEY else "❌ not set"
    await send_telegram(
        f"📊 <b>Status</b>\n"
        f"Uptime: {duration_str}\n"
        f"API key: {key_status}\n"
        f"Calls tracked: {len(processed_calls)}\n"
        f"Last update ID: {last_update_id}",
        with_footer=True,
    )

async def cmd_revokekey() -> None:
    global AGENTPHONE_API_KEY
    AGENTPHONE_API_KEY = ""
    if KEY_FILE.exists():
        try:
            KEY_FILE.unlink()
        except Exception as e:
            log.warning("Could not delete key file: %s", e)
    await send_telegram(
        "🗑 <b>API key removed</b> from memory and disk.",
        with_footer=True,
    )

async def cmd_start() -> None:
    status = "✅ API key loaded" if AGENTPHONE_API_KEY else "⚠️ No API key — send /setapikey"
    await send_telegram(
        f"🤖 <b>{BRAND_NAME} Bot</b>\n"
        f"Status: {status}\n\n"
        f"<b>Commands</b>\n"
        f"/setapikey &lt;sk_live_...&gt; — set API key\n"
        f"/transcript &lt;id&gt; — fetch a call transcript\n"
        f"/status — show bot status\n"
        f"/revokekey — remove saved API key"
        f"{_brand_footer()}"
    )

# ---------------------------------------------------------------------------
# Telegram poller
# ---------------------------------------------------------------------------
async def telegram_poller_loop() -> None:
    global last_update_id
    log.info("Telegram poller started")
    backoff = 0
    while True:
        try:
            await asyncio.sleep(3)
            r = await _client().get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset": last_update_id + 1, "limit": 20, "timeout": 0},
            )
            if r.status_code == 409:
                log.error("Telegram 409 Conflict — another bot instance is running")
                await asyncio.sleep(30)
                continue
            if r.status_code != 200:
                log.warning("getUpdates HTTP %s: %s", r.status_code, r.text[:200])
                backoff = min(backoff + 1, 6)
                await asyncio.sleep(min(60, 2 ** backoff))
                continue
            backoff = 0

            try:
                payload = r.json()
            except Exception as e:
                log.warning("getUpdates non-JSON: %s", e)
                continue
            if not payload.get("ok"):
                continue

            for upd in payload.get("result", []):
                last_update_id = max(last_update_id, upd.get("update_id", 0))
                msg     = upd.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                text    = (msg.get("text") or "").strip()
                msg_id  = msg.get("message_id")

                if chat_id != TELEGRAM_USER_ID or not text.startswith("/"):
                    continue

                parts   = text.split()
                command = parts[0].lower().split("@", 1)[0]  # strip @botname suffix

                if   command == "/setapikey":  await cmd_setapikey(msg_id, parts)
                elif command == "/transcript": await cmd_transcript(parts)
                elif command == "/status":     await cmd_status()
                elif command == "/revokekey":  await cmd_revokekey()
                elif command == "/start":      user_task = await cmd_start()

            _write_state()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("telegram_poller error: %s: %r", type(e).__name__, e)
            await asyncio.sleep(5)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    global _http
    _http = httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=10.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    log.info("Starting %s Bot", BRAND_NAME)
    log.info("Key file: %s (%s)", KEY_FILE, "loaded" if AGENTPHONE_API_KEY else "empty")
    log.info("State: last_update_id=%d, processed=%d", last_update_id, len(processed_calls))
    log.info("YouTube: %s", BRAND_YOUTUBE_URL)
    log.info("Telegram: %s", BRAND_TELEGRAM_URL)
    try:
        await asyncio.gather(api_poller_loop(), telegram_poller_loop())
    finally:
        log.info("Shutting down...")
        for task in list(_tasks):
            task.cancel()
        if _tasks:
            await asyncio.gather(*_tasks, return_exceptions=True)
        await _http.aclose()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
