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
import json
import base64
import time
import logging
import requests
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

# ============== CONFIG ==============
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
GITHUB_TOKEN       = os.getenv("GITHUB_TOKEN", "PASTE_YOUR_GITHUB_PAT_HERE")
GITHUB_REPO        = os.getenv("GITHUB_REPO", "yourusername/portfolio")  # e.g. "yakob/portfolio"
GITHUB_BRANCH      = os.getenv("GITHUB_BRANCH", "main")
ALLOWED_USER_ID    = int(os.getenv("ALLOWED_USER_ID", "0"))  # your telegram user id — 0 = anyone (NOT recommended)

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
      My project description here.
      Multiple lines OK.
    if no 'Title:' line, first line is title, rest is description.
    """
    if not caption:
        return None, "", []

    title = None
    tags = []
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
        else:
            desc_lines.append(line)

    if title is None and desc_lines:
        title = desc_lines.pop(0)

    description = " ".join(desc_lines).strip()
    return title, description, tags


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

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("⛔ not authorized.")
        return

    msg = update.message
    caption = msg.caption or ""
    title, description, tags = parse_caption(caption)

    if not title:
        await msg.reply_text(
            "⚠️ i need at least a title.\n\n"
            "send the photo again with a caption like:\n"
            "<pre>Title: My Project\nThe description here.</pre>",
            parse_mode="HTML"
        )
        return

    status = await msg.reply_text("📥 downloading photo...")

    try:
        # grab the highest-res version
        photo = msg.photo[-1]
        tg_file = await ctx.bot.get_file(photo.file_id)
        photo_bytes = bytes(await tg_file.download_as_bytearray())

        await status.edit_text("⬆️ uploading to github...")

        ts = int(time.time())
        safe_title = "".join(c if c.isalnum() else "-" for c in title.lower())[:40].strip("-")
        image_filename = f"{ts}-{safe_title}.jpg"
        image_path = f"{IMAGES_DIR}/{image_filename}"

        gh_put_file(image_path, photo_bytes, f"bot: add image for '{title}'")

        await status.edit_text("📝 updating projects.json...")

        projects, sha = load_projects()
        new_project = {
            "title": title,
            "description": description or "no description provided.",
            "image": image_path,
            "tags": tags,
            "timestamp": ts,
            "date": time.strftime("%b %Y", time.localtime(ts)),
        }
        projects.append(new_project)
        save_projects(projects, sha)

        site_url = f"https://{GITHUB_REPO.split('/')[0]}.github.io/{GITHUB_REPO.split('/')[1]}/"
        await status.edit_text(
            f"✅ <b>posted!</b>\n\n"
            f"<b>{title}</b>\n"
            f"tags: {', '.join(tags) if tags else '—'}\n\n"
            f"site rebuilds in ~30s:\n{site_url}",
            parse_mode="HTML"
        )

    except requests.HTTPError as e:
        log.exception("github api error")
        await status.edit_text(f"❌ github error: {e.response.status_code}\n{e.response.text[:300]}")
    except Exception as e:
        log.exception("post failed")
        await status.edit_text(f"❌ failed: {e}")

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
    if TELEGRAM_BOT_TOKEN.startswith("PASTE"):
        raise RuntimeError("set TELEGRAM_BOT_TOKEN in environment or edit the CONFIG block.")
    if GITHUB_TOKEN.startswith("PASTE"):
        raise RuntimeError("set GITHUB_TOKEN in environment or edit the CONFIG block.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_projects))
    app.add_handler(CommandHandler("delete", delete_project))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("bot started. send /start to your bot in telegram.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
