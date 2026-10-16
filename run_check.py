"""
Main entry point, meant to be run periodically (by Windows Task Scheduler
locally, or by the .github/workflows/watch.yml GitHub Actions workflow).
Settings come from config.json locally, or from env vars / GitHub Secrets
in CI (see load_config()) - the script itself is identical either way.

Each run:
  1. Scans every doctor matching config["speciality_filter"].
  2. Rewrites report.html with the latest full picture (busy + possible
     openings) for every doctor.
  3. Compares against the previous run's snapshot.
  4. Emails:
       - immediately, if a possible opening appeared that wasn't there
         before, or a new/changed scan error appeared,
       - otherwise once per calendar day (a quiet "still nothing" heartbeat),
       - otherwise stays silent.

Nothing here books, modifies, or writes anything on the hospital's server;
every API call is a read-only lookup also reachable by clicking through the
public registration page.
"""
import json
import os
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import spz_client as spz
from report_builder import build_report_html
from emailer import send_email

STATE_DIR = BASE_DIR / "state"
SNAPSHOT_PATH = STATE_DIR / "last_snapshot.json"
STATE_PATH = STATE_DIR / "state.json"
LOG_PATH = STATE_DIR / "run_log.txt"
REPORT_PATH = BASE_DIR / "report.html"
CONFIG_PATH = BASE_DIR / "config.json"


def log(line: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {line}\n")


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config() -> dict:
    """
    Loads config.json (if present) and layers environment variable overrides
    on top, so the exact same script runs unchanged both locally (via
    config.json) and in CI (via secrets exposed as env vars, no config.json
    needed at all).
    """
    config = load_json(CONFIG_PATH, {})
    config.setdefault("installation_id", "fc695f76-ee04-48da-9209-7664775d9f7c")
    config.setdefault("speciality_filter", "офтальмолог дитячий")
    config.setdefault("horizon_days", 120)
    config.setdefault("recipient_email", None)
    config.setdefault("smtp", {})
    config["smtp"].setdefault("host", "smtp.gmail.com")
    config["smtp"].setdefault("port", 587)
    config["smtp"].setdefault("use_tls", True)

    top_level_env = {
        "installation_id": "SPZ_INSTALLATION_ID",
        "speciality_filter": "SPZ_SPECIALITY_FILTER",
        "recipient_email": "SPZ_RECIPIENT_EMAIL",
    }
    for key, env in top_level_env.items():
        if os.environ.get(env):
            config[key] = os.environ[env]

    smtp_env = {
        "host": "SMTP_HOST", "port": "SMTP_PORT", "username": "SMTP_USERNAME",
        "password": "SMTP_PASSWORD", "from_addr": "SMTP_FROM",
    }
    for key, env in smtp_env.items():
        if os.environ.get(env):
            config["smtp"][key] = os.environ[env]
    config["smtp"].setdefault("from_addr", config["smtp"].get("username"))

    return config


def scan_to_payload(config: dict) -> dict:
    scans = spz.scan_speciality(
        config["installation_id"],
        config["speciality_filter"],
        horizon_days=config.get("horizon_days", 120),
    )
    doctors = []
    for s in scans:
        doctors.append({
            "name": s.doctor.user_name,
            "user_ref": s.doctor.user_ref,
            "speciality": s.doctor.speciality,
            "error": s.error,
            "last_published_date": s.last_published_date,
            "free_dates": s.free_dates,
            "days": s.days,
        })
    return {
        "scan_time": datetime.now().isoformat(timespec="seconds"),
        "speciality_filter": config["speciality_filter"],
        "horizon_days": config.get("horizon_days", 120),
        "doctors": doctors,
    }


def diff_changes(previous: dict | None, current: dict) -> list[str]:
    prev_by_ref = {d["user_ref"]: d for d in (previous or {}).get("doctors", [])}
    changes = []
    for doc in current["doctors"]:
        prev = prev_by_ref.get(doc["user_ref"])
        prev_free = set(prev["free_dates"]) if prev else set()
        cur_free = set(doc["free_dates"])
        newly_free = sorted(cur_free - prev_free)
        if newly_free:
            changes.append(f"{doc['name']}: possible opening on {', '.join(newly_free)}")

        prev_err = prev.get("error") if prev else None
        if doc["error"] and doc["error"] != prev_err:
            changes.append(f"{doc['name']}: scan error - {doc['error']}")
        elif prev_err and not doc["error"]:
            changes.append(f"{doc['name']}: previous scan error resolved")
    return changes


def build_summary_text(current: dict) -> str:
    lines = [
        f"Speciality: {current['speciality_filter']}",
        f"Scanned: {current['scan_time']} (horizon: {current['horizon_days']} days)",
        "",
    ]
    for doc in current["doctors"]:
        if doc["error"]:
            lines.append(f"- {doc['name']}: SCAN ERROR - {doc['error']}")
            continue
        if doc["free_dates"]:
            lines.append(f"- {doc['name']}: possible opening(s) on {', '.join(doc['free_dates'])}")
        else:
            lines.append(f"- {doc['name']}: fully booked through {doc['last_published_date'] or 'n/a'}")
    return "\n".join(lines)


def main() -> None:
    config = load_config()
    if not config.get("recipient_email"):
        log("FATAL: no recipient_email set (config.json or SPZ_RECIPIENT_EMAIL)")
        return
    if not config["smtp"].get("username") or not config["smtp"].get("password"):
        log("FATAL: SMTP username/password not set (config.json or SMTP_USERNAME/SMTP_PASSWORD)")
        return

    try:
        current = scan_to_payload(config)
        scan_failed = False
    except Exception as e:
        log(f"FATAL scan failure: {e}\n{traceback.format_exc()}")
        current = {
            "scan_time": datetime.now().isoformat(timespec="seconds"),
            "speciality_filter": config.get("speciality_filter", ""),
            "horizon_days": config.get("horizon_days", 120),
            "doctors": [],
        }
        scan_failed = True

    REPORT_PATH.write_text(build_report_html(current), encoding="utf-8")

    previous = load_json(SNAPSHOT_PATH, None)
    changes = diff_changes(previous, current) if not scan_failed else ["Watcher could not reach the schedule API at all"]
    save_json(SNAPSHOT_PATH, current)

    state = load_json(STATE_PATH, {})
    today_str = date.today().isoformat()
    last_email_date = state.get("last_email_date")

    should_send_change = bool(changes)
    should_send_heartbeat = (not should_send_change) and (last_email_date != today_str)

    if should_send_change or should_send_heartbeat:
        subject = (
            "VODKL watcher: possible schedule change"
            if should_send_change
            else "VODKL watcher: daily check, no openings"
        )
        body_parts = []
        if changes:
            body_parts.append("Changes since last check:\n" + "\n".join(f"- {c}" for c in changes))
            body_parts.append("")
        body_parts.append("Current status:\n" + build_summary_text(current))
        body_parts.append("")
        body_parts.append("Full visual report attached (report.html) - open it in any browser.")
        body_parts.append(
            "Note: 'possible opening' is inferred from gaps between busy blocks "
            "the server reports, not an explicit free-slot flag. Verify on the "
            "booking page before assuming it's really bookable."
        )
        text_body = "\n".join(body_parts)

        try:
            send_email(
                config["smtp"], config["recipient_email"], subject, text_body,
                attachments=[("report.html", REPORT_PATH.read_bytes(), "text/html")],
            )
            log(f"Email sent ({'change' if should_send_change else 'heartbeat'}): {subject}")
            state["last_email_date"] = today_str
            save_json(STATE_PATH, state)
        except Exception as e:
            log(f"Email send FAILED: {e}\n{traceback.format_exc()}")
    else:
        log("No change, heartbeat already sent today - staying quiet.")


if __name__ == "__main__":
    main()
