import asyncio
import logging
import os
import shutil
import subprocess
import time
import zipfile
import resource


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
from pathlib import Path

import aiohttp
import libtorrent as lt
from aiohttp import web
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("torrent-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_IDS = {
    int(x)
    for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",")
    if x.strip()
}

WORK_DIR = Path(os.environ.get("WORK_DIR", "/tmp/torrents"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

MAX_BYTES = int(float(os.environ.get("MAX_TORRENT_SIZE_GB", "1.5")) * 1024 ** 3)
CHUNK_BYTES = int(float(os.environ.get("UPLOAD_CHUNK_MB", "900")) * 1024 ** 2)
TG_SAFE = 1980 * 1024 * 1024

PUBLIC_URL = (
    os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    or os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
)
PORT = int(os.environ.get("PORT", "10000"))

EXTRA_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "http://tracker.openbittorrent.com:80/announce",
    "http://tracker.opentrackr.org:1337/announce",
]

SETTINGS = {
    "listen_interfaces": "0.0.0.0:6881",
    "enable_dht": True,
    "enable_lsd": False,
    "enable_upnp": False,
    "enable_natpmp": False,
    "connections_limit": 50,
    "active_limit": 60,
    "aio_threads": 1,
    "cache_size": 8 * 1024 * 1024,
    "max_queued_disk_bytes": 8 * 1024 * 1024,
    "alert_mask": lt.alert.category_t.error_notification,
}
try:
    SESSION = lt.session(SETTINGS)
except Exception:
    log.warning("settings rejected, using defaults")
    SESSION = lt.session()

JOBS: dict[int, dict] = {}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def bar(p: float, width: int = 18) -> str:
    filled = int(p * width)
    return "█" * filled + "░" * (width - filled)


def split_file(path: Path, part: int) -> list[Path]:
    out = path.parent / (path.name + ".parts")
    out.mkdir(exist_ok=True)
    log.info("splitting %s into %s MB chunks", path.name, part // 1024 ** 2)
    subprocess.run(
        ["split", "-b", str(part), "-d", "-a", "3", str(path), str(out / (path.name + ".p"))],
        check=True,
    )
    parts = sorted(p for p in out.iterdir() if p.is_file())
    log.info("split done: %s parts", len(parts))
    return parts


def zip_files(files: list[Path], dest: Path) -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for f in files:
            z.write(f, f.name)


def cleanup_old() -> None:
    for p in WORK_DIR.iterdir():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


async def edit(bot, chat_id: int, msg_id: int, text: str) -> None:
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text)
    except Exception:
        pass


async def guard(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    if ALLOWED_IDS and uid not in ALLOWED_IDS:
        await update.effective_message.reply_text(f"دسترسی نداری.\nآیدی عددی تو: {uid}")
        return False
    return True


async def typing_loop(bot, chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id, "upload_document")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass


async def upload_once(bot, chat_id: int, path: Path, caption: str | None) -> None:
    with path.open("rb") as f:
        await bot.send_document(
            chat_id,
            document=f,
            filename=path.name,
            caption=caption,
            read_timeout=1800,
            write_timeout=1800,
            connect_timeout=120,
        )


async def send_file(bot, chat_id: int, path: Path) -> None:
    size = path.stat().st_size
    log.info("start sending %s (%.1f MB)", path.name, size / 1024 ** 2)

    if size <= min(TG_SAFE, CHUNK_BYTES):
        try:
            await upload_once(bot, chat_id, path, None)
            log.info("sent %s", path.name)
            return
        except Exception as e:
            log.warning("single upload of %s failed: %s -> will split", path.name, e)

    parts = await asyncio.to_thread(split_file, path, CHUNK_BYTES)
    try:
        path.unlink()
        log.info("removed original to free disk")
    except Exception:
        pass
    total = len(parts)
    for i, p in enumerate(parts, 1):
        caption = f"بخش {i} از {total}"
        for attempt in range(1, 4):
            try:
                log.info("uploading part %s/%s (%s)", i, total, p.name)
                await upload_once(bot, chat_id, p, caption)
                break
            except Exception as e:
                log.warning("part %s attempt %s failed: %s", p.name, attempt, e)
                if attempt == 3:
                    await bot.send_message(chat_id, f"بخش {i} ارسال نشد: {e}")
                else:
                    await asyncio.sleep(5 * attempt)


async def run_job(chat_id: int, msg_id: int, magnet: str, job: dict) -> None:
    bot = application.bot
    save_dir = WORK_DIR / str(chat_id) / str(int(time.time()))
    save_dir.mkdir(parents=True, exist_ok=True)
    job["dir"] = save_dir
    handle = None
    stop = asyncio.Event()

    try:
        try:
            params = lt.parse_magnet_uri(magnet)
        except Exception as e:
            await edit(bot, chat_id, msg_id, f"لینک مگنت نامعتبر است.\n{e}")
            return

        params.save_path = str(save_dir)
        params.storage_mode = lt.storage_mode_t.storage_mode_sparse
        handle = SESSION.add_torrent(params)
        job["handle"] = handle
        try:
            for t in EXTRA_TRACKERS:
                handle.add_tracker({"url": t, "tier": 3})
            handle.set_max_connections(40)
        except Exception:
            pass

        await edit(bot, chat_id, msg_id, "در حال گرفتن اطلاعات تورنت...")

        for _ in range(90):
            if job["cancel"]:
                await edit(bot, chat_id, msg_id, "لغو شد.")
                return
            if handle.torrent_file() is not None:
                break
            await asyncio.sleep(2)
        else:
            await edit(bot, chat_id, msg_id, "اطلاعات تورنت پیدا نشد (مگنت مرده یا بدون سیدر).")
            return

        ti = handle.torrent_file()
        name = ti.name()
        total = ti.total_size()
        log.info("metadata ok: %s (%s)", name, human(total))

        if total > MAX_BYTES:
            await edit(
                bot,
                chat_id,
                msg_id,
                f"حجم تورنت {human(total)} از سقف {human(MAX_BYTES)} بیشتر است.",
            )
            return

        await edit(bot, chat_id, msg_id, f"📥 {name}\nحجم: {human(total)}\nآماده‌سازی...")

last = 0.0
        while True:
            if job["cancel"]:
                await edit(bot, chat_id, msg_id, "لغو شد.")
                return
            st = handle.status()
            log.info("progress %.1f%% rss=%.0fMB peers=%s", st.progress * 100, rss_mb(), st.num_peers)
            if st.state == lt.torrent_status.seeding or st.progress >= 1.0:
                break
            now = time.time()
            if now - last > 8:
                last = now
                await edit(
                    bot,
                    chat_id,
                    msg_id,
                    f"📥 {name}\n"
                    f"{bar(st.progress)} {st.progress * 100:.1f}%\n"
                    f"{human(st.total_done)} از {human(total)}\n"
                    f"سرعت: {human(st.download_rate)}/s · سیدر: {st.num_seeds}\n"
                    f"RAM: {rss_mb():.0f}MB",
                )
            await asyncio.sleep(2)

        log.info("download finished: %s", name)
        await edit(bot, chat_id, msg_id, "✅ دانلود کامل شد. در حال ارسال...")

        files = sorted(p for p in save_dir.rglob("*") if p.is_file())
        log.info("files on disk: %s (%s)", len(files), human(sum(f.stat().st_size for f in files)))
        if not files:
            await edit(bot, chat_id, msg_id, "فایلی پیدا نشد.")
            return

        size_all = sum(f.stat().st_size for f in files)
        targets: list[Path] = []

        if len(files) > 1 and size_all <= CHUNK_BYTES:
            archive = save_dir / (name.replace("/", "_") + ".zip")
            await asyncio.to_thread(zip_files, files, archive)
            targets = [archive]
        else:
            targets = files

        stop.clear()
        typing = asyncio.create_task(typing_loop(bot, chat_id, stop))
        try:
            for f in targets:
                await send_file(bot, chat_id, f)
        finally:
            stop.set()
            typing.cancel()

        log.info("all sent: %s", name)
        await bot.send_message(chat_id, f"تمام شد: {name}")

    except Exception as e:
        log.exception("job failed")
        await edit(bot, chat_id, msg_id, f"خطا:\n{e}")
    finally:
        stop.set()
        if handle is not None:
            try:
                SESSION.remove_torrent(handle)
            except Exception:
                pass
        await asyncio.sleep(1)
        shutil.rmtree(save_dir, ignore_errors=True)
        JOBS.pop(chat_id, None)


async def on_magnet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text = (update.message.text or "").strip()
    if not text.startswith("magnet:?"):
        await update.message.reply_text("فقط لینک مگنت بفرست. راهنما: /help")
        return

    chat_id = update.effective_chat.id
    if chat_id in JOBS:
        await update.message.reply_text("یک دانلود دیگر در جریان است. اول /cancel بزن.")
        return

    job: dict = {"cancel": False, "handle": None, "dir": None}
    JOBS[chat_id] = job
    msg = await update.message.reply_text("در حال آماده‌سازی...")
    asyncio.create_task(run_job(chat_id, msg.message_id, text, job))

async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    du = shutil.disk_usage("/tmp")
    await update.message.reply_text(
        f"RAM: {rss_mb():.0f} MB\nدیسک آزاد: {human(du.free)}\nکارهای فعال: {len(JOBS)}"
    )
application = (
    Application.builder()
    .token(BOT_TOKEN)
    .updater(None)
    .connect_timeout(60)
    .read_timeout(120)
    .write_timeout(1800)
    .build()
)

application.add_handler(CommandHandler("start", cmd_start := None) or CommandHandler("start", on_magnet))
application.add_handler(CommandHandler("help", on_magnet))
application.add_handler(CommandHandler("cancel", on_magnet))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_magnet))
application.add_handler(CommandHandler("debug", cmd_debug))
