"""Notion -> Telegram notifier, portal sync, timestamps, decisions, hours ledger.

Runs on a schedule (GitHub Actions). Six jobs are described below; three of
them run — job 3 is disabled and jobs 5 and 6 are gated behind LEDGER_ENABLED,
see main(). Each polling cycle:

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
3. Timestamps: DISABLED, see main(). Stamps "Delivered At" the first cycle a
   task has a Final Link, and "Approved At" the first cycle its Status is
   Approved, in Asia/Baghdad time. Write-once — an existing stamp is never
   overwritten, so re-pasting a link or re-approving keeps the original date.
   Neither field is mirrored to the client portal. Turned off because its
   cutoff cannot tell a new task from an old one entered late; both fields
   are maintained by hand meanwhile.
4. Client decisions: reads "Client Decision" from portal rows and Telegrams
   the main task's assignee *and* Mustafa when it changes, approving the main
   task on "✅ Approved". The decision last pinged about is stored on the main
   task, so each decision pings once and a changed decision pings again. This
   is the one place a client action reaches the main database, and it only
   ever sets Status.
5. Hours ledger: hourly, sums the Hours formula over each person's tasks whose
   Deadline falls in the current half-month period (the 1st to the 15th, or the
   16th to the last day, Baghdad) and whose Status is Review or Approved, into
   the Hours Ledger database, one row per person per period, keyed by
   Person + Period. New and In Progress tasks count 0 until they move. Everyone
   gets a row even at zero hours, so an empty period is visible rather than
   missing. A new row carries in the previous period's Carry-out, so a
   shortfall follows a person into the next period and raises their Target;
   a surplus does not carry, because overtime is paid rather than banked.
   The bot writes Actual Hours, and Base Target and Carry-in once at creation.
   Target, Remaining, Overtime, Carry-out and التقدم are Notion's formulas and
   are never written.
6. Period close: when a period ends — the 16th, and the 1st of next month —
   refreshes the finished period's numbers, Telegrams each person their period
   and Mustafa all five in one message, then ticks Closed. It fires on the
   condition "last period still has an open row", not on a clock instant, and
   it sends before it closes so a failed send is retried rather than buried.
   Closed rows are never touched again by either ledger job.

Both ledger jobs ignore every period ending before LEDGER_START, so the
database's months of older work are never created, closed, reported or
carried from. Sep 1-15 2026 is the first live period and carries in 0.

Jobs 5 and 6 only run when LEDGER_ENABLED is set. Preview them against live
data without writing or sending anything:

    python bot.py --dry-run
    python bot.py --dry-run --as-of 2026-09-16T09:30

Required environment variables:
    NOTION_TOKEN    Notion integration token
    TELEGRAM_TOKEN  Telegram bot token
    TEAM_MAP        JSON: {name: {"notion_user_id": ..., "chat_id": ...}, ...}
    LEDGER_ENABLED  "true" to run jobs 5 and 6 in the polling loop (default off)
"""

import argparse
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
# Client decisions go to the main task's assignee and to Mustafa, who is
# copied on every decision whether or not he owns the task.
OWNER_CHAT_ID = int(os.environ.get("OWNER_CHAT_ID", "7469972624"))
NOTION_VERSION = "2025-09-03"

# Half-month hours ledger (jobs 5 and 6)
LEDGER_DATA_SOURCE_ID = "729ea6ac-d3cf-49f7-9cd8-df82751119dc"
# Computed by Notion. Never sent in a write. Target is among them now: it is
# Base Target + Carry-in, so the bot writes those two operands and lets Notion
# do the sum.
LEDGER_FORMULA_PROPS = frozenset(
    {"Target", "Remaining", "Overtime", "Carry-out", "التقدم"}
)
# Only work that reached the client counts toward the ledger. A task still in
# New or In Progress contributes 0 hours until it moves — it is not skipped or
# flagged, it simply is not work yet. Compared case-folded because the database
# mixes cases across its Status options ("Review", "cancelled").
COUNTED_STATUSES = frozenset({"review", "approved"})
# Written to Base Target on create. The Target a person is actually judged
# against is this plus whatever they carried in, and Notion computes that.
LEDGER_BASE_TARGET = 104

# Nothing before this exists as far as the ledger is concerned. A period that
# ends earlier is never created, never closed, never reported and never
# carried from — the database holds months of finished work, and the close
# fires on a condition rather than a clock, so without this the first enabled
# run would reach back and message five people about a period that ended weeks
# ago.
#
# A period's *end* is compared, not its start, and the end is exclusive. So the
# first live period is Sep 1-15, which ends at Sep 16 00:00; Aug 16-31, ending
# at Sep 1, is out. Move this date to move the whole boundary.
LEDGER_START = datetime(2026, 9, 16, tzinfo=BAGHDAD)
# The period this process has already closed, so the trigger condition is only
# paid for once. Deliberately not persistence: a restarted run re-checks
# against Notion, where Closed is the durable record.
_closed_period = None
# Monotonic mark of the last successful ledger pass; see main().
_last_ledger_run = None
# Both ledger jobs stay out of the polling loop until this is set. They write
# to a live database and message five people, so switching them on is a
# separate, deliberate act from deploying the code.
LEDGER_ENABLED = os.environ.get("LEDGER_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

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
        if resp.status_code >= 400:
            # raise_for_status() reports only "400 Bad Request for url ...",
            # which for a query is never the useful half. Notion says which
            # part of the filter it rejected in the response body, and the
            # filter we sent is the other half of the story — a 400 here is
            # almost always a malformed filter, so print both.
            raise RuntimeError(
                f"Notion {resp.status_code} querying data source "
                f"{data_source_id}: {resp.text[:600]}\n"
                f"  request body: {json.dumps(body, ensure_ascii=False)[:1200]}"
            )
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
    +00:00 before Python 3.11.

    Fails closed. A page with no created_time, or one that will not parse, is
    treated as pre-cutoff and left alone: the stamps are write-once, so being
    wrong in the permissive direction burns a permanent fake date into the
    record, while being wrong in the strict direction only leaves a cell
    blank until someone looks.

    Note what this does *not* mean. It is the age of the Notion row, not the
    age of the work. A task typed in today about a job finished in June is
    "after the epoch" and will be stamped with today's date.
    """
    created = (page.get("created_time") or "").replace("Z", "+00:00")
    if not created:
        return False
    try:
        return datetime.fromisoformat(created) >= STAMP_EPOCH
    except ValueError:
        return False


def fetch_unstamped_tasks():
    """Tasks created since the cutoff that are missing a stamp they earned.

    The cutoff is repeated inside both branches instead of being wrapped
    around them. Notion allows a compound filter to nest two levels deep, and
    and[ cutoff, or[ and[...], and[...] ] ] is three — Notion rejects the
    whole query with a 400 and the stamp job dies every cycle. Written as
    or[ and[...], and[...] ] it is two levels, at the cost of naming the
    cutoff twice.
    """
    since = {
        "timestamp": "created_time",
        "created_time": {"on_or_after": STAMP_EPOCH.isoformat()},
    }
    return query_data_source(
        DATA_SOURCE_ID,
        {
            "or": [
                {
                    "and": [
                        since,
                        {"property": "Final Link", "url": {"is_not_empty": True}},
                        {"property": "Delivered At", "date": {"is_empty": True}},
                    ]
                },
                {
                    "and": [
                        since,
                        {"property": "Status", "select": {"equals": APPROVED_STATUS}},
                        {"property": "Approved At", "date": {"is_empty": True}},
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
            print(
                f"STAMP SKIP '{label}': row created {page.get('created_time')}, "
                f"before cutoff {STAMP_EPOCH.isoformat()}"
            )
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
                # The row's age is printed next to every stamp on purpose.
                # These writes cannot be undone by the bot, so the log has to
                # carry the evidence for why each one was allowed.
                print(
                    f"STAMP {field} on '{label}' = {now} "
                    f"(row created {page.get('created_time')})"
                )
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
# Job 4: client decision notifications (portal -> Telegram: assignee + Mustafa)
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


def build_decision_message(decision, task_name, assignee_name, comment, task_url):
    if decision == DECISION_APPROVED:
        header = "✅ الميادين وافقت | Client Approved"
    elif decision == DECISION_CHANGES:
        header = "🔁 الميادين تطلب تعديلات | Changes Requested"
    else:
        # An option added in Notion that this code predates. Still worth a
        # ping — losing the signal is worse than an unstyled message.
        header = f"📣 قرار جديد من الميادين | {html.escape(decision)}"

    lines = [header, f"📌 {html.escape(task_name)}"]
    # These messages now land in more than one inbox, so name the owner: the
    # assignee sees the client answered *their* task, Mustafa sees whose it is.
    # Omitted entirely when nobody could be resolved, rather than "unassigned".
    if assignee_name:
        lines.append(f"👤 {html.escape(assignee_name)}")
    if comment:
        lines.append(f"💬 {html.escape(comment)}")
    # Every decision carries the link, approvals included: they reach the
    # editor now, and a name without a way back to the task is half a message.
    lines.append(f"🔗 {task_url}")
    return "\n".join(lines)


def decision_recipients(task, label):
    """Who hears about a client decision: the main task's assignee, plus
    Mustafa, always.

    Returns (assignee_name, chat_ids). chat_ids is de-duplicated by string
    value — TEAM_MAP may hold a chat id as a number or a string — so Mustafa
    owning the task means one message, not two. Assignee comes first: it is
    their task, and Mustafa is the copy. An unassigned task, or an assignee
    missing from TEAM_MAP, falls back to Mustafa alone and says so in the log.
    """
    assignee_name = None
    chat_ids = []

    people = task["properties"].get("Assignee", {}).get("people", [])
    if not people:
        print(f"DECISION FALLBACK '{label}': main task has no assignee — owner only")
    else:
        assignee_id = people[0]["id"]
        match = USER_LOOKUP.get(assignee_id)
        if match is None:
            print(
                f"DECISION FALLBACK '{label}': assignee {assignee_id} "
                "not in TEAM_MAP — owner only"
            )
        else:
            assignee_name, chat_id = match
            chat_ids.append(chat_id)

    chat_ids.append(OWNER_CHAT_ID)

    seen = set()
    unique = []
    for chat_id in chat_ids:
        key = str(chat_id).strip()
        if key not in seen:
            seen.add(key)
            unique.append(chat_id)
    return assignee_name, unique


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
            #
            # This skip used to be the one silent path in the job, which made
            # "2 skipped" indistinguishable from a missing Source ID or a
            # deleted task. It prints the stored value now: if a decision is
            # not arriving, this line says whether the bot thinks it already
            # sent it, and what it thinks it sent.
            notified_state = rich_text(state_prop).strip()
            if notified_state == decision:
                print(
                    f"DECISION SKIP '{row_label}': already notified about "
                    f"'{decision}'"
                )
                skipped += 1
                continue

            task_name = title_text(task["properties"]["Task Name"]) or row_label
            comment = rich_text(props["comment"]).strip()
            assignee_name, chat_ids = decision_recipients(task, row_label)
            message = build_decision_message(
                decision, task_name, assignee_name, comment, task["url"]
            )

            # Send first, record second. A failure between the two re-pings
            # next cycle, which is the better failure: a duplicate message
            # beats silently swallowing a client decision.
            #
            # Each recipient is sent independently and one failure does not
            # abort the rest: a teammate who never opened the bot chat returns
            # a permanent Telegram error, and letting that block the state
            # write would re-ping everyone else every cycle, forever. One
            # delivery is enough to consider the decision announced; the
            # recipients that failed are named in the log.
            delivered = []
            failures = []
            for chat_id in chat_ids:
                try:
                    send_telegram(chat_id, message)
                    delivered.append(chat_id)
                except Exception as exc:
                    failures.append(f"{chat_id}: {sanitize(exc)}")
                    print(
                        f"DECISION ERROR '{task_name}' -> {chat_id}: {sanitize(exc)}"
                    )

            # The state write below is the thing that stops this decision ever
            # pinging again, so nothing may reach it without a message having
            # actually left. Raising here keeps the state untouched, which is
            # what lets the next cycle retry — recording a decision nobody
            # received would bury it permanently.
            if not delivered:
                raise RuntimeError(
                    "no recipient could be reached ("
                    + "; ".join(failures)
                    + f") — leaving {DECISION_STATE_PROP} unwritten so the "
                    "next cycle retries"
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

            print(
                f"DECISION SENT '{task_name}': {decision} -> "
                f"{len(delivered)}/{len(chat_ids)} recipients "
                f"{delivered}"
            )
            sent += 1
        except Exception as exc:  # one bad row must never kill the run
            print(f"DECISION ERROR '{row_label}': {sanitize(exc)}")
            failed += 1

    print(
        f"Decisions: {len(rows)} decided rows, {sent} notified, "
        f"{skipped} skipped, {failed} failed",
        flush=True,
    )


# --------------------------------------------------------------------------
# Jobs 5 and 6: the half-month hours ledger, and the close that ends a period
# --------------------------------------------------------------------------

def period_of(moment):
    """The half-month containing `moment`, as [start, end) Baghdad midnights.

    Two periods a month: the 1st to the 15th, and the 16th to the last day.
    The end is exclusive, so a deadline belongs to exactly one period and no
    day is claimed by two.
    """
    local = moment.astimezone(BAGHDAD)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if local.day <= 15:
        return midnight.replace(day=1), midnight.replace(day=16)
    # The 1st of next month, reached without knowing this month's length:
    # 32 days past the 1st always lands inside the following month.
    next_first = (midnight.replace(day=1) + timedelta(days=32)).replace(day=1)
    return midnight.replace(day=16), next_first


def previous_period(start):
    """The period immediately before the one beginning at `start`.

    The day before a period's first day is the last day of the one before it,
    so this crosses a month boundary without any month arithmetic.
    """
    return period_of(start - timedelta(days=1))


def period_label(start):
    """The Period text a row is keyed by: "Sep 1-15" or "Sep 16-31".

    The second half is written "16-31" in every month, February included. It
    is a label, not a range — it only has to read the same here as it does in
    the Period formula on the tasks database, because those two strings are
    what a human lines up when checking a row. Which days are actually in the
    period is period_of's business, and that one does know month lengths.
    """
    return f"{start:%b} 1-15" if start.day == 1 else f"{start:%b} 16-31"


def period_is_live(end):
    """True if a period falls inside the ledger's history.

    Takes the period's exclusive end, so the boundary sits between two whole
    periods and never cuts one in half: Sep 1-15 ends at Sep 16 00:00 and is
    live, Aug 16-31 ends at Sep 1 and is not.
    """
    return end >= LEDGER_START


def parse_deadline(prop):
    """A Deadline as an aware Baghdad datetime, or None if unset/unparseable.

    Deadlines come in both shapes: a bare "2026-09-07" typed in the UI, and a
    full "2026-09-07T14:00:00.000+03:00" when someone sets a time. A bare date
    is read as midnight *in Baghdad*, not UTC — otherwise a deadline on the
    16th would land in the previous period for three hours a day.
    """
    date = prop.get("date")
    if not date or not date.get("start"):
        return None
    raw = date["start"]
    try:
        if len(raw) == 10:
            return datetime.fromisoformat(raw).replace(tzinfo=BAGHDAD)
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(BAGHDAD)
    except ValueError:
        return None


def formula_number(prop):
    """The numeric value of a formula property, or None if it is not a number.

    Never raises on an unexpected formula type: a Hours formula that starts
    returning text should show up as an unreadable task in the summary line,
    not kill the run.
    """
    formula = prop.get("formula") or {}
    return formula.get("number") if formula.get("type") == "number" else None


def number_value(prop):
    return (prop or {}).get("number")


def checkbox_value(prop):
    return bool((prop or {}).get("checkbox"))


def target_of(props):
    """A row's Target: Base Target + Carry-in, as Notion computed it.

    Falls back to that same sum taken from the row's own two number columns,
    but only when Notion returned nothing for the formula — "ساعاتك: 97 من —"
    helps nobody, and both operands are sitting right there on the row. That
    is the whole of this fallback: no other formula is ever reconstructed
    locally, Carry-out least of all.

    Base Target is tested against None rather than falsiness, because 0 is a
    real value — someone on leave for the period — and reading it as 104 would
    tell them they are 104 hours short of a target nobody set them.
    """
    target = formula_number(props["Target"])
    if target is not None:
        return target
    base = number_value(props["Base Target"])
    base = LEDGER_BASE_TARGET if base is None else base
    return base + (number_value(props["Carry-in"]) or 0.0)


def carry_out_of(row):
    """What a row hands to the next period, or 0 when there is no such row.

    Read exactly as Notion computed it and never recomputed here. Two rules
    live inside that formula and are invisible from this side: a surplus does
    not carry (overtime is paid, not banked), and ticking "Reset Carry"
    forgives the shortfall. Working the number out locally would quietly
    break both — the same reason the old weekly ledger read Week Balance back
    off the row instead of deriving it.

    An unreadable formula carries 0. Of the two ways to be wrong, inventing a
    debt someone then has to argue their way out of is the worse one.
    """
    if row is None:
        return 0.0
    return formula_number(row["properties"]["Carry-out"]) or 0.0


def fmt_hours(value):
    """104.0 -> "104", 47.5 -> "47.5". Whole hours read better without ".0"."""
    if value is None:
        return "—"
    rounded = round(float(value), 2)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:g}"


def ledger_write(method, url, properties, extra=None):
    """Write to the ledger, refusing to touch a formula column.

    Remaining and Overtime are computed by Notion. Sending either is a 400 at
    best and silent nonsense at worst, so the guard raises before the request
    rather than after — this bot has a history of writing values that could
    not be taken back.
    """
    forbidden = LEDGER_FORMULA_PROPS.intersection(properties)
    if forbidden:
        raise RuntimeError(
            f"refusing to write formula field(s) {sorted(forbidden)} — "
            "Notion computes these"
        )
    body = dict(extra or {})
    body["properties"] = properties
    return notion_write(method, url, body)


def period_actuals(start, end):
    """{notion_user_id: hours} for one period, and the count of unreadable tasks.

    Sums the Hours formula over tasks whose Deadline falls in [start, end)
    and whose Status is Review or Approved. Anything else — New, In Progress,
    cancelled — contributes nothing, so a person's hours climb only as their
    work reaches the client, and an untouched task in the period reads as 0
    rather than as time already earned.

    The Notion filter is deliberately a day wider on each side and the real
    boundary is applied locally. Notion compares datetimes in UTC, so a filter
    written in Baghdad dates clips tasks near midnight at the edges of the
    period; fetching a little extra and bucketing here is exact.

    Hours land on the *first* assignee, matching how job 1 decides who to
    notify. A task shared between two people counts once, for its owner.
    """
    tasks = query_data_source(
        DATA_SOURCE_ID,
        {
            "and": [
                {
                    "property": "Deadline",
                    "date": {
                        "on_or_after": (start - timedelta(days=1)).date().isoformat()
                    },
                },
                {
                    "property": "Deadline",
                    "date": {"before": (end + timedelta(days=1)).date().isoformat()},
                },
            ]
        },
    )

    totals = {}
    unreadable = 0
    for page in tasks:
        props = page["properties"]
        if (select_name(props["Status"]) or "").strip().lower() not in COUNTED_STATUSES:
            continue
        due = parse_deadline(props["Deadline"])
        if due is None or not (start <= due < end):
            continue
        people = props.get("Assignee", {}).get("people", [])
        if not people:
            continue
        hours = formula_number(props["Hours"])
        if hours is None:
            unreadable += 1
            continue
        owner = people[0]["id"]
        totals[owner] = totals.get(owner, 0.0) + hours
    return totals, unreadable


def ledger_rows_for(label):
    """Every ledger row carrying this Period label — at most one per person."""
    return query_data_source(
        LEDGER_DATA_SOURCE_ID,
        {"property": "Period", "rich_text": {"equals": label}},
    )


def index_ledger(rows):
    """{(person, period label): row}, ignoring rows missing either key."""
    indexed = {}
    for row in rows:
        props = row["properties"]
        person = select_name(props["Person"])
        label = rich_text(props["Period"]).strip()
        if person and label:
            indexed[(person, label)] = row
    return indexed


def sync_period(start, end, dry_run=False):
    """Upsert every person's row for one period and return what each holds.

    Writes three numbers and only these: Actual Hours on every pass, plus Base
    Target and Carry-in at the moment a row is created. Target, Remaining,
    Overtime, Carry-out and التقدم belong to Notion and are read back off the
    row after the write, never recomputed.

    Carry-in is set once, at creation, from the previous period's Carry-out.
    It is deliberately not refreshed afterwards: the previous period is closed
    by then, and a carry that moved under someone mid-period would make the
    target they were working toward change beneath them.

    A row with Closed ticked is history: it is read for its numbers and never
    written to.
    """
    label = period_label(start)
    if not period_is_live(end):
        # Before the ledger existed. Returning no entries is what stops the
        # caller writing a row, sending a report or closing anything.
        print(
            f"Ledger: {label} ends before the ledger start "
            f"({LEDGER_START:%d %b %Y}) — nothing to do",
            flush=True,
        )
        return []

    prev_start, prev_end = previous_period(start)
    prev_label = period_label(prev_start)
    prev_is_live = period_is_live(prev_end)
    actuals, unreadable = period_actuals(start, end)
    indexed = index_ledger(ledger_rows_for(label))
    # The previous period's rows, needed only to read Carry-out off them when
    # a row has to be created. Fetched at most once per pass, and not at all
    # on the hourly passes that create nothing — which is all of them but the
    # first of each period.
    prev_indexed = None

    def carry_in_for(person):
        """What `person` carries into this period: the previous period's
        Carry-out, or 0 when that period is outside the ledger's history.

        The first live period carries in 0 for everyone by construction, which
        is the point of the start date — no shortfall is inherited from work
        the ledger never tracked.
        """
        nonlocal prev_indexed
        if not prev_is_live:
            return 0.0
        if prev_indexed is None:
            prev_indexed = index_ledger(ledger_rows_for(prev_label))
        return carry_out_of(prev_indexed.get((person, prev_label)))

    entries = []
    created = updated = closed_skips = failed = 0

    for name, member in TEAM_MAP.items():
        actual = round(actuals.get(member["notion_user_id"], 0.0), 2)
        row = indexed.get((name, label))
        entry = {
            "name": name,
            "chat_id": member["chat_id"],
            "period": label,
            "actual": actual,
            "target": float(LEDGER_BASE_TARGET),
            "carry_in": 0.0,
            "remaining": None,
            "overtime": None,
            "closed": False,
            "estimated": False,
            "action": "unchanged",
            "row_id": row["id"] if row else None,
        }
        try:
            if row is not None and checkbox_value(row["properties"]["Closed"]):
                # A closed period is history. Report what it says, change
                # nothing.
                props = row["properties"]
                entry.update(
                    actual=number_value(props["Actual Hours"]),
                    target=target_of(props),
                    carry_in=number_value(props["Carry-in"]) or 0.0,
                    remaining=formula_number(props["Remaining"]),
                    overtime=formula_number(props["Overtime"]),
                    closed=True,
                    action="closed",
                )
                print(f"LEDGER SKIP {name} {label}: row is closed")
                closed_skips += 1
                entries.append(entry)
                continue

            stored_actual = (
                number_value(row["properties"]["Actual Hours"]) if row else None
            )

            if dry_run:
                # Nothing is written, so Notion's formulas still describe the
                # old Actual Hours. Quoting them as if they were the new
                # numbers would be a preview of the wrong period, so anything
                # downstream of a value this run did not store is estimated
                # from the plain rule and flagged.
                if row is not None:
                    entry["target"] = target_of(row["properties"])
                    entry["carry_in"] = (
                        number_value(row["properties"]["Carry-in"]) or 0.0
                    )
                if row is None:
                    # Carry-in is readable even here: it comes off the previous
                    # period's row, which does exist. Only this row's own
                    # formulas are out of reach, so the target is quoted as
                    # the sum they would be given.
                    entry["carry_in"] = carry_in_for(name)
                    entry["target"] = LEDGER_BASE_TARGET + entry["carry_in"]
                    entry["action"] = "would create"
                    created += 1
                elif stored_actual != actual:
                    entry["action"] = (
                        f"would set actual {fmt_hours(stored_actual)} -> "
                        f"{fmt_hours(actual)}"
                    )
                    updated += 1
                if row is not None and stored_actual == actual:
                    # Unchanged, so the stored formulas are already correct.
                    entry["remaining"] = formula_number(
                        row["properties"]["Remaining"]
                    )
                    entry["overtime"] = formula_number(row["properties"]["Overtime"])
                else:
                    entry["remaining"] = round(max(0.0, entry["target"] - actual), 2)
                    entry["overtime"] = round(max(0.0, actual - entry["target"]), 2)
                    entry["estimated"] = True
                print(
                    f"  {name:<10} actual {fmt_hours(actual):>5} / "
                    f"{fmt_hours(entry['target']):<4} "
                    f"carry-in {fmt_hours(entry['carry_in']):>5}  "
                    f"left {fmt_hours(entry['remaining']):>5}  "
                    f"extra {fmt_hours(entry['overtime']):>5}  "
                    f"[{entry['action']}"
                    + ("; formulas estimated" if entry["estimated"] else "")
                    + "]"
                )
                entries.append(entry)
                continue

            if row is None:
                # An untouched period must still be visible in the ledger, so
                # a person with no tasks gets a row reading 0, not no row.
                #
                # This is the one moment Carry-in is written, so the previous
                # period's Carry-out is read here and nowhere else. A person
                # with no previous row — a new hire, or the first period the
                # ledger ever ran — starts clean at 0.
                carry_in = carry_in_for(name)
                created_row = ledger_write(
                    "POST",
                    "https://api.notion.com/v1/pages",
                    {
                        "Record": {
                            "title": [{"text": {"content": f"{name} — {label}"}}]
                        },
                        "Person": {"select": {"name": name}},
                        "Period": {"rich_text": [{"text": {"content": label}}]},
                        "Base Target": {"number": LEDGER_BASE_TARGET},
                        "Carry-in": {"number": carry_in},
                        "Actual Hours": {"number": actual},
                    },
                    extra={
                        "parent": {
                            "type": "data_source_id",
                            "data_source_id": LEDGER_DATA_SOURCE_ID,
                        }
                    },
                ).json()
                row = fetch_page(created_row["id"]) or created_row
                entry["row_id"] = row["id"]
                entry["action"] = "created"
                created += 1
            elif stored_actual != actual:
                ledger_write(
                    "PATCH",
                    f"https://api.notion.com/v1/pages/{row['id']}",
                    {"Actual Hours": {"number": actual}},
                )
                # Re-read: Remaining and Overtime are computed downstream of
                # the number just written, so take them from Notion after the
                # write rather than from the pre-write copy.
                row = fetch_page(row["id"]) or row
                entry["action"] = "updated"
                updated += 1

            props = row["properties"]
            entry["target"] = target_of(props)
            entry["carry_in"] = number_value(props["Carry-in"]) or 0.0
            entry["remaining"] = formula_number(props["Remaining"])
            entry["overtime"] = formula_number(props["Overtime"])
            if entry["action"] != "unchanged":
                # Every other job in this file logs the writes it makes; a
                # ledger that moved someone's hours and said nothing would be
                # the one place a wrong number has no trail. The carry is on
                # the line too: it is written once and never revisited, so the
                # log is the only record of what this row was handed.
                print(
                    f"LEDGER {entry['action'].upper()} {name} {label}: actual "
                    f"{fmt_hours(actual)}/{fmt_hours(entry['target'])}, "
                    f"carry-in {fmt_hours(entry['carry_in'])}, "
                    f"left {fmt_hours(entry['remaining'])}, "
                    f"extra {fmt_hours(entry['overtime'])}"
                )
            entries.append(entry)
        except Exception as exc:  # one bad person must never kill the period
            print(f"LEDGER ERROR {name} {label}: {sanitize(exc)}")
            entry["action"] = "failed"
            entries.append(entry)
            failed += 1

    print(
        f"Ledger{' (dry run)' if dry_run else ''}: {label}, "
        f"{created} created, {updated} updated, {closed_skips} closed, "
        f"{failed} failed"
        + (f", {unreadable} task(s) with unreadable Hours" if unreadable else ""),
        flush=True,
    )
    return entries


def run_ledger(now=None, dry_run=False):
    """Job 5: keep the current period's ledger rows current."""
    start, end = period_of(now or datetime.now(BAGHDAD))
    return sync_period(start, end, dry_run=dry_run)


def build_person_report(entry):
    lines = [
        f"📊 تقريرك | {html.escape(entry['period'])}",
        f"⏱️ ساعاتك: {fmt_hours(entry['actual'])} من {fmt_hours(entry['target'])}",
    ]
    # Only shown when there is a carry, so an ordinary period reads the way it
    # always did. The target above already includes it; this line says why it
    # is not the plain 104.
    carry_in = entry["carry_in"]
    if carry_in and carry_in > 0:
        lines.append(f"(منها {fmt_hours(carry_in)} مرحّلة من الفترة السابقة)")
    remaining = entry["remaining"]
    lines.append(
        f"⚠️ ناقصك {fmt_hours(remaining)} ساعة — تنتقل للفترة الجاية"
        if remaining is not None and remaining > 0
        else "✅ كملت الهدف"
    )
    overtime = entry["overtime"]
    if overtime is not None and overtime > 0:
        lines.append(f"💰 ساعات إضافية: {fmt_hours(overtime)}")
    return "\n".join(lines)


def build_owner_report(entries, label):
    """Mustafa's whole-team view: all five in one message, furthest behind first.

    Laid out as a monospace block with Latin headers: a column-aligned table
    mixing Arabic headers with Latin names and digits gets reordered by the
    bidi algorithm and the columns stop lining up.
    """
    ordered = sorted(
        entries, key=lambda e: (e["remaining"] is None, -(e["remaining"] or 0))
    )
    width = max([len("Name")] + [len(e["name"]) for e in ordered])
    header = f"{'Name':<{width}}  {'Hours':>5}  {'Left':>5}  {'Extra':>5}"
    body = [header, "-" * len(header)]
    for entry in ordered:
        body.append(
            f"{entry['name']:<{width}}  {fmt_hours(entry['actual']):>5}  "
            f"{fmt_hours(entry['remaining']):>5}  {fmt_hours(entry['overtime']):>5}"
        )
    table = html.escape("\n".join(body))
    return f"📊 تقرير الفريق | {html.escape(label)}\n<pre>{table}</pre>"


def run_period_close(now=None, dry_run=False):
    """Job 6: close the period that just ended — report it, then tick Closed.

    Fires on a condition, never on a clock instant: any cycle whose previous
    period still has a row that is not Closed. The schedule this bot runs on
    is throttled hard enough that a given minute is not guaranteed to have a
    live runner, and a report waiting for one would be lost whenever it did
    not. Closed is both the end state this job leaves behind and the flag that
    stops it sending twice, so a missed changeover still goes out — late,
    once, and correct.

    Sends before closing, deliberately. A close that lands before a failed
    send hides the period forever, while a send that lands before a failed
    close costs one duplicate next cycle. Job 4 makes the same trade.
    """
    global _closed_period
    now = now or datetime.now(BAGHDAD)
    current_start, _ = period_of(now)
    last_start, last_end = previous_period(current_start)
    label = period_label(last_start)
    if _closed_period == label:
        return None

    if not period_is_live(last_end):
        # Outside the ledger's history. Reuses the same memo as an already
        # closed period so this is said once per process rather than every
        # minute for however long the start date is still in the future — and
        # so nothing below runs: no query, no rows, no messages.
        if not dry_run:
            _closed_period = label
        print(
            f"Close: {label} ends before the ledger start "
            f"({LEDGER_START:%d %b %Y}) — nothing to close",
            flush=True,
        )
        return None

    indexed = index_ledger(ledger_rows_for(label))
    present = [indexed.get((name, label)) for name in TEAM_MAP]
    if all(
        row is not None and checkbox_value(row["properties"]["Closed"])
        for row in present
    ):
        # Already closed — by an earlier cycle, or by hand in Notion.
        if not dry_run:
            _closed_period = label
        print(f"Close: {label} already closed, nothing to send", flush=True)
        return None

    # Refresh before freezing. Job 5 only ever touches the current period, so
    # at the changeover last period's numbers are as stale as the final cycle
    # that ran before midnight — and closing makes whatever is there permanent.
    entries = sync_period(last_start, last_end, dry_run=dry_run)

    sent = failed = 0
    for entry in entries:
        message = build_person_report(entry)
        if dry_run:
            print(f"\n--- would send to {entry['name']} ({entry['chat_id']}) ---")
            print(message)
            sent += 1
            continue
        try:
            send_telegram(entry["chat_id"], message)
            sent += 1
        except Exception as exc:
            print(f"CLOSE ERROR {entry['name']}: {sanitize(exc)}")
            failed += 1

    owner_message = build_owner_report(entries, label)
    if dry_run:
        print(f"\n--- would send team table to Mustafa ({OWNER_CHAT_ID}) ---")
        print(owner_message)
    else:
        try:
            send_telegram(OWNER_CHAT_ID, owner_message)
        except Exception as exc:
            # Logged rather than retried: the individual reports have gone out
            # and the rows are about to close, so the table is not resent.
            print(f"CLOSE ERROR owner table: {sanitize(exc)}")
            failed += 1

    if dry_run:
        print(
            f"Close (dry run): {label}, {sent} message(s) prepared, "
            f"{len([e for e in entries if not e['closed']])} row(s) would close",
            flush=True,
        )
        return entries

    if not sent:
        # Nothing reached anybody. Leaving the rows open is what makes the
        # next cycle try again instead of burying the period.
        print(
            f"Close: {label} NOT closed — no message reached anyone "
            f"({failed} failed); retrying next cycle",
            flush=True,
        )
        return entries

    closed = 0
    for entry in entries:
        # A row whose sync failed holds numbers this run could not confirm.
        # Closing is permanent, so it is left open: a period that stays
        # editable can be corrected by hand, one frozen shut on wrong numbers
        # cannot.
        if entry["closed"] or entry["action"] == "failed" or not entry["row_id"]:
            continue
        try:
            ledger_write(
                "PATCH",
                f"https://api.notion.com/v1/pages/{entry['row_id']}",
                {"Closed": {"checkbox": True}},
            )
            closed += 1
        except Exception as exc:
            print(f"CLOSE ERROR closing {entry['name']}: {sanitize(exc)}")
            failed += 1

    # Set even when some rows stayed open, so a stuck row cannot make this
    # process re-send the whole team's report every minute. The names below
    # are the ones to look at by hand.
    _closed_period = label
    open_rows = [
        entry["name"]
        for entry in entries
        if not entry["closed"] and entry["action"] == "failed"
    ]
    print(
        f"Close: {label} closed, {sent} sent, {closed} ticked, {failed} failed"
        + (f", left open for {', '.join(open_rows)}" if open_rows else ""),
        flush=True,
    )
    return entries


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


def preview(as_of=None):
    """One no-write pass over jobs 5 and 6, printing what they would do.

    Reads the live databases — the numbers below are real — but takes no write
    path and sends no message. A row that does not exist yet cannot show true
    Remaining and Overtime, since those formulas belong to a row Notion has
    not created; those lines are marked as estimates.
    """
    now = as_of or datetime.now(BAGHDAD)
    start, _ = period_of(now)
    print(
        f"DRY RUN at {now:%Y-%m-%d %H:%M %Z} — current period is "
        f"{period_label(start)}. Nothing is written or sent.\n"
    )
    run_ledger(now=now, dry_run=True)
    print()
    # There is always a previous period, so the close is always "due"; whether
    # it has anything to do (already closed, or a full preview) it says in its
    # own log lines.
    run_period_close(now=now, dry_run=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Notion -> Telegram notifier. With no arguments it runs "
        "the polling loop; --dry-run previews the hours ledger."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview jobs 5 and 6 against live data without writing or sending.",
    )
    parser.add_argument(
        "--as-of",
        metavar="ISO",
        help="Pretend it is this moment, e.g. 2026-09-16T09:30. Dry run only.",
    )
    args = parser.parse_args(argv)
    # Pinned to --dry-run on purpose. The live close is meant to be driven by
    # its condition and nothing else; a switch that could move the clock is a
    # switch that can close a period people are still working.
    if args.as_of and not args.dry_run:
        parser.error("--as-of may only be used with --dry-run")
    if args.as_of:
        try:
            moment = datetime.fromisoformat(args.as_of)
        except ValueError:
            parser.error(f"--as-of is not an ISO 8601 datetime: {args.as_of}")
        args.as_of = (
            moment.replace(tzinfo=BAGHDAD)
            if moment.tzinfo is None
            else moment.astimezone(BAGHDAD)
        )
    return args


def main():
    """Poll once, or keep polling every minute for LOOP_MINUTES minutes."""
    global _last_ledger_run
    args = parse_args()
    if args.dry_run:
        preview(as_of=args.as_of)
        return
    loop_minutes = int(os.environ.get("LOOP_MINUTES", "0"))
    deadline = time.monotonic() + loop_minutes * 60
    while True:
        # The jobs are independent: a Notion or Telegram outage in one must
        # not stop the others from running this cycle.
        #
        # stamp_timestamps is deliberately absent. Its cutoff compares the age
        # of the Notion row, not the age of the work, so rows back-entered for
        # finished jobs read as new and get stamped with the time the bot
        # happened to see them. It wrote ten such dates on 2026-08-11 before
        # this was caught. The stamps are write-once, so every cycle it runs
        # costs another permanent wrong date — it stays off until the cutoff
        # keys off something better than row age. The function is left intact;
        # re-add it here to switch it back on. Until then "Approved At" is
        # filled in by hand, and the bot leaves any value already present
        # alone.
        for job in (run_once, sync_portal, notify_client_decisions):
            try:
                job()
            except Exception as exc:  # e.g. Notion outage — keep the loop alive
                print(f"ERROR {job.__name__} failed: {sanitize(exc)}", flush=True)

        if LEDGER_ENABLED:
            # The ledger is hourly, not per-minute: its numbers move when a
            # deadline or a task's hours change, not continuously, and every
            # pass costs a handful of writes. A fresh process runs it once at
            # startup, then on the hour. The marker only advances on success,
            # so a Notion outage is retried next cycle rather than in an hour.
            now_mono = time.monotonic()
            if _last_ledger_run is None or now_mono - _last_ledger_run >= 3600:
                try:
                    run_ledger()
                    _last_ledger_run = now_mono
                except Exception as exc:
                    print(f"ERROR run_ledger failed: {sanitize(exc)}", flush=True)
            # Checked every cycle. Once the last period is closed it costs one
            # query and stops, so the report lands in the first minute a runner
            # is alive after the period ends — the 16th, or the 1st.
            try:
                run_period_close()
            except Exception as exc:
                print(f"ERROR run_period_close failed: {sanitize(exc)}", flush=True)

        if time.monotonic() + 60 > deadline:
            return
        time.sleep(60)


if __name__ == "__main__":
    main()
