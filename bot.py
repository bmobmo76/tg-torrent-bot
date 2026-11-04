import asyncio
import logging
import os
import shutil
import subprocess
import time
import zipfile
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

MAX_BYTES = int(float(os.environ.get("MAX_TORRENT_SIZE_GB", "2")) * 1024 ** 3)
PART_BYTES = 1900 * 1024 * 1024
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
    "enable_lsd": True,
    "enable_upnp": True,
    "enable_natpmp": True,
    "connections_limit": 120,
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
    subprocess.run(
        ["split", "-b", str(part), "-d", "-a", "3", str(path), str(out / (path.name + ".p"))],
        check=True,
    )
    return sorted(p for p in out.iterdir() if p.is_file())


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


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.message.reply_text(
        "سلام.\n\n"
        "لینک مگنت را بفرست تا دانلود کنم و فایل را همینجا تحویل بدهم.\n\n"
        "• /cancel برای لغو دانلود جاری\n"
        "• سقف هر تورنت ۲ گیگابایت است؛ فایل بزرگتر خودکار تکه میشود.\n"
        "• فایلها پس از تحویل پاک میشوند."
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.message.reply_text(
        "۱. لینک مگنت را کپی کن.\n"
        "۲. همینجا بفرست.\n"
        "۳. منتظر بمان؛ پیشرفت را نشان میدهم.\n"
        "۴. فایل بهصورت داکیومنت میرسد.\n\n"
        "اگر فایل از ۲ گیگ بیشتر باشد، قطعهقطعه میشود؛ روی ویندوز با "
        "`copy /b` و روی لینوکس با `cat` دوباره یکیاش کن."
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    chat_id = update.effective_chat.id
    job = JOBS.get(chat_id)
    if not job:
        await update.message.reply_text("دانلودی در جریان نیست.")
        return
    job["cancel"] = True
    await update.message.reply_text("دارم لغو میکنم...")


async def send_file(bot, chat_id: int, path: Path) -> None:
    size = path.stat().st_size
    if size <= TG_SAFE:
        with path.open("rb") as f:
            await bot.send_document(
                chat_id,
                document=f,
                filename=path.name,
                read_timeout=900,
                write_timeout=900,
                connect_timeout=120,
            )
        return

    parts = await asyncio.to_thread(split_file, path, PART_BYTES)
    total = len(parts)
    for i, p in enumerate(parts, 1):
        with p.open("rb") as f:
            await bot.send_document(
                chat_id,
                document=f,
                filename=p.name,
                caption=f"بخش {i} از {total}",
                read_timeout=900,
                write_timeout=900,
                connect_timeout=120,
            )


async def run_job(chat_id: int, msg_id: int, magnet: str, job: dict) -> None:
    bot = application.bot
    save_dir = WORK_DIR / str(chat_id) / str(int(time.time()))
    save_dir.mkdir(parents=True, exist_ok=True)
    job["dir"] = save_dir
    handle = None

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
            handle.set_sequential_download(True)
            for t in EXTRA_TRACKERS:
                handle.add_tracker({"url": t, "tier": 3})
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

        if total > MAX_BYTES:
            await edit(
                bot,
                chat_id,
                msg_id,
                f"حجم تورنت {human(total)} از سقف {human(MAX_BYTES)} بیشتر است.",
            )
            return

        await edit(bot, chat_id, msg_id, f"📥 {name}\nحجم: {human(total)}\nآمادهسازی...")

        last = 0.0
        while True:
            if job["cancel"]:
                await edit(bot, chat_id, msg_id, "لغو شد.")
                return
            st = handle.status()
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
                    f"سرعت: {human(st.download_rate)}/s · سیدر: {st.num_seeds}",
                )
            await asyncio.sleep(2)

        await edit(bot, chat_id, msg_id, "✅ دانلود کامل شد. در حال ارسال...")
        await asyncio.sleep(1)

        files = sorted(p for p in save_dir.rglob("*") if p.is_file())
        if not files:
            await edit(bot, chat_id, msg_id, "فایلی پیدا نشد.")
            return

        size_all = sum(f.stat().st_size for f in files)

        if len(files) > 1 and size_all <= TG_SAFE:
            archive = save_dir / (name + ".zip")
            await asyncio.to_thread(zip_files, files, archive)
            await send_file(bot, chat_id, archive)
        else:
            for f in files:
                await send_file(bot, chat_id, f)

        await bot.send_message(chat_id, f"تمام شد: {name}")

    except Exception as e:
        log.exception("job failed")
        await edit(bot, chat_id, msg_id, f"خطا:\n{e}")
    finally:
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
    msg = await update.message.reply_text("در حال آمادهسازی...")
    asyncio.create_task(run_job(chat_id, msg.message_id, text, job))


application = (
    Application.builder()
    .token(BOT_TOKEN)
    .updater(None)
    .connect_timeout(60)
    .read_timeout(120)
    .write_timeout(900)
    .build()
)

application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("help", cmd_help))
application.add_handler(CommandHandler("cancel", cmd_cancel))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_magnet))


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def hook(request: web.Request) -> web.Response:
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.update_queue.put(update)
    return web.Response(text="ok")


async def poll_loop() -> None:
    offset = 0
    while True:
        try:
            updates = await application.bot.get_updates(
                offset=offset, timeout=30, allowed_updates=["message"]
            )
            for u in updates:
                offset = u.update_id + 1
                await application.update_queue.put(u)
        except Exception:
            await asyncio.sleep(3)


async def keepalive() -> None:
    if not PUBLIC_URL:
        return
    url = PUBLIC_URL + "/"
    await asyncio.sleep(120)
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=30) as r:
                    await r.read()
        except Exception:
            pass
        await asyncio.sleep(600)


async def main() -> None:
    cleanup_old()

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post(f"/{BOT_TOKEN}", hook)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    await application.initialize()
    await application.start()
    await application.bot.set_my_commands(
        [
            BotCommand("start", "شروع"),
            BotCommand("help", "راهنما"),
            BotCommand("cancel", "لغو دانلود"),
        ]
    )

    if PUBLIC_URL:
        url = f"{PUBLIC_URL}/{BOT_TOKEN}"
        await application.bot.set_webhook(
            url, allowed_updates=["message"], drop_pending_updates=True, max_connections=8
        )
        log.info("webhook: %s", url)
        asyncio.create_task(keepalive())
    else:
        log.info("no public url, falling back to polling")
        asyncio.create_task(poll_loop())

    log.info("listening on %s", PORT)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
