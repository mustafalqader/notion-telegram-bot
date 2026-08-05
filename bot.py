"""Notion -> Telegram notifier, Mayadeen portal sync, timestamps, decisions.

Runs on a schedule (GitHub Actions). Each polling cycle does four jobs:

1. Notifier: finds tasks in the iPlugn Tasks database that have an Assignee
   but haven't been notified yet, sends the assignee a Telegram message, then
   checks the task's Notified box.
2. Portal sync: one-way mirror into the client portal database (Mayadeen
   Productions) of every task whose Client/Project is "Mayadeen approval" —
   that select value is the only thing that shares a task with the client.
   Mirrors only Task Name and Final Link, keyed by Source ID = main task page
   ID; our internal Status is not shared. Rows whose task left "Mayadeen
   approval" (or was deleted) are archived. The portal's "comment" and
   "Client Decision" columns belong to the client and are never written.
3. Timestamps: stamps "Delivered At" the first cycle a task has a Final Link,
   and "Approved At" the first cycle its Status is Approved, in Asia/Baghdad
   time. Write-once — an existing stamp is never overwritten, so re-pasting a
   link or re-approving keeps the original date. Neither field is mirrored to
   the client portal.
4. Client decisions: reads "Client Decision" from portal rows and Telegrams
   Mustafa when it changes, approving the main task on "✅ Approved". The
   decision last pinged about is stored on the main task, so each decision
   pings once and a changed decision pings again. This is the one place a
   client action reaches the main database, and it only ever sets Status.

Required environment variables:
    NOTION_TOKEN    Notion integration token
    TELEGRAM_TOKEN  Telegram bot token
    TEAM_MAP        JSON: {name: {"notion_user_id": ..., "chat_id": ...}, ...}
"""

import html
import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

try:
    from zoneinfo import ZoneInfo

    BAGHDAD = ZoneInfo("Asia/Baghdad")
except Exception:  # host without a tz database (e.g. a bare Windows box)
    # Iraq dropped DST in 2015 and has been a flat UTC+3 since.
    BAGHDAD = timezone(timedelta(hours=3), "Asia/Baghdad")

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
# utf-8-sig strips a UTF-8 BOM in case the secret was pasted from Windows
TEAM_MAP = json.loads(os.environ["TEAM_MAP"].encode("utf-8").decode("utf-8-sig"))

DATA_SOURCE_ID = "e89111c3-6385-43a4-b3d3-0d722bc29981"
PORTAL_DATA_SOURCE_ID = "dac57661-37e6-47c9-9ac0-a8282144e197"
# The single condition that puts a task on the client portal. Tasks tagged
# plain "Mayadeen" stay internal.
PORTAL_CLIENT = "Mayadeen approval"
APPROVED_STATUS = "Approved"

# Milestone stamps only apply to tasks created on or after the day this was
# deployed. The database carries months of finished work whose Approved At is
# empty; stamping those now would record the deploy time as the approval time,
# and because the stamps are write-once that wrong date would be permanent.
# Backfilling from last_edited_time was considered and rejected: most of those
# tasks were edited long after approval, so it produces wrong data that looks
# right. Older tasks keep empty cells, on purpose, forever.
STAMP_EPOCH = datetime(2026, 8, 5, tzinfo=BAGHDAD)

# Client decision handling (job 4)
DECISION_APPROVED = "✅ Approved"
DECISION_CHANGES = "🔁 Needs Changes"
# Which decision we last pinged about, kept on the MAIN task. Storing it in
# Notion rather than on disk is what makes the ping survive between runs:
# every GitHub Actions run starts with an empty filesystem.
DECISION_STATE_PROP = "Client Decision Notified"
# Client decisions go to Mustafa only, not to the task's assignee.
OWNER_CHAT_ID = int(os.environ.get("OWNER_CHAT_ID", "7469972624"))
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
    """The only fields the portal mirrors. Our internal Status is not among
    them — the client does not see our production stages. "comment" and
    "Client Decision" belong to the client and must never be written."""
    props = page["properties"]
    return {
        "Task Name": title_text(props["Task Name"]),
        "Final Link": props["Final Link"].get("url"),
    }


def portal_current(row):
    props = row["properties"]
    return {
        "Task Name": title_text(props["Task Name"]),
        "Final Link": props["Final Link"].get("url"),
    }


def portal_properties(payload, source_id=None):
    name = payload["Task Name"]
    props = {
        # Notion rejects a title part with empty content, so an untitled task
        # mirrors as an empty title rather than a blank text part.
        "Task Name": {"title": [{"text": {"content": name}}] if name else []},
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


# --------------------------------------------------------------------------
# Job 3: milestone timestamps (Delivered At / Approved At)
# --------------------------------------------------------------------------

def date_start(prop):
    date = prop.get("date")
    return date.get("start") if date else None


def created_after_epoch(page):
    """True if the task is new enough to be stamped. Notion returns
    created_time as UTC with a trailing Z, which fromisoformat wants as
    +00:00 before Python 3.11."""
    created = page["created_time"].replace("Z", "+00:00")
    return datetime.fromisoformat(created) >= STAMP_EPOCH


def fetch_unstamped_tasks():
    """Tasks created since the cutoff that are missing a stamp they earned."""
    return query_data_source(
        DATA_SOURCE_ID,
        {
            "and": [
                {
                    "timestamp": "created_time",
                    "created_time": {"on_or_after": STAMP_EPOCH.isoformat()},
                },
                {
                    "or": [
                        {
                            "and": [
                                {"property": "Final Link", "url": {"is_not_empty": True}},
                                {"property": "Delivered At", "date": {"is_empty": True}},
                            ]
                        },
                        {
                            "and": [
                                {
                                    "property": "Status",
                                    "select": {"equals": APPROVED_STATUS},
                                },
                                {"property": "Approved At", "date": {"is_empty": True}},
                            ]
                        },
                    ]
                },
            ]
        },
    )


def stamp_timestamps():
    tasks = fetch_unstamped_tasks()
    # One timestamp for the whole cycle: tasks stamped in the same pass should
    # agree, and the drift across a single run is not meaningful.
    now = datetime.now(BAGHDAD).isoformat(timespec="seconds")
    delivered = approved = skipped = failed = 0

    for page in tasks:
        props = page["properties"]
        label = title_text(props["Task Name"]) or page["id"]

        # Belt and braces on the cutoff. The stamps are write-once, so a
        # historical task stamped by a bad filter could not be undone by
        # rerunning anything — re-check locally before writing.
        if not created_after_epoch(page):
            skipped += 1
            continue

        # Re-check each field against the page itself. A task can match the
        # query on one branch while the other stamp is already set, and these
        # stamps are write-once: only ever fill a blank, so re-pasting a link
        # or re-approving a task never resets the original time.
        updates = {}
        if props["Final Link"].get("url") and not date_start(props["Delivered At"]):
            updates["Delivered At"] = {"date": {"start": now}}
        if (
            select_name(props["Status"]) == APPROVED_STATUS
            and not date_start(props["Approved At"])
        ):
            updates["Approved At"] = {"date": {"start": now}}
        if not updates:
            continue

        try:
            notion_write(
                "PATCH",
                f"https://api.notion.com/v1/pages/{page['id']}",
                {"properties": updates},
            )
            for field in updates:
                print(f"STAMP {field} on '{label}' = {now}")
            delivered += "Delivered At" in updates
            approved += "Approved At" in updates
        except Exception as exc:  # one bad task must never kill the run
            print(f"STAMP ERROR '{label}': {sanitize(exc)}")
            failed += 1

    print(
        f"Stamps: {len(tasks)} candidates, {skipped} pre-cutoff, "
        f"{delivered} Delivered At, "
        f"{approved} Approved At, {failed} failed",
        flush=True,
    )


# --------------------------------------------------------------------------
# Job 4: client decision notifications (portal -> Telegram, Mustafa only)
# --------------------------------------------------------------------------

def fetch_page(page_id):
    """Returns the page, or None if it is gone or in the trash."""
    resp = requests.get(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=NOTION_HEADERS,
        timeout=30,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise RuntimeError(f"Notion {resp.status_code}: {resp.text[:400]}")
    page = resp.json()
    if page.get("archived") or page.get("in_trash"):
        return None
    return page


def build_decision_message(decision, task_name, comment, task_url):
    name = html.escape(task_name)
    if decision == DECISION_APPROVED:
        lines = ["✅ الميادين وافقت | Client Approved", f"📌 {name}"]
        if comment:
            lines.append(f"💬 {html.escape(comment)}")
        return "\n".join(lines)

    if decision == DECISION_CHANGES:
        header = "🔁 الميادين تطلب تعديلات | Changes Requested"
    else:
        # An option added in Notion that this code predates. Still worth a
        # ping — losing the signal is worse than an unstyled message.
        header = f"📣 قرار جديد من الميادين | {html.escape(decision)}"
    lines = [header, f"📌 {name}"]
    if comment:
        lines.append(f"💬 {html.escape(comment)}")
    lines.append(f"🔗 {task_url}")
    return "\n".join(lines)


def notify_client_decisions():
    rows = query_data_source(
        PORTAL_DATA_SOURCE_ID,
        {"property": "Client Decision", "select": {"is_not_empty": True}},
    )
    sent = skipped = failed = 0

    for row in rows:
        props = row["properties"]
        row_label = title_text(props["Task Name"]) or row["id"]
        try:
            decision = select_name(props["Client Decision"])
            source_id = rich_text(props["Source ID"]).strip()
            if not source_id:
                print(f"DECISION SKIP '{row_label}': portal row has no Source ID")
                skipped += 1
                continue

            task = fetch_page(source_id)
            if task is None:
                print(f"DECISION SKIP '{row_label}': main task {source_id} is gone")
                skipped += 1
                continue

            state_prop = task["properties"].get(DECISION_STATE_PROP)
            if state_prop is None:
                raise RuntimeError(
                    f'main database is missing the "{DECISION_STATE_PROP}" '
                    "text property; add it so decisions can be tracked"
                )

            # Compare against the decision we last pinged about, not a simple
            # "seen" flag: if the client switches from Needs Changes to
            # Approved, the stored value no longer matches and it pings again.
            if rich_text(state_prop).strip() == decision:
                skipped += 1
                continue

            task_name = title_text(task["properties"]["Task Name"]) or row_label
            comment = rich_text(props["comment"]).strip()

            # Send first, record second. A failure between the two re-pings
            # next cycle, which is the better failure: a duplicate message
            # beats silently swallowing a client decision.
            send_telegram(
                OWNER_CHAT_ID,
                build_decision_message(decision, task_name, comment, task["url"]),
            )

            updates = {
                DECISION_STATE_PROP: {"rich_text": [{"text": {"content": decision}}]}
            }
            if decision == DECISION_APPROVED:
                # Job 3 turns this into an "Approved At" stamp later this run.
                updates["Status"] = {"select": {"name": APPROVED_STATUS}}
            notion_write(
                "PATCH",
                f"https://api.notion.com/v1/pages/{task['id']}",
                {"properties": updates},
            )

            print(f"DECISION SENT '{task_name}': {decision}")
            sent += 1
        except Exception as exc:  # one bad row must never kill the run
            print(f"DECISION ERROR '{row_label}': {sanitize(exc)}")
            failed += 1

    print(
        f"Decisions: {len(rows)} decided rows, {sent} notified, "
        f"{skipped} skipped, {failed} failed",
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
        # Decisions run before stamping so an approval coming back from the
        # client gets its "Approved At" in the same cycle, not the next one.
        for job in (run_once, sync_portal, notify_client_decisions, stamp_timestamps):
            try:
                job()
            except Exception as exc:  # e.g. Notion outage — keep the loop alive
                print(f"ERROR {job.__name__} failed: {sanitize(exc)}", flush=True)
        if time.monotonic() + 60 > deadline:
            return
        time.sleep(60)


if __name__ == "__main__":
    main()
