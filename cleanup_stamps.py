"""One-off: clear "Delivered At" / "Approved At" stamps that should never
have been written.

Nothing is written unless you pass --apply. By default it queries, prints
every task it would touch and exactly which fields it would clear, and exits.
Read that list before you run it for real.

Two ways to choose what gets cleared:

  --stamped-at VALUE
      Every task whose Delivered At or Approved At equals VALUE. One stamping
      cycle computes a single timestamp and writes that identical value to
      every task it touches, so one bad cycle's damage shares one exact
      second. This is the precise undo, and the one to reach for.

      Only the field that matches VALUE is cleared. A task carrying a good
      Delivered At from June and a bad Approved At from the bad cycle keeps
      the June date.

  --before-epoch
      Every task whose Notion row was created before STAMP_EPOCH and has
      either stamp set. This is the "no historical row should carry a stamp"
      sweep. Note it keys off row creation time, so a row typed in last week
      about work finished in June does NOT match — for that case use
      --stamped-at.

Requires NOTION_TOKEN. Unlike bot.py it needs no Telegram secrets, since it
never sends anything.

    python cleanup_stamps.py --stamped-at 2026-08-11T02:48:13+03:00
    python cleanup_stamps.py --stamped-at 2026-08-11T02:48:13+03:00 --apply
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

try:
    from zoneinfo import ZoneInfo

    BAGHDAD = ZoneInfo("Asia/Baghdad")
except Exception:
    BAGHDAD = timezone(timedelta(hours=3), "Asia/Baghdad")

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATA_SOURCE_ID = "e89111c3-6385-43a4-b3d3-0d722bc29981"
NOTION_VERSION = "2025-09-03"

# Kept in step with bot.py deliberately rather than imported: importing bot.py
# demands TELEGRAM_TOKEN and TEAM_MAP and starts a module that talks to
# Telegram. A cleanup tool should need one secret, not three.
STAMP_EPOCH = datetime(2026, 8, 5, tzinfo=BAGHDAD)
STAMP_FIELDS = ("Delivered At", "Approved At")

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


def query_all(query_filter):
    url = f"https://api.notion.com/v1/data_sources/{DATA_SOURCE_ID}/query"
    body = {"filter": query_filter}
    results = []
    while True:
        resp = requests.post(url, headers=HEADERS, json=body, timeout=30)
        if resp.status_code >= 400:
            raise SystemExit(f"Notion {resp.status_code}: {resp.text[:600]}")
        data = resp.json()
        results.extend(data["results"])
        if not data.get("has_more"):
            return results
        body["start_cursor"] = data["next_cursor"]


def title_text(prop):
    return "".join(part["plain_text"] for part in prop.get("title", []))


def date_start(prop):
    date = prop.get("date")
    return date.get("start") if date else None


def parse(value):
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_stamped_tasks():
    """Every task carrying at least one stamp. One level of nesting, well
    inside what Notion accepts."""
    return query_all(
        {
            "or": [
                {"property": field, "date": {"is_not_empty": True}}
                for field in STAMP_FIELDS
            ]
        }
    )


def fields_to_clear(page, args):
    """Which stamps on this page match the selector. Empty means leave it."""
    props = page["properties"]
    if args.stamped_at:
        target = parse(args.stamped_at)
        matches = []
        for field in STAMP_FIELDS:
            current = parse(date_start(props[field]))
            # Compared as instants, not strings: Notion may hand back the same
            # moment written a different way than it was sent.
            if current is not None and target is not None and current == target:
                matches.append(field)
        return matches

    created = parse(page.get("created_time"))
    if created is None or created >= STAMP_EPOCH:
        return []
    return [field for field in STAMP_FIELDS if date_start(props[field])]


def clear(page_id, fields):
    resp = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=HEADERS,
        json={"properties": {field: {"date": None} for field in fields}},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Notion {resp.status_code}: {resp.text[:400]}")


def main():
    parser = argparse.ArgumentParser(
        description="Clear Delivered At / Approved At stamps written in error."
    )
    picker = parser.add_mutually_exclusive_group(required=True)
    picker.add_argument(
        "--stamped-at",
        metavar="TIMESTAMP",
        help="clear stamps equal to this exact instant, e.g. "
        "2026-08-11T02:48:13+03:00 (undoes one bad cycle)",
    )
    picker.add_argument(
        "--before-epoch",
        action="store_true",
        help=f"clear stamps on rows created before {STAMP_EPOCH.isoformat()}",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write. Without it, nothing is modified.",
    )
    args = parser.parse_args()

    if args.stamped_at and parse(args.stamped_at) is None:
        raise SystemExit(
            f"--stamped-at {args.stamped_at!r} is not an ISO timestamp. "
            "Copy it from the STAMP log line, e.g. 2026-08-11T02:48:13+03:00"
        )

    tasks = fetch_stamped_tasks()
    targets = [(page, fields_to_clear(page, args)) for page in tasks]
    targets = [(page, fields) for page, fields in targets if fields]

    selector = (
        f"stamps equal to {args.stamped_at}"
        if args.stamped_at
        else f"rows created before {STAMP_EPOCH.isoformat()}"
    )
    print(f"Selector: {selector}")
    print(f"{len(tasks)} tasks carry a stamp; {len(targets)} match.\n")

    if not targets:
        print("Nothing matches — nothing to clear.")
        if args.before_epoch:
            print(
                "\nIf you expected matches here, the rows are probably newer "
                "than the cutoff even though the work is old. Check the "
                "'row created' value in the STAMP log lines and use "
                "--stamped-at instead."
            )
        else:
            # Running this blind from the Actions tab, an empty result is
            # indistinguishable from a mistyped timestamp. Show what is
            # actually in the database so the right value is one glance away.
            print(
                f"\nNo stamp equals {args.stamped_at}. The values currently "
                "in the database are:"
            )
            seen = {}
            for page in tasks:
                for field in STAMP_FIELDS:
                    value = date_start(page["properties"][field])
                    if value:
                        seen[value] = seen.get(value, 0) + 1
            for value, count in sorted(seen.items()):
                print(f"  {value}  ({count} stamp(s))")
            print(
                "\nA bad cycle shows up as one value repeated across many "
                "tasks. Copy it exactly."
            )
        return

    for page, fields in targets:
        label = title_text(page["properties"]["Task Name"]) or page["id"]
        print(f"  {label}")
        print(f"      row created: {page.get('created_time')}")
        for field in fields:
            print(f"      clear {field}: {date_start(page['properties'][field])}")

    if not args.apply:
        print(
            f"\nDry run — nothing was changed. Re-run with --apply to clear "
            f"{sum(len(f) for _, f in targets)} stamp(s) on "
            f"{len(targets)} task(s)."
        )
        return

    print()
    cleared = failed = 0
    for page, fields in targets:
        label = title_text(page["properties"]["Task Name"]) or page["id"]
        try:
            clear(page["id"], fields)
            print(f"CLEARED {', '.join(fields)} on '{label}'")
            cleared += 1
        except Exception as exc:  # one bad row must not strand the rest
            print(f"ERROR '{label}': {exc}")
            failed += 1

    print(f"\nDone: {cleared} task(s) cleared, {failed} failed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
