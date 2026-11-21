"""Alert when a TCF Canada (computer-based) slot in Toronto opens before the cutoff date.

Reads GB Language Centre's public schedule feed (the same data the booking page uses)
and sends push notifications via ntfy.sh.

Safety nets:
- Retries network calls before giving up.
- Re-sends the alert every REMIND_MINUTES while a matching slot stays open,
  so one missed notification does not mean a missed slot.
- Alerts you if the checker keeps failing (site down, data format changed, etc.),
  and again when it recovers.
- Daily "still watching" heartbeat, so silence never goes unnoticed.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.gblc.ca/candidates/test-schedules/?location=Toronto&has_available_seats=true"
TEST = os.environ.get("TEST_TITLE", "TCF Canada")
FORMAT = os.environ.get("TEST_FORMAT", "Computer")
CUTOFF = os.environ.get("CUTOFF_DATE", "2026-12-21")  # alert for exam dates strictly before this
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
REMIND_MINUTES = int(os.environ.get("REMIND_MINUTES", "30"))
FAIL_ALERT_AFTER = 3        # consecutive failed runs (~15 min) before alerting
FAIL_REALERT_EVERY = 36     # then remind every ~3 hours while still failing
STATE = Path("state.json")
BOOK_URL = "https://gblc.ca/en/book-now/choose-date"


def now():
    return datetime.now(timezone.utc)


def with_retries(fn, attempts=3, delay=10):
    for i in range(attempts):
        try:
            return fn()
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))


def fetch():
    def go():
        req = urllib.request.Request(API, headers={"User-Agent": "Mozilla/5.0 (slot-checker)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    data = with_retries(go)
    if isinstance(data, dict):  # tolerate a future switch to a paginated {"results": [...]} shape
        data = data.get("results", data.get("data"))
    if not isinstance(data, list):
        raise ValueError("Unexpected data format from the booking site")
    return data


def notify(title, body, priority="urgent", tags="rotating_light"):
    def go():
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode(),
            headers={"Title": title, "Priority": priority, "Tags": tags, "Click": BOOK_URL},
        )
        urllib.request.urlopen(req, timeout=30).read()
    with_retries(go)


def is_target(s):
    title = ((s.get("tests") or {}).get("title") or "").strip().lower()
    fmt = ((s.get("formats") or {}).get("title") or "").strip().lower()
    return title == TEST.lower() and fmt == FORMAT.lower()


def load_state():
    try:
        st = json.loads(STATE.read_text())
        if isinstance(st, dict):
            st.setdefault("notified", {})
            st.setdefault("failures", 0)
            return st
    except Exception:
        pass
    return {"notified": {}, "failures": 0}


def save_state(st):
    STATE.write_text(json.dumps(st, indent=1, sort_keys=True) + "\n")


def check(st):
    schedules = fetch()
    targets = [s for s in schedules if is_target(s)]
    if not targets:
        # Dozens of TCF Canada sessions are normally listed; zero means something changed.
        raise ValueError(f"No '{TEST}' ({FORMAT}) sessions found at all - the site may have changed")

    matches = [s for s in targets
               if s.get("has_available_seats") and str(s.get("start_at_date", "9999")) < CUTOFF]
    earliest = min(str(s.get("start_at_date")) for s in targets)
    print(f"Checked {len(schedules)} sessions; {len(targets)} {TEST} ({FORMAT}). "
          f"Earliest: {earliest}. Open before {CUTOFF}: {len(matches)}")

    t = now()
    due = []
    for s in matches:
        last = st["notified"].get(str(s["id"]))
        if last is None or (t - datetime.fromisoformat(last)).total_seconds() >= REMIND_MINUTES * 60:
            due.append(s)
    if due:
        lines = [f"{s['start_at_date']}: {max(0, s.get('seats', 0) - s.get('taken_seats', 0))} seat(s), "
                 f"register by {s.get('registration_deadline')}"
                 for s in sorted(matches, key=lambda s: s["start_at_date"])]
        is_new = any(str(s["id"]) not in st["notified"] for s in due)
        title = f"{TEST} slot before {CUTOFF}!" if is_new else f"Still open: {TEST} slot before {CUTOFF}"
        notify(title, "\n".join(lines) + "\nTap to book now.")
        print("Notified:\n" + "\n".join(lines))
        for s in due:
            st["notified"][str(s["id"])] = t.isoformat()
    # Forget slots that are no longer open, so a reopened slot alerts again immediately.
    open_ids = {str(s["id"]) for s in matches}
    st["notified"] = {k: v for k, v in st["notified"].items() if k in open_ids}
    return earliest


def main():
    if not NTFY_TOPIC:
        print("Error: NTFY_TOPIC secret is missing", file=sys.stderr)
        sys.exit(1)
    mode = os.environ.get("MODE", "check")

    if mode == "test":
        notify("Test alert", "Your TCF slot checker can reach your phone. Tap to open the booking page.")
        print("Test alert sent")
        return

    st = load_state()
    try:
        earliest = check(st)
    except Exception as e:
        st["failures"] += 1
        print(f"Error (failure #{st['failures']} in a row): {e}", file=sys.stderr)
        n = st["failures"]
        if n == FAIL_ALERT_AFTER or (n > FAIL_ALERT_AFTER and (n - FAIL_ALERT_AFTER) % FAIL_REALERT_EVERY == 0):
            try:
                notify("Slot checker is failing", f"{n} checks in a row failed. Last error: {e}\n"
                       "Check the booking page manually until it recovers.",
                       priority="high", tags="warning")
            except Exception as ne:
                print(f"Could not send failure alert: {ne}", file=sys.stderr)
        save_state(st)
        sys.exit(1)

    if st["failures"] >= FAIL_ALERT_AFTER:
        try:
            notify("Slot checker recovered", "Checks are working again.", priority="default", tags="white_check_mark")
        except Exception as ne:
            print(f"Could not send recovery alert: {ne}", file=sys.stderr)
    st["failures"] = 0
    save_state(st)

    if mode == "heartbeat":
        notify("Slot checker: still watching",
               f"Working normally. Earliest {TEST} ({FORMAT}) date right now: {earliest}.",
               priority="min", tags="eyes")


if __name__ == "__main__":
    main()
