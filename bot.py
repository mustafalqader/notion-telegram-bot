"""Notion -> Telegram task notifier.

Runs on a schedule (GitHub Actions). Finds tasks in the iPlugn Tasks database
that have an Assignee but haven't been notified yet, sends the assignee a
Telegram message, then checks the task's Notified box.

Required environment variables:
    NOTION_TOKEN    Notion integration token
    TELEGRAM_TOKEN  Telegram bot token
    TEAM_MAP        JSON: {name: {"notion_user_id": ..., "chat_id": ...}, ...}
"""

import html
import json
import os
from datetime import datetime

import requests

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TEAM_MAP = json.loads(os.environ["TEAM_MAP"])

DATA_SOURCE_ID = "e89111c3-6385-43a4-b3d3-0d722bc29981"
NOTION_VERSION = "2025-09-03"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

# notion_user_id -> (member name, telegram chat_id)
USER_LOOKUP = {
    member["notion_user_id"]: (name, member["chat_id"])
    for name, member in TEAM_MAP.items()
}


def sanitize(text):
    """Keep secrets out of logs even when they leak into exception text."""
    return str(text).replace(TELEGRAM_TOKEN, "***").replace(NOTION_TOKEN, "***")


def fetch_unnotified_tasks():
    url = f"https://api.notion.com/v1/data_sources/{DATA_SOURCE_ID}/query"
    body = {
        "filter": {
            "and": [
                {"property": "Notified", "checkbox": {"equals": False}},
                {"property": "Assignee", "people": {"is_not_empty": True}},
            ]
        }
    }
    results = []
    while True:
        resp = requests.post(url, headers=NOTION_HEADERS, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data["results"])
        if not data.get("has_more"):
            return results
        body["start_cursor"] = data["next_cursor"]


def title_text(prop):
    return "".join(part["plain_text"] for part in prop.get("title", []))


def select_name(prop):
    sel = prop.get("select")
    return sel["name"] if sel else None


def format_deadline(prop):
    date = prop.get("date")
    if not date or not date.get("start"):
        return None
    dt = datetime.fromisoformat(date["start"][:10])
    return f"{dt:%a} {dt.day} {dt:%b}"  # e.g. "Tue 14 Jul"


def build_message(page):
    props = page["properties"]
    lines = [
        "🎬 مهمة جديدة | New Task",
        f"📌 {html.escape(title_text(props['Task Name']))}",
    ]

    tags = [
        select_name(props["Client/Project"]),
        select_name(props["Priority"]),
    ]
    tags = [html.escape(t) for t in tags if t]
    if tags:
        lines.append("🏷 " + " | ".join(tags))

    deadline = format_deadline(props["Deadline"])
    if deadline:
        lines.append(f"📅 Deadline: {deadline}")

    lines.append(f"🔗 {page['url']}")
    return "\n".join(lines)


def send_telegram(chat_id, text):
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(
            f"Telegram error {resp.status_code}: {body.get('description')}"
        )


def mark_notified(page_id):
    resp = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=NOTION_HEADERS,
        json={"properties": {"Notified": {"checkbox": True}}},
        timeout=30,
    )
    resp.raise_for_status()


def main():
    tasks = fetch_unnotified_tasks()
    notified = skipped = 0
    for page in tasks:
        task_label = title_text(page["properties"]["Task Name"]) or page["id"]
        try:
            assignee_id = page["properties"]["Assignee"]["people"][0]["id"]
            match = USER_LOOKUP.get(assignee_id)
            if match is None:
                print(f"SKIP  '{task_label}': assignee {assignee_id} not in TEAM_MAP")
                skipped += 1
                continue
            member_name, chat_id = match
            send_telegram(chat_id, build_message(page))
            mark_notified(page["id"])
            print(f"SENT  '{task_label}' -> {member_name}")
            notified += 1
        except Exception as exc:  # one bad task must never kill the run
            print(f"ERROR '{task_label}': {sanitize(exc)}")
            skipped += 1
    print(
        f"Summary: {len(tasks)} tasks checked, "
        f"{notified} notified, {skipped} skipped"
    )


if __name__ == "__main__":
    main()
