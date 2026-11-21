"""Alert when a TCF Canada (computer-based) slot in Toronto opens before the cutoff date.

Reads GB Language Centre's public schedule feed (the same data the booking page uses),
and sends a push notification via ntfy.sh for any new matching slot.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

API = "https://api.gblc.ca/candidates/test-schedules/?location=Toronto&has_available_seats=true"
TEST = os.environ.get("TEST_TITLE", "TCF Canada")
FORMAT = os.environ.get("TEST_FORMAT", "Computer")
CUTOFF = os.environ.get("CUTOFF_DATE", "2026-12-21")  # alert for dates strictly before this
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
STATE = Path("seen.json")
BOOK_URL = "https://gblc.ca/en/book-now/choose-date"


def fetch():
    req = urllib.request.Request(API, headers={"User-Agent": "Mozilla/5.0 (slot-checker)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def notify(title, body):
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode(),
        headers={"Title": title, "Priority": "urgent", "Tags": "rotating_light", "Click": BOOK_URL},
    )
    urllib.request.urlopen(req, timeout=30)


def main():
    if os.environ.get("TEST_ALERT") == "true":
        notify("Test alert", "Your TCF slot checker can reach your phone. Tap to open the booking page.")
        print("Test alert sent")
        return
    schedules = fetch()
    matches = [
        s for s in schedules
        if s.get("tests", {}).get("title") == TEST
        and (s.get("formats") or {}).get("title") == FORMAT
        and s.get("has_available_seats")
        and s.get("start_at_date", "9999") < CUTOFF
    ]
    earliest = min((s["start_at_date"] for s in schedules
                    if s.get("tests", {}).get("title") == TEST
                    and (s.get("formats") or {}).get("title") == FORMAT), default="none")
    print(f"Checked {len(schedules)} schedules. Earliest {TEST} ({FORMAT}): {earliest}. Matches before {CUTOFF}: {len(matches)}")

    seen = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    new = [s for s in matches if str(s["id"]) not in seen]
    if new:
        lines = [f"{s['start_at_date']} - {s['seats'] - s['taken_seats']} seat(s), "
                 f"register by {s.get('registration_deadline')}" for s in sorted(new, key=lambda s: s["start_at_date"])]
        notify(f"{TEST} slot before {CUTOFF}!", "\n".join(lines) + "\nTap to book.")
        print("Notified:\n" + "\n".join(lines))
    # Remember current matches only, so a slot that disappears and reopens alerts again.
    STATE.write_text(json.dumps(sorted(str(s["id"]) for s in matches)))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # surface failures in the Actions log without spamming alerts
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
