"""Notion -> Telegram task notifier + Mayadeen client portal sync.

Runs on a schedule (GitHub Actions). Each polling cycle does two jobs:

1. Notifier: finds tasks in the iPlugn Tasks database that have an Assignee
   but haven't been notified yet, sends the assignee a Telegram message, then
   checks the task's Notified box.
2. Portal sync: one-way mirror into the client portal database (Mayadeen
   Productions) of every task whose Client/Project is "Mayadeen approval" —
   that select value is the only thing that shares a task with the client.
   Mirrors only Task Name, Status, and Final Link, keyed by Source ID = main
   task page ID. Rows whose task left "Mayadeen approval" (or was deleted)
   are archived. The portal's "comment" field belongs to the client and is
   never written. Client edits are never written back to the main database.

Required environment variables:
    NOTION_TOKEN    Notion integration token
    TELEGRAM_TOKEN  Telegram bot token
    TEAM_MAP        JSON: {name: {"notion_user_id": ..., "chat_id": ...}, ...}
"""

import html
import json
import os
import time
from datetime import datetime

import requests

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
# utf-8-sig strips a UTF-8 BOM in case the secret was pasted from Windows
TEAM_MAP = json.loads(os.environ["TEAM_MAP"].encode("utf-8").decode("utf-8-sig"))

DATA_SOURCE_ID = "e89111c3-6385-43a4-b3d3-0d722bc29981"
PORTAL_DATA_SOURCE_ID = "dac57661-37e6-47c9-9ac0-a8282144e197"
# The single condition that puts a task on the client portal. Tasks tagged
# plain "Mayadeen" stay internal.
PORTAL_CLIENT = "Mayadeen approval"
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


def query_data_source(data_source_id, query_filter=None):
    url = f"https://api.notion.com/v1/data_sources/{data_source_id}/query"
    body = {"filter": query_filter} if query_filter else {}
    results = []
    while True:
        resp = requests.post(url, headers=NOTION_HEADERS, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data["results"])
        if not data.get("has_more"):
            return results
        body["start_cursor"] = data["next_cursor"]


def fetch_unnotified_tasks():
    return query_data_source(
        DATA_SOURCE_ID,
        {
            "and": [
                {"property": "Notified", "checkbox": {"equals": False}},
                {"property": "Assignee", "people": {"is_not_empty": True}},
            ]
        },
    )


def title_text(prop):
    return "".join(part["plain_text"] for part in prop.get("title", []))


def select_name(prop):
    sel = prop.get("select")
    return sel["name"] if sel else None


def rich_text(prop):
    return "".join(part["plain_text"] for part in prop.get("rich_text", []))


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


# --------------------------------------------------------------------------
# Job 2: Mayadeen client portal sync (one-way, main DB -> portal)
# --------------------------------------------------------------------------

def normalize_id(value):
    """Notion page IDs appear both dashed and undashed. Compare them in one
    form so a hand-pasted Source ID still matches its task."""
    return value.replace("-", "").strip().lower()


def portal_payload(page):
    """The only fields the portal mirrors. "comment" is deliberately absent —
    it is the client's column and must never be written by the sync."""
    props = page["properties"]
    return {
        "Task Name": title_text(props["Task Name"]),
        "Status": select_name(props["Status"]),
        "Final Link": props["Final Link"].get("url"),
    }


def portal_current(row):
    props = row["properties"]
    return {
        "Task Name": title_text(props["Task Name"]),
        "Status": select_name(props["Status"]),
        "Final Link": props["Final Link"].get("url"),
    }


def portal_properties(payload, source_id=None):
    name = payload["Task Name"]
    props = {
        # Notion rejects a title part with empty content, so an untitled task
        # mirrors as an empty title rather than a blank text part.
        "Task Name": {"title": [{"text": {"content": name}}] if name else []},
        "Status": (
            {"select": {"name": payload["Status"]}} if payload["Status"] else {"select": None}
        ),
        "Final Link": {"url": payload["Final Link"] or None},
    }
    if source_id is not None:
        props["Source ID"] = {"rich_text": [{"text": {"content": source_id}}]}
    return props


def notion_write(method, url, body):
    """raise_for_status() hides Notion's error body, which is the only part
    that says *why* a write was rejected. Surface it in the log line."""
    resp = requests.request(method, url, headers=NOTION_HEADERS, json=body, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Notion {resp.status_code}: {resp.text[:400]}")
    return resp


def create_portal_row(payload, source_id):
    notion_write(
        "POST",
        "https://api.notion.com/v1/pages",
        {
            "parent": {"type": "data_source_id", "data_source_id": PORTAL_DATA_SOURCE_ID},
            "properties": portal_properties(payload, source_id),
        },
    )


def update_portal_row(row_id, payload):
    notion_write(
        "PATCH",
        f"https://api.notion.com/v1/pages/{row_id}",
        {"properties": portal_properties(payload)},
    )


def archive_portal_row(row_id):
    notion_write(
        "PATCH",
        f"https://api.notion.com/v1/pages/{row_id}",
        {"archived": True},
    )


def sync_portal():
    tasks = query_data_source(
        DATA_SOURCE_ID,
        {"property": "Client/Project", "select": {"equals": PORTAL_CLIENT}},
    )
    rows = query_data_source(PORTAL_DATA_SOURCE_ID)

    # Source ID -> portal rows. A source id should map to exactly one row, but
    # duplicates can appear if a row was copied by hand; keep the oldest as the
    # live row and archive the rest so the mirror stays one-to-one.
    by_source = {}
    orphans = []
    for row in rows:
        source_id = normalize_id(rich_text(row["properties"]["Source ID"]))
        if source_id:
            by_source.setdefault(source_id, []).append(row)
        else:
            orphans.append(row)

    created = updated = archived = failed = 0

    for page in tasks:
        source_id = page["id"]
        task_label = title_text(page["properties"]["Task Name"]) or source_id
        # Claim the row before doing anything that can raise. If this task
        # errors below, its row must still be off the stale list — otherwise a
        # transient failure would archive a live task's portal row.
        matches = sorted(
            by_source.pop(normalize_id(source_id), []),
            key=lambda r: r["created_time"],
        )
        try:
            payload = portal_payload(page)
            if not matches:
                create_portal_row(payload, source_id)
                print(f"PORTAL CREATE '{task_label}'")
                created += 1
                continue
            row, duplicates = matches[0], matches[1:]
            if portal_current(row) != payload:
                update_portal_row(row["id"], payload)
                print(f"PORTAL UPDATE '{task_label}'")
                updated += 1
            for dupe in duplicates:
                archive_portal_row(dupe["id"])
                print(f"PORTAL ARCHIVE duplicate row for '{task_label}'")
                archived += 1
        except Exception as exc:  # one bad task must never kill the run
            print(f"PORTAL ERROR '{task_label}': {sanitize(exc)}")
            failed += 1

    # Whatever is left in by_source points at a task that left
    # "Mayadeen approval" or was deleted. Rows with a blank Source ID were
    # created by hand inside the portal, so the sync leaves them alone.
    stale = [row for rows_ in by_source.values() for row in rows_]

    # Safety valve for a client-facing database: zero matching tasks alongside
    # existing portal rows is far more likely to be a broken query (renamed
    # select option, permission loss) than every task genuinely leaving the
    # project. Refuse to empty the portal on that signal.
    if stale and not tasks:
        print(
            f"PORTAL ABORT archive step: 0 {PORTAL_CLIENT} tasks returned but "
            f"{len(stale)} portal rows exist — refusing to archive them all. "
            "Check the Client/Project filter and the integration's access.",
            flush=True,
        )
        stale = []

    for row in stale:
        label = title_text(row["properties"]["Task Name"]) or row["id"]
        try:
            archive_portal_row(row["id"])
            print(f"PORTAL ARCHIVE '{label}' (source no longer {PORTAL_CLIENT})")
            archived += 1
        except Exception as exc:
            print(f"PORTAL ERROR archiving '{label}': {sanitize(exc)}")
            failed += 1

    print(
        f"Portal: {len(tasks)} {PORTAL_CLIENT} tasks, {created} created, "
        f"{updated} updated, {archived} archived, {failed} failed"
        + (f", {len(orphans)} manual rows left alone" if orphans else ""),
        flush=True,
    )


def run_once():
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
        f"{notified} notified, {skipped} skipped",
        flush=True,
    )


def main():
    """Poll once, or keep polling every minute for LOOP_MINUTES minutes."""
    loop_minutes = int(os.environ.get("LOOP_MINUTES", "0"))
    deadline = time.monotonic() + loop_minutes * 60
    while True:
        # The two jobs are independent: a Notion or Telegram outage in one
        # must not stop the other from running this cycle.
        for job in (run_once, sync_portal):
            try:
                job()
            except Exception as exc:  # e.g. Notion outage — keep the loop alive
                print(f"ERROR {job.__name__} failed: {sanitize(exc)}", flush=True)
        if time.monotonic() + 60 > deadline:
            return
        time.sleep(60)


if __name__ == "__main__":
    main()
