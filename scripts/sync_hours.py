#!/usr/bin/env python3
"""
Sync "Actual Hours" on the iPlugn Hours Ledger from the iPlugn Tasks database.

For every OPEN ledger row (Closed unticked) it:
  1. reads every task whose `Period` formula matches that row's Period text,
  2. sums the task's `Hours` formula result per person,
  3. PATCHes the row's `Actual Hours` if the number changed.

The rate card is NOT duplicated here. Hours come from the `Hours` formula in
Notion, so the formula stays the single source of truth and `Custom Hours`
keeps working.

Stdlib only — no pip install.

Env:
  NOTION_TOKEN            (required) internal integration token
  DRY_RUN                 "true" to log without writing        (default false)
  SPLIT_MULTI_ASSIGNEE    "true" splits hours across assignees (default true)

Usage:
  python sync_hours.py
  python sync_hours.py --list-users     # print workspace user IDs + names
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

# ---------------------------------------------------------------- config ---

API = "https://api.notion.com/v1"

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_VERSION = os.environ.get("NOTION_VERSION", "2025-09-03")
LEGACY_VERSION = "2022-06-28"

# Data source IDs (API version 2025-09-03)
TASKS_DS = "e89111c3-6385-43a4-b3d3-0d722bc29981"
LEDGER_DS = "729ea6ac-d3cf-49f7-9cd8-df82751119dc"

# Database IDs (fallback for the older API version)
TASKS_DB = "e94cbb20-1fd6-45a5-aa9a-1eaa51ac683f"
LEDGER_DB = "a9cce9bb-aecb-41ef-baed-6dd5be4c9022"

# Task statuses that earn no hours.
EXCLUDED_STATUSES = ["cancelled"]

# Notion user ID -> Hours Ledger "Person" option.
# Only needed when a person's Notion display name does not start with the
# ledger name (run `--list-users` to get the IDs).
PERSON_OVERRIDES = {
    # "1dcd872b-594c-8179-bacd-0002616ae7f1": "Zain",
}

DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() == "true"
SPLIT_MULTI_ASSIGNEE = os.environ.get(
    "SPLIT_MULTI_ASSIGNEE", "true").strip().lower() != "false"

_legacy = False


# ------------------------------------------------------------ http layer ---

class APIError(Exception):
    def __init__(self, code, body):
        super().__init__(f"{code}: {body}")
        self.code = code
        self.body = body


def _headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": LEGACY_VERSION if _legacy else NOTION_VERSION,
        "Content-Type": "application/json",
    }


def api(method, path, payload=None):
    """One Notion call, with backoff on rate limits and 5xx."""
    body = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(5):
        req = urllib.request.Request(
            API + path, data=body, method=method, headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            text = err.read().decode("utf-8", "replace")
            if err.code in (429, 500, 502, 503, 504) and attempt < 4:
                wait = float(err.headers.get("Retry-After") or 2 ** attempt)
                time.sleep(wait)
                continue
            raise APIError(err.code, text)
        except urllib.error.URLError as err:
            if attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise APIError(0, str(err))
    raise APIError(0, "retries exhausted")


def query_all(ds_id, db_id, filt=None):
    """Every row of a data source, following pagination."""
    global _legacy
    rows, cursor = [], None
    while True:
        payload = {"page_size": 100}
        if filt:
            payload["filter"] = filt
        if cursor:
            payload["start_cursor"] = cursor
        try:
            path = (f"/databases/{db_id}/query" if _legacy
                    else f"/data_sources/{ds_id}/query")
            data = api("POST", path, payload)
        except APIError as err:
            # Workspace still on the pre-data-source API — switch once.
            if not _legacy and err.code in (400, 404):
                _legacy = True
                print("note: falling back to the 2022-06-28 database API")
                continue
            raise
        rows.extend(data["results"])
        if not data.get("has_more"):
            return rows
        cursor = data["next_cursor"]


def all_users():
    """Notion user ID -> display name, humans only."""
    users, cursor = {}, None
    while True:
        path = "/users?page_size=100"
        if cursor:
            path += "&start_cursor=" + urllib.parse.quote(cursor)
        data = api("GET", path)
        for user in data["results"]:
            if user.get("type") == "bot":
                continue
            users[user["id"]] = (user.get("name") or "").strip()
        if not data.get("has_more"):
            return users
        cursor = data["next_cursor"]


# --------------------------------------------------------- property reads --

def prop(page, name):
    return page.get("properties", {}).get(name, {}) or {}


def read_text(page, name):
    return "".join(t.get("plain_text", "")
                   for t in prop(page, name).get("rich_text", []) or []).strip()


def read_select(page, name):
    return ((prop(page, name).get("select") or {}).get("name") or "").strip()


def read_formula_number(page, name):
    return (prop(page, name).get("formula") or {}).get("number")


def read_formula_string(page, name):
    return ((prop(page, name).get("formula") or {}).get("string") or "").strip()


def resolve_person(user_id, users, ledger_people):
    """Map a Notion user to a ledger Person option by first name."""
    if user_id in PERSON_OVERRIDES:
        return PERSON_OVERRIDES[user_id]
    name = users.get(user_id, "")
    if not name:
        return None
    first = name.split()[0].lower()
    for person in ledger_people:
        if person.lower() == first:
            return person
    return None


# -------------------------------------------------------------- the sync ---

def main():
    if not NOTION_TOKEN:
        sys.exit("NOTION_TOKEN is not set")

    if "--list-users" in sys.argv:
        for uid, name in sorted(all_users().items(), key=lambda kv: kv[1]):
            print(f"{uid}  {name}")
        return

    # 1. Open ledger rows.
    ledger = query_all(LEDGER_DS, LEDGER_DB,
                       {"property": "Closed", "checkbox": {"equals": False}})
    open_rows = []
    for row in ledger:
        person = read_select(row, "Person")
        period = read_text(row, "Period")
        if person and period:
            open_rows.append({
                "id": row["id"],
                "person": person,
                "period": period,
                "actual": prop(row, "Actual Hours").get("number"),
            })

    if not open_rows:
        print("no open ledger rows — nothing to sync")
        return

    periods = sorted({r["period"] for r in open_rows})
    ledger_people = sorted({r["person"] for r in open_rows})
    print(f"open periods: {', '.join(periods)}")

    # 2. Tasks landing in those periods.
    task_filter = {"and": [
        {"or": [{"property": "Period", "formula": {"string": {"equals": p}}}
                for p in periods]},
        *[{"property": "Status", "select": {"does_not_equal": s}}
          for s in EXCLUDED_STATUSES],
    ]}
    tasks = query_all(TASKS_DS, TASKS_DB, task_filter)
    print(f"tasks in range: {len(tasks)}")

    # 3. Sum per (person, period).
    users = all_users()
    totals = defaultdict(float)
    unassigned = 0.0
    unmapped = defaultdict(float)

    for task in tasks:
        hours = read_formula_number(task, "Hours") or 0.0
        if not hours:
            continue
        period = read_formula_string(task, "Period")
        if period not in periods:
            continue
        people = [p["id"] for p in prop(task, "Assignee").get("people", [])]
        if not people:
            unassigned += hours
            continue
        share = hours / len(people) if SPLIT_MULTI_ASSIGNEE else hours
        for uid in people:
            person = resolve_person(uid, users, ledger_people)
            if person is None:
                unmapped[users.get(uid) or uid] += share
                continue
            totals[(person, period)] += share

    # 4. Write back what changed.
    changed = 0
    for row in open_rows:
        new = round(totals.get((row["person"], row["period"]), 0.0), 2)
        old = row["actual"]
        if old is not None and abs(old - new) < 0.001:
            print(f"  = {row['person']:<10} {row['period']:<12} {new:>7.2f}")
            continue
        arrow = "-" if old is None else f"{old:g}"
        print(f"  ~ {row['person']:<10} {row['period']:<12} "
              f"{arrow} -> {new:.2f}")
        changed += 1
        if not DRY_RUN:
            api("PATCH", f"/pages/{row['id']}",
                {"properties": {"Actual Hours": {"number": new}}})

    if unassigned:
        print(f"warning: {unassigned:.2f}h on tasks with no Assignee")
    for name, hours in unmapped.items():
        print(f"warning: {hours:.2f}h for '{name}' — no matching ledger Person; "
              f"add them to PERSON_OVERRIDES")

    print(f"{'would update' if DRY_RUN else 'updated'} {changed} row(s)")


if __name__ == "__main__":
    try:
        main()
    except APIError as err:
        sys.exit(f"Notion API error {err.code}: {err.body}")
