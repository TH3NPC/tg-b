"""
yakob portfolio — telegram bot
==============================
how it works:
  1. you send a photo to your bot, with a caption like:
       Title: Drone Project
       Tags: embedded, robotics, computer-vision
       Smart quadcopter that avoids obstacles in real-time.
       Built on STM32 with custom PID firmware.
  2. bot uploads the image to your github repo (in /images/)
  3. bot updates projects.json in the same repo
  4. github pages serves the updated site within ~30 seconds

REQUIRED ENV VARS (or edit the CONFIG block below):
  TELEGRAM_BOT_TOKEN  — get from @BotFather on telegram
  GITHUB_TOKEN        — fine-grained PAT with "Contents: read/write" on your repo
  GITHUB_REPO         — like "yakob/portfolio"
  GITHUB_BRANCH       — usually "main"
  ALLOWED_USER_ID     — your telegram user id (so only YOU can post)

install:
  pip install python-telegram-bot requests

run:
  python bot.py

usage in telegram:
  /start                — say hi
  /list                 — show all current projects
  /delete <number>      — remove a project (by its number from /list)
  send a photo + caption — adds a new project
"""

import os
import asyncio
import json
import base64
import time
import logging
from urllib.parse import urlparse
import requests
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)


def _get_env(name: str, default: str):
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def normalize_github_repo(value: str) -> str:
    value = value.strip()
    if not value:
        return value

    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        host = parsed.netloc.lower()
        path = parsed.path.strip("/")

        if host.endswith(".github.io"):
            repo_name = path.split("/")[-1] if path else ""
            owner = host.split(".github.io")[0]
            if owner and repo_name:
                return f"{owner}/{repo_name}"

        if host.endswith("github.com"):
            parts = [part for part in path.split("/") if part and part != "repos"]
            if len(parts) >= 2:
                repo_name = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
                return f"{parts[0]}/{repo_name}"

    return value


# ============== CONFIG ==============
TELEGRAM_BOT_TOKEN = _get_env("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
GITHUB_TOKEN       = _get_env("GITHUB_TOKEN", "PASTE_YOUR_GITHUB_PAT_HERE")
GITHUB_REPO        = normalize_github_repo(_get_env("GITHUB_REPO", "yourusername/portfolio"))
GITHUB_BRANCH      = _get_env("GITHUB_BRANCH", "main")
ALLOWED_USER_ID    = int(_get_env("ALLOWED_USER_ID", "0"))


def validate_config():
    missing = []
    if TELEGRAM_BOT_TOKEN.startswith("PASTE_"):
        missing.append("TELEGRAM_BOT_TOKEN")
    if GITHUB_TOKEN.startswith("PASTE_"):
        missing.append("GITHUB_TOKEN")
    if GITHUB_REPO.startswith("yourusername/") and "yourusername" in GITHUB_REPO:
        missing.append("GITHUB_REPO")
    if not GITHUB_REPO or "/" not in GITHUB_REPO:
        missing.append("GITHUB_REPO")
    if ALLOWED_USER_ID == 0:
        missing.append("ALLOWED_USER_ID (set your real Telegram user ID)")
    if missing:
        return False, "Missing or placeholder env values: " + ", ".join(missing)
    return True, "OK"

SITE_URL = _get_env("SITE_URL", "https://yakob-portfolio.vercel.app")

# paths inside the repo
PROJECTS_JSON_PATH = "projects.json"
IMAGES_DIR         = "images"

# ============== LOGGING ==============
logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
log = logging.getLogger("yakob-bot")


# ============== GITHUB API HELPERS ==============
GITHUB_API = "https://api.github.com"

def gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def gh_get_file(path: str):
    """returns (content_bytes, sha) or (None, None) if not found"""
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}?ref={GITHUB_BRANCH}"
    r = requests.get(url, headers=gh_headers(), timeout=30)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    content = base64.b64decode(data["content"])
    return content, data["sha"]

def gh_put_file(path: str, content_bytes: bytes, message: str, sha: str | None = None):
    """create or update a file in the repo"""
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("utf-8"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=gh_headers(), json=payload, timeout=60)
    r.raise_for_status()
    return r.json()


# ============== PROJECTS HELPERS ==============
def load_projects():
    content, sha = gh_get_file(PROJECTS_JSON_PATH)
    if content is None:
        return [], None
    try:
        return json.loads(content.decode("utf-8")), sha
    except json.JSONDecodeError:
        return [], sha

def save_projects(projects: list, sha: str | None):
    content = json.dumps(projects, indent=2, ensure_ascii=False).encode("utf-8")
    return gh_put_file(
        PROJECTS_JSON_PATH,
        content,
        f"bot: update projects ({len(projects)} total)",
        sha
    )


# ============== AUTH ==============
def is_authorized(update: Update) -> bool:
    if ALLOWED_USER_ID == 0:
        return True  # open to all (NOT recommended)
    return update.effective_user and update.effective_user.id == ALLOWED_USER_ID


# ============== CAPTION PARSER ==============
def parse_caption(caption: str):
    """
    parses captions like:
      Title: My Project
      Tags: embedded, iot
      Link: https://myproject.com
      My project description here.
      Multiple lines OK.
    if no 'Title:' line, first line is title, rest is description.
    """
    if not caption:
        return None, "", [], ""

    title = None
    tags = []
    link = ""
    desc_lines = []

    for raw in caption.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("title:"):
            title = line.split(":", 1)[1].strip()
        elif low.startswith("tags:"):
            tags_str = line.split(":", 1)[1]
            tags = [t.strip() for t in tags_str.split(",") if t.strip()]
        elif low.startswith(("link:", "url:", "site:")):
            candidate = line.split(":", 1)[1].strip()
            if candidate.startswith(("http://", "https://")):
                link = candidate
            elif candidate:
                link = "https://" + candidate
        else:
            desc_lines.append(line)

    if title is None and desc_lines:
        title = desc_lines.pop(0)

    description = " ".join(desc_lines).strip()
    return title, description, tags, link


# ============== HANDLERS ==============
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("⛔ not authorized.")
        return
    user_id = update.effective_user.id
    await update.message.reply_text(
        "👋 yakob's portfolio bot is live.\n\n"
        f"your telegram id: <code>{user_id}</code>\n\n"
        "send me a photo + caption to add a project.\n\n"
        "caption format:\n"
        "<pre>Title: Project Name\n"
        "Tags: tag1, tag2, tag3\n"
        "Your description goes here.\n"
        "Multiple lines OK.</pre>\n\n"
        "commands:\n"
        "/list — show projects\n"
        "/delete &lt;number&gt; — remove a project",
        parse_mode="HTML"
    )

async def list_projects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("⛔ not authorized.")
        return
    projects, _ = load_projects()
    if not projects:
        await update.message.reply_text("no projects yet.")
        return
    lines = ["📂 <b>your projects</b>\n"]
    for i, p in enumerate(projects, 1):
        title = p.get("title", "untitled")
        lines.append(f"<b>{i}.</b> {title}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def delete_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("⛔ not authorized.")
        return
    if not ctx.args:
        await update.message.reply_text("usage: /delete <number>  (use /list to see numbers)")
        return
    try:
        idx = int(ctx.args[0]) - 1
    except ValueError:
        await update.message.reply_text("that's not a number.")
        return
    projects, sha = load_projects()
    if idx < 0 or idx >= len(projects):
        await update.message.reply_text(f"no project at #{idx + 1}.")
        return
    removed = projects.pop(idx)
    save_projects(projects, sha)
    await update.message.reply_text(f"🗑️ removed: <b>{removed.get('title', 'untitled')}</b>", parse_mode="HTML")

# albums arrive as separate updates sharing a media_group_id, and only one of them
# carries the caption - so collect them for a moment, then post once.
_albums = {}
ALBUM_WAIT = 3.0

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("\u26d4 not authorized.")
        return

    msg = update.message
    gid = msg.media_group_id

    if not gid:
        await post_project(ctx, msg, [(msg.message_id, msg.photo[-1].file_id)], msg.caption or "")
        return

    album = _albums.get(gid)
    if album is None:
        album = _albums[gid] = {"shots": [], "caption": "", "msg": msg}
        asyncio.create_task(_flush_album(gid, ctx))
    album["shots"].append((msg.message_id, msg.photo[-1].file_id))
    if msg.caption:
        album["caption"] = msg.caption
        album["msg"] = msg


async def _flush_album(gid: str, ctx: ContextTypes.DEFAULT_TYPE):
    await asyncio.sleep(ALBUM_WAIT)
    album = _albums.pop(gid, None)
    if album:
        await post_project(ctx, album["msg"], album["shots"], album["caption"])


async def post_project(ctx: ContextTypes.DEFAULT_TYPE, msg, shots, caption: str):
    """shots is a list of (message_id, file_id); message_id keeps the album in order."""
    title, description, tags, link = parse_caption(caption)

    if not title:
        await msg.reply_text(
            "\u26a0\ufe0f i need at least a title.\n\n"
            "send the photo again with a caption like:\n"
            "<pre>Title: My Project\nLink: https://the-site.com\nThe description here.</pre>",
            parse_mode="HTML"
        )
        return

    shots = sorted(shots, key=lambda shot: shot[0])
    status = await msg.reply_text("\U0001f4e5 downloading %d photo(s)..." % len(shots))

    try:
        ts = int(time.time())
        safe_title = "".join(c if c.isalnum() else "-" for c in title.lower())[:40].strip("-")

        image_paths = []
        for n, (_, file_id) in enumerate(shots):
            tg_file = await ctx.bot.get_file(file_id)
            photo_bytes = bytes(await tg_file.download_as_bytearray())
            await status.edit_text("\u2b06\ufe0f uploading image %d/%d to github..." % (n + 1, len(shots)))
            suffix = "" if n == 0 else "-%d" % (n + 1)
            image_path = f"{IMAGES_DIR}/{ts}-{safe_title}{suffix}.jpg"
            gh_put_file(image_path, photo_bytes, f"bot: add image for '{title}'")
            image_paths.append(image_path)

        await status.edit_text("\U0001f4dd updating projects.json...")

        projects, sha = load_projects()
        new_project = {
            "title": title,
            "description": description or "no description provided.",
            "image": image_paths[0],          # kept so older site versions still work
            "images": image_paths,
            "link": link,
            "tags": tags,
            "timestamp": ts,
            "date": time.strftime("%b %Y", time.localtime(ts)),
        }
        projects.append(new_project)
        save_projects(projects, sha)

        await status.edit_text(
            f"\u2705 <b>posted!</b>\n\n"
            f"<b>{title}</b>\n"
            f"photos: {len(image_paths)}\n"
            f"tags: {', '.join(tags) if tags else '-'}\n"
            f"link: {link or '-'}\n\n"
            f"site rebuilds in ~1 min:\n{SITE_URL}",
            parse_mode="HTML"
        )

    except requests.HTTPError as e:
        log.exception("github api error")
        code = e.response.status_code
        hint = ""
        if code in (401, 403):
            hint = ("\n\n\u26a0\ufe0f your GITHUB_TOKEN is expired, revoked, or missing "
                    "'Contents: read and write'. make a new fine-grained PAT and update the env var.")
        elif code == 404:
            hint = "\n\n\u26a0\ufe0f check GITHUB_REPO - it is currently '%s'." % GITHUB_REPO
        await status.edit_text(f"\u274c github error: {code}\n{e.response.text[:250]}{hint}")
    except Exception as e:
        log.exception("post failed")
        await status.edit_text(f"\u274c failed: {e}")


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "📷 send me a <b>photo</b> with a caption to add a project.\n"
        "(text-only messages don't post.)",
        parse_mode="HTML"
    )


# ============== MAIN ==============
def main():
    ok, why = validate_config()
    if not ok:
        raise RuntimeError(why)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_projects))
    app.add_handler(CommandHandler("delete", delete_project))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Render hands a web service $PORT and $RENDER_EXTERNAL_URL. a polling bot never
    # binds a port, so Render keeps scanning and then fails the deploy - run as a
    # webhook there. anywhere else (laptop, Termux, a worker) keep polling.
    port = os.getenv("PORT")
    base_url = _get_env("RENDER_EXTERNAL_URL", _get_env("WEBHOOK_URL", ""))

    if port and base_url:
        log.info("webhook mode on port %s (%s)", port, base_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=int(port),
            url_path=TELEGRAM_BOT_TOKEN,
            webhook_url=f"{base_url.rstrip('/')}/{TELEGRAM_BOT_TOKEN}",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        log.info("polling mode. send /start to your bot in telegram.")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
