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
   Deadline falls in the current week (Saturday 00:00 -> Friday 23:59 Baghdad,
   cancelled excluded) into the Weekly Hours database, one row per person per
   week. Everyone gets a row even at zero hours, so an empty week is visible
   rather than missing. Running Balance chains off Notion's own Week Balance
   formula; Capped Hours, Week Balance and Remaining are never written.
6. Weekly report: closes the week that just ended — refreshes its numbers,
   Telegrams each person their week and Mustafa a table of all five, then ticks
   Locked. Locked rows are never touched again by either ledger job.

Jobs 5 and 6 only run when LEDGER_ENABLED is set. Preview them against live
data without writing or sending anything:

    python bot.py --dry-run
    python bot.py --dry-run --as-of 2026-09-05T09:30 --force-report

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

# Weekly hours ledger (jobs 5 and 6)
WEEKLY_DATA_SOURCE_ID = "729ea6ac-d3cf-49f7-9cd8-df82751119dc"
# Computed by Notion from Actual Hours and Target. The Capped Hours rule lives
# in the formula and is not readable from here, which is exactly why this code
# reads Week Balance back off the row instead of recomputing it.
LEDGER_FORMULA_PROPS = frozenset({"Capped Hours", "Week Balance", "Remaining"})
# The Status option is lowercase in the database; compared case-folded anyway.
CANCELLED_STATUS = "cancelled"
LEDGER_TARGET_DEFAULT = 48
# Flat monthly figure quoted in the report, not derived from the week target.
MONTH_TARGET = 208
# Saturday, Baghdad. See run_weekly_report for why this is a floor, not a slot.
REPORT_HOUR = 9
# The week this process has already reported on, so the trigger condition is
# only paid for once per week. Deliberately not persistence: a restarted run
# re-checks against Notion, where Locked is the durable record.
_reported_week = None
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
# Jobs 5 and 6: weekly hours ledger, and the Saturday report that closes a week
# --------------------------------------------------------------------------

def week_start_of(moment):
    """The Saturday 00:00 Baghdad on or before `moment`.

    Python's weekday() is Mon=0 .. Sat=5, Sun=6, so (weekday() - 5) % 7 is the
    number of days back to Saturday: Saturday itself gives 0 and Friday gives 6.
    """
    local = moment.astimezone(BAGHDAD)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=(local.weekday() - 5) % 7)


def week_key(week_start):
    """The Week Start value as Notion stores it: a plain YYYY-MM-DD date."""
    return week_start.date().isoformat()


def week_span(week_start):
    """Human range for the message header, e.g. "29 Aug - 04 Sep"."""
    friday = week_start + timedelta(days=6)
    return f"{week_start:%d %b} - {friday:%d %b}"


def parse_deadline(prop):
    """A Deadline as an aware Baghdad datetime, or None if unset/unparseable.

    Deadlines come in both shapes: a bare "2026-09-07" typed in the UI, and a
    full "2026-09-07T14:00:00.000+03:00" when someone sets a time. A bare date
    is read as midnight *in Baghdad*, not UTC — otherwise a Saturday deadline
    would land in the previous week for three hours a day.
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


def target_or_default(prop):
    """A row's Target, falling back to 48 only when it is genuinely unset.

    Tested against None rather than falsiness: a Target of 0 is a real value —
    someone on leave for the week — and reporting it as 48 would tell them they
    are 48 hours short of a target nobody set them.
    """
    value = number_value(prop)
    return LEDGER_TARGET_DEFAULT if value is None else value


def fmt_hours(value):
    """48.0 -> "48", 47.5 -> "47.5". Whole hours read better without a ".0"."""
    if value is None:
        return "—"
    rounded = round(float(value), 2)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:g}"


def fmt_balance(value):
    """Signed, so "+6" and "-6" can never be confused at a glance."""
    if value is None:
        return "—"
    if round(float(value), 2) == 0:
        return "0"
    return f"-{fmt_hours(abs(value))}" if value < 0 else f"+{fmt_hours(value)}"


def ledger_write(method, url, properties, extra=None):
    """Write to the ledger, refusing to touch a formula column.

    Capped Hours, Week Balance and Remaining are computed by Notion. Sending
    any of them is a 400 at best and silent nonsense at worst, so the guard
    raises before the request rather than after — this bot has a history of
    writing values that could not be taken back.
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


def week_actuals(week_start):
    """{notion_user_id: hours} for one week, and the count of unreadable tasks.

    Sums the Hours formula over tasks whose Deadline falls in
    [Saturday 00:00, next Saturday 00:00) Baghdad, excluding cancelled ones.

    The Notion filter is deliberately a day wider on each side and the real
    boundary is applied locally. Notion compares datetimes in UTC, so a filter
    written in Baghdad dates clips tasks near midnight at the edges of the
    week; fetching a little extra and bucketing here is exact.

    Hours land on the *first* assignee, matching how job 1 decides who to
    notify. A task shared between two people counts once, for its owner.
    """
    week_end = week_start + timedelta(days=7)
    tasks = query_data_source(
        DATA_SOURCE_ID,
        {
            "and": [
                {
                    "property": "Deadline",
                    "date": {"on_or_after": week_key(week_start - timedelta(days=1))},
                },
                {
                    "property": "Deadline",
                    "date": {"before": week_key(week_end + timedelta(days=1))},
                },
            ]
        },
    )

    totals = {}
    unreadable = 0
    for page in tasks:
        props = page["properties"]
        if (select_name(props["Status"]) or "").strip().lower() == CANCELLED_STATUS:
            continue
        due = parse_deadline(props["Deadline"])
        if due is None or not (week_start <= due < week_end):
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


def ledger_rows_upto(week_start):
    """Every ledger row on or before `week_start`.

    One query serves all three needs: this week's rows to upsert, earlier weeks
    to chain the running balance from, and the same month's weeks for the
    month-to-date figure. The ledger grows by five rows a week, so this stays
    small for years.
    """
    return query_data_source(
        WEEKLY_DATA_SOURCE_ID,
        {"property": "Week Start", "date": {"on_or_before": week_key(week_start)}},
    )


def index_ledger(rows):
    """{(person, week key): row}, ignoring rows missing either key."""
    indexed = {}
    for row in rows:
        props = row["properties"]
        person = select_name(props["Person"])
        start = date_start(props["Week Start"])
        if person and start:
            indexed[(person, start[:10])] = row
    return indexed


def previous_running(indexed, person, week_start):
    """The running balance carried into `week_start`, or 0 for a first week.

    Takes the most recent earlier week rather than exactly seven days back.
    In normal operation they are the same row, because job 5 writes a row for
    every person every week; they differ only after a gap (the bot was off for
    a fortnight), and there carrying the last known balance forward is right
    where treating the gap as "no history" would silently reset someone to 0.
    """
    key = week_key(week_start)
    earlier = [
        (wk, row)
        for (who, wk), row in indexed.items()
        if who == person and wk < key
    ]
    if not earlier:
        return 0.0
    _, row = max(earlier, key=lambda pair: pair[0])
    return number_value(row["properties"]["Running Balance"]) or 0.0


def sync_week(week_start, dry_run=False):
    """Upsert every person's row for one week and return what each holds.

    Writes Actual Hours, then reads Week Balance back off the row and uses it
    for Running Balance. Week Balance applies the Capped Hours rule, which
    lives in Notion and is not visible to this code — recomputing it here would
    be guessing at the cap, so the formula stays the single source of truth.

    Locked rows are read for their numbers and never written to.
    """
    key = week_key(week_start)
    actuals, unreadable = week_actuals(week_start)
    indexed = index_ledger(ledger_rows_upto(week_start))
    entries = []
    created = updated = locked_skips = failed = 0

    for name, member in TEAM_MAP.items():
        actual = round(actuals.get(member["notion_user_id"], 0.0), 2)
        row = indexed.get((name, key))
        entry = {
            "name": name,
            "chat_id": member["chat_id"],
            "week_start": week_start,
            "span": week_span(week_start),
            "actual": actual,
            "target": float(LEDGER_TARGET_DEFAULT),
            "remaining": None,
            "week_balance": None,
            "running": None,
            "locked": False,
            "estimated": False,
            "action": "unchanged",
            "row_id": row["id"] if row else None,
        }
        try:
            if row is not None and checkbox_value(row["properties"]["Locked"]):
                # A closed week is history. Report what it says, change nothing.
                props = row["properties"]
                entry.update(
                    actual=number_value(props["Actual Hours"]),
                    target=number_value(props["Target"]),
                    remaining=formula_number(props["Remaining"]),
                    week_balance=formula_number(props["Week Balance"]),
                    running=number_value(props["Running Balance"]),
                    locked=True,
                    action="locked",
                )
                print(f"LEDGER SKIP {name} {key}: row is locked")
                locked_skips += 1
                entries.append(entry)
                continue

            stored_actual = (
                number_value(row["properties"]["Actual Hours"]) if row else None
            )

            if dry_run:
                # Nothing is written, so Notion's formulas still describe the
                # old Actual Hours. Quoting them as if they were the new
                # numbers would be a preview of the wrong week, so anything
                # downstream of a value this run did not store is estimated
                # from the plain rule and flagged — the Capped Hours cap lives
                # in the formula and cannot be evaluated for an unstored value.
                if row is not None:
                    entry["target"] = target_or_default(row["properties"]["Target"])
                if row is None:
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
                    entry["week_balance"] = formula_number(
                        row["properties"]["Week Balance"]
                    )
                else:
                    entry["week_balance"] = round(actual - entry["target"], 2)
                    entry["remaining"] = round(max(0.0, entry["target"] - actual), 2)
                    entry["estimated"] = True
                entry["running"] = round(
                    previous_running(indexed, name, week_start)
                    + (entry["week_balance"] or 0.0),
                    2,
                )
                print(
                    f"  {name:<10} actual {fmt_hours(actual):>5} / "
                    f"{fmt_hours(entry['target']):<3} "
                    f"balance {fmt_balance(entry['week_balance']):>5}  "
                    f"running {fmt_balance(entry['running']):>5}  "
                    f"[{entry['action']}"
                    + ("; balance estimated" if entry["estimated"] else "")
                    + "]"
                )
                entries.append(entry)
                continue

            if row is None:
                # An untouched week must still be visible in the ledger, so a
                # person with no tasks gets a row reading 0 rather than no row.
                created_row = ledger_write(
                    "POST",
                    "https://api.notion.com/v1/pages",
                    {
                        "Record": {
                            "title": [
                                {"text": {"content": f"{name} — {week_start:%d %b}"}}
                            ]
                        },
                        "Person": {"select": {"name": name}},
                        "Week Start": {"date": {"start": key}},
                        "Target": {"number": LEDGER_TARGET_DEFAULT},
                        "Actual Hours": {"number": actual},
                    },
                    extra={
                        "parent": {
                            "type": "data_source_id",
                            "data_source_id": WEEKLY_DATA_SOURCE_ID,
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
                # Re-read: the formulas downstream of Actual Hours are the
                # numbers this job depends on, so take them from Notion after
                # the write rather than from the pre-write copy.
                row = fetch_page(row["id"]) or row
                entry["action"] = "updated"
                updated += 1

            props = row["properties"]
            entry["target"] = target_or_default(props["Target"])
            entry["remaining"] = formula_number(props["Remaining"])
            entry["week_balance"] = formula_number(props["Week Balance"])

            balance = entry["week_balance"]
            if balance is None:
                raise RuntimeError(
                    "Week Balance came back empty — cannot chain Running Balance"
                )
            running = round(previous_running(indexed, name, week_start) + balance, 2)
            entry["running"] = running
            if number_value(props["Running Balance"]) != running:
                ledger_write(
                    "PATCH",
                    f"https://api.notion.com/v1/pages/{row['id']}",
                    {"Running Balance": {"number": running}},
                )
            if entry["action"] != "unchanged":
                # Every other job in this file logs the writes it makes; a
                # ledger that moved someone's balance and said nothing would be
                # the one place a wrong number has no trail.
                print(
                    f"LEDGER {entry['action'].upper()} {name} {key}: actual "
                    f"{fmt_hours(actual)}/{fmt_hours(entry['target'])}, "
                    f"balance {fmt_balance(balance)}, "
                    f"running {fmt_balance(running)}"
                )
            entries.append(entry)
        except Exception as exc:  # one bad person must never kill the week
            print(f"LEDGER ERROR {name} {key}: {sanitize(exc)}")
            entry["action"] = "failed"
            entries.append(entry)
            failed += 1

    print(
        f"Ledger{' (dry run)' if dry_run else ''}: week {key}, "
        f"{created} created, {updated} updated, {locked_skips} locked, "
        f"{failed} failed"
        + (f", {unreadable} task(s) with unreadable Hours" if unreadable else ""),
        flush=True,
    )
    return entries


def run_ledger(now=None, dry_run=False):
    """Job 5: keep the current week's ledger rows current."""
    return sync_week(week_start_of(now or datetime.now(BAGHDAD)), dry_run=dry_run)


def month_to_date(entry, indexed):
    """Hours logged in the calendar month containing this week's Saturday.

    A week belongs to the month its Saturday falls in, so every week counts
    once and a week straddling a month boundary is not split. MONTH_TARGET is
    the flat monthly figure and does not vary with how many Saturdays a month
    happens to contain.
    """
    week_start = entry["week_start"]
    prefix = f"{week_start:%Y-%m}"
    total = entry["actual"] or 0.0
    for (who, wk), row in indexed.items():
        if who == entry["name"] and wk.startswith(prefix) and wk != week_key(week_start):
            total += number_value(row["properties"]["Actual Hours"]) or 0.0
    return round(total, 2)


def build_person_report(entry):
    span = html.escape(entry["span"])
    remaining = entry["remaining"]
    lines = [
        f"📊 تقرير الأسبوع | {span}",
        f"⏱️ ساعاتك: {fmt_hours(entry['actual'])} من {fmt_hours(entry['target'])}",
        f"⚠️ ناقصك {fmt_hours(remaining)} ساعة"
        if remaining is not None and remaining > 0
        else "✅ كملت الهدف",
        f"🏦 رصيدك التراكمي: {fmt_balance(entry['running'])}",
        f"📅 الشهر: {fmt_hours(entry['month_actual'])} من {MONTH_TARGET}",
    ]
    return "\n".join(lines)


def build_owner_report(entries, span):
    """Mustafa's whole-team view, worst running balance first.

    Laid out as a monospace block with Latin headers: a column-aligned table
    mixing Arabic headers with Latin names and digits gets reordered by the
    bidi algorithm and the columns stop lining up.
    """
    ordered = sorted(entries, key=lambda e: (e["running"] is None, e["running"] or 0))
    width = max([len("Name")] + [len(e["name"]) for e in ordered])
    header = f"{'Name':<{width}}  {'Hours':>5}  {'Left':>5}  {'Balance':>7}"
    body = [header, "-" * len(header)]
    for entry in ordered:
        body.append(
            f"{entry['name']:<{width}}  {fmt_hours(entry['actual']):>5}  "
            f"{fmt_hours(entry['remaining']):>5}  {fmt_balance(entry['running']):>7}"
        )
    table = html.escape("\n".join(body))
    return f"📊 تقرير الفريق | {html.escape(span)}\n<pre>{table}</pre>"


def run_weekly_report(now=None, dry_run=False, force=False):
    """Job 6: close the week that just ended — report it, then lock it.

    Fires on a condition rather than at an instant: any cycle at or after
    Saturday 09:00 Baghdad whose previous week is not yet fully locked. The
    schedule this bot runs on is throttled hard enough that a given minute is
    not guaranteed to have a live runner, and a strict 09:00 check would drop
    a whole week's report whenever it did not. Locked is both the end state
    the report is supposed to leave behind and the flag that stops it sending
    twice, so a missed Saturday still goes out — late, once, and correct.

    Sends before locking, deliberately. A lock that lands before a failed send
    hides the week forever, while a send that lands before a failed lock costs
    one duplicate next cycle. Job 4 makes the same trade for the same reason.
    """
    global _reported_week
    now = now or datetime.now(BAGHDAD)
    current = week_start_of(now)
    if not force and now < current + timedelta(hours=REPORT_HOUR):
        return None  # still before this Saturday's 09:00
    last_week = current - timedelta(days=7)
    key = week_key(last_week)
    if _reported_week == key:
        return None

    rows = index_ledger(
        query_data_source(
            WEEKLY_DATA_SOURCE_ID,
            {"property": "Week Start", "date": {"equals": key}},
        )
    )
    present = [rows.get((name, key)) for name in TEAM_MAP]
    if all(
        row is not None and checkbox_value(row["properties"]["Locked"])
        for row in present
    ):
        # Already closed — by an earlier cycle, or by hand in Notion.
        if not dry_run:
            _reported_week = key
        print(f"Report: week {key} already locked, nothing to send", flush=True)
        return None

    # Refresh before freezing. Job 5 only ever touches the current week, so by
    # Saturday morning last week's numbers are as stale as the final cycle that
    # ran before midnight — and locking makes whatever is there permanent.
    entries = sync_week(last_week, dry_run=dry_run)
    indexed = index_ledger(ledger_rows_upto(last_week))
    for entry in entries:
        entry["month_actual"] = month_to_date(entry, indexed)

    span = week_span(last_week)
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
            print(f"REPORT ERROR {entry['name']}: {sanitize(exc)}")
            failed += 1

    owner_message = build_owner_report(entries, span)
    if dry_run:
        print(f"\n--- would send team table to Mustafa ({OWNER_CHAT_ID}) ---")
        print(owner_message)
    else:
        try:
            send_telegram(OWNER_CHAT_ID, owner_message)
        except Exception as exc:
            # Logged rather than retried: the individual reports have gone out
            # and the rows are about to lock, so the table will not be resent.
            print(f"REPORT ERROR owner table: {sanitize(exc)}")
            failed += 1

    locked = 0
    if dry_run:
        print(f"\nReport (dry run): week {key}, {sent} message(s) prepared, "
              f"{len([e for e in entries if not e['locked']])} row(s) would lock",
              flush=True)
        return entries
    if not sent:
        # Nothing reached anybody. Leaving the rows unlocked is what makes the
        # next cycle try again instead of burying the week.
        print(
            f"Report: week {key} NOT locked — no message reached anyone "
            f"({failed} failed); retrying next cycle",
            flush=True,
        )
        return entries

    for entry in entries:
        # A row whose sync failed holds numbers this run could not confirm.
        # Locking is permanent, so it is left open: a week that stays editable
        # can be corrected by hand, while a wrong week frozen shut cannot.
        if entry["locked"] or entry["action"] == "failed" or not entry["row_id"]:
            continue
        try:
            ledger_write(
                "PATCH",
                f"https://api.notion.com/v1/pages/{entry['row_id']}",
                {"Locked": {"checkbox": True}},
            )
            locked += 1
        except Exception as exc:
            print(f"REPORT ERROR locking {entry['name']}: {sanitize(exc)}")
            failed += 1

    # Set even when some rows stayed open, so a stuck row cannot make this
    # process re-send the whole team's report every minute for six hours. The
    # names below are the ones to look at by hand.
    _reported_week = key
    open_rows = [
        entry["name"]
        for entry in entries
        if not entry["locked"] and entry["action"] == "failed"
    ]
    print(
        f"Report: week {key} closed, {sent} sent, {locked} locked, {failed} failed"
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


def preview(as_of=None, force_report=False):
    """One no-write pass over jobs 5 and 6, printing what they would do.

    Reads the live database — the numbers below are real — but takes no write
    path and sends no message. Row creation cannot show a true Week Balance,
    since the formula belongs to a row that does not exist yet; those lines are
    marked as estimates.
    """
    now = as_of or datetime.now(BAGHDAD)
    print(
        f"DRY RUN at {now:%Y-%m-%d %H:%M %Z} — current week starts "
        f"{week_key(week_start_of(now))}. Nothing is written or sent.\n"
    )
    run_ledger(now=now, dry_run=True)
    print()
    trigger = week_start_of(now) + timedelta(hours=REPORT_HOUR)
    if now < trigger and not force_report:
        print(
            f"Report: not due — this week's trigger is {trigger:%a %d %b %H:%M}. "
            "Re-run with --force-report to preview it anyway."
        )
        return
    # Anything else the report decides (already locked, or a full preview) it
    # explains in its own log lines.
    run_weekly_report(now=now, dry_run=True, force=force_report)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Notion -> Telegram notifier. With no arguments it runs "
        "the polling loop; --dry-run previews the weekly hours ledger."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview jobs 5 and 6 against live data without writing or sending.",
    )
    parser.add_argument(
        "--as-of",
        metavar="ISO",
        help="Pretend it is this moment, e.g. 2026-09-05T09:30. Dry run only.",
    )
    parser.add_argument(
        "--force-report",
        action="store_true",
        help="Preview the Saturday report even when it is not due. Dry run only.",
    )
    args = parser.parse_args(argv)
    # Both simulation switches are pinned to --dry-run on purpose. The live
    # report is meant to be driven by its condition and nothing else; a flag
    # that could fire it early is a flag that can lock a week that is still
    # being worked.
    if (args.as_of or args.force_report) and not args.dry_run:
        parser.error("--as-of and --force-report may only be used with --dry-run")
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
        preview(as_of=args.as_of, force_report=args.force_report)
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
            # Checked every cycle, but it costs nothing until Saturday 09:00
            # and nothing again once the week is locked, so the report lands in
            # the first minute the runner is alive after it comes due.
            try:
                run_weekly_report()
            except Exception as exc:
                print(f"ERROR run_weekly_report failed: {sanitize(exc)}", flush=True)

        if time.monotonic() + 60 > deadline:
            return
        time.sleep(60)


if __name__ == "__main__":
    main()
