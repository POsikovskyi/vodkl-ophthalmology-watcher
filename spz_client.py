"""
Client for the public, unauthenticated "SPZ" booking API used by
https://portal-doctor.eleks.com/web/vodkl/registration.html

The whole UI talks to one endpoint:
    POST /api/runanonymous
    {"name": "<commandName>", "params": [{"name": X, "value": Y}, ...], "installationId": "<guid>"}

This module re-implements, read-only, the exact sequence of calls the Angular
app makes when a visitor picks "Запис до лікаря" -> a doctor -> a service ->
browses the calendar. Nothing here books, cancels, or modifies anything on
the server; every call is a plain GET-equivalent lookup also reachable by
clicking through the public page.
"""
from __future__ import annotations

import json
import re
import urllib.request
import urllib.error
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

API_URL = "https://portal-doctor.eleks.com/api/runanonymous"
REFERER = "https://portal-doctor.eleks.com/web/vodkl/"

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

BUSY_STATUS = "Зайнято"
# Any gap of at least this many minutes between/around busy blocks on a
# published working day is treated as a possible free slot. This is a
# heuristic: the server only ever sends back the *busy* blocks for a day,
# never explicit "free" markers, so a day's true free capacity can only be
# inferred from gaps in what it already works, not stated outright.
MIN_GAP_MINUTES = 15


class SpzApiError(RuntimeError):
    pass


def _fmt_date(d: date) -> str:
    return f"{_MONTHS[d.month - 1]} {d.day} {d.year} 12:00AM"


def api_call(name: str, params: dict, installation_id: str, timeout: int = 20) -> dict:
    payload = {
        "name": name,
        "params": [{"name": k, "value": v} for k, v in params.items()],
        "installationId": installation_id,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://portal-doctor.eleks.com",
            "Referer": REFERER,
            "User-Agent": "Mozilla/5.0 (schedule-watcher script)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise SpzApiError(f"HTTP {e.code} calling {name}: {e.read()[:300]!r}") from e
    except urllib.error.URLError as e:
        raise SpzApiError(f"Network error calling {name}: {e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise SpzApiError(f"Bad JSON from {name}: {raw[:300]!r}") from e


_USERREF_RE = re.compile(r'<param name="UserRef">([^<]*)</param>')


def _extract_user_ref(result_url: str) -> Optional[str]:
    m = _USERREF_RE.search(result_url or "")
    return m.group(1) if m else None


@dataclass
class Doctor:
    user_name: str
    user_ref: str
    speciality: str
    company_name: str
    first_free_time_hint: str


def get_all_doctors(installation_id: str) -> list[Doctor]:
    """Returns every doctor listed under 'Запис до лікаря' (RecordMode=Physician)."""
    data = api_call(
        "spzIBWebAnonymousUserList",
        {"CompanyGUID": "null", "RecordMode": "Physician", "Step": "1"},
        installation_id,
    )
    grid = data.get("userGrid", [])
    doctors = []
    for row in grid:
        ref = _extract_user_ref(row.get("resultUrl", ""))
        if not ref:
            continue
        doctors.append(Doctor(
            user_name=(row.get("userName") or "").strip(),
            user_ref=ref,
            speciality=(row.get("userSpecialityName") or "").strip(),
            company_name=(row.get("companyName") or "").strip(),
            first_free_time_hint=row.get("firstFreeTime") or "",
        ))
    return doctors


def get_doctors_by_speciality(installation_id: str, speciality_substring: str) -> list[Doctor]:
    needle = speciality_substring.strip().lower()
    return [d for d in get_all_doctors(installation_id) if needle in d.speciality.lower()]


def _find_grid_key(data: dict, prefix: str) -> Optional[str]:
    for k in data.keys():
        if k.startswith(prefix) and "setting" not in k.lower():
            return k
    return None


def find_leaf_service_id(installation_id: str, user_ref: str, start: date) -> Optional[str]:
    """
    Walks the Service selection tree (Step 2: "Вибір послуги") for a doctor
    and returns the first leaf ServiceID whose action is the real calendar
    step (spzIBWebAnonymousTimeTablePatientWizard). Doctors in this system
    so far only expose a single leaf service, so "first" is also "only".
    """
    data = api_call(
        "spzIBWebAnonymousUserServiceList",
        {
            "RecordMode": "Physician",
            "CompanyGUID": "null",
            "UserRef": user_ref,
            "Date": _fmt_date(start),
            "Step": "2",
        },
        installation_id,
    )
    for _ in range(6):  # safety cap on tree depth
        key = _find_grid_key(data, "serviceGrid")
        if not key:
            return None
        rows = data.get(key) or []
        if not rows:
            return None
        first = rows[0]
        actions = first.get("action") or []
        if not actions:
            return None
        act = actions[0]
        if act.get("name") == "spzIBWebAnonymousTimeTablePatientWizard":
            return act.get("params", {}).get("serviceID")
        if act.get("name") == "spzIBWebAnonymousUserServiceList":
            data = api_call("spzIBWebAnonymousUserServiceList", act.get("params", {}), installation_id)
            continue
        return None
    return None


@dataclass
class DaySlots:
    busy: list[tuple[str, str]] = field(default_factory=list)  # (HH:MM, HH:MM)


def walk_schedule(installation_id: str, user_ref: str, service_id: str,
                   start: date, horizon_days: int = 120,
                   max_iterations: int = 40) -> dict[str, DaySlots]:
    """
    Walks forward through the doctor's published calendar starting at `start`,
    stopping once the server reports no more published days (empty
    plannerColumns) or the horizon/iteration cap is hit. Returns a map of
    ISO date -> DaySlots (the busy blocks the server reports for that day;
    an ISO date with an empty busy list means a published working day with
    zero reported busy blocks, i.e. apparently wide open).
    """
    result: dict[str, DaySlots] = {}
    cursor = start
    deadline = start + timedelta(days=horizon_days)
    seen_dates: set[str] = set()

    for _ in range(max_iterations):
        if cursor > deadline:
            break
        data = api_call(
            "spzIBWebAnonymousTimeTablePatientWizard",
            {
                "CompanyGUID": "null",
                "UserRef": user_ref,
                "ServiceID": service_id,
                "Date": _fmt_date(cursor),
                "ViewMode": "User",
                "ShowUserList": "0",
                "UserSpecialityCode": "null",
                "Step": "3",
                "RecordMode": "Physician",
            },
            installation_id,
        )
        col_key = _find_grid_key(data, "plannerColumns")
        columns = data.get(col_key) or [] if col_key else []
        if not columns:
            break

        col_dates = []
        for col in columns:
            code = col.get("code", "")
            if len(code) == 8 and code.isdigit():
                iso = f"{code[0:4]}-{code[4:6]}-{code[6:8]}"
                col_dates.append(iso)
                result.setdefault(iso, DaySlots())

        if not col_dates or all(d in seen_dates for d in col_dates):
            break  # server stuck repeating the same page; stop to avoid a loop
        seen_dates.update(col_dates)

        planner = data.get("planner") or []
        code_to_iso = {
            col.get("code", ""): f"{col.get('code','')[0:4]}-{col.get('code','')[4:6]}-{col.get('code','')[6:8]}"
            for col in columns if len(col.get("code", "")) == 8
        }
        for entry in planner:
            iso = code_to_iso.get(entry.get("columnKey", ""))
            if not iso:
                continue
            start_t = (entry.get("startTime") or "")[11:16]
            end_t = (entry.get("endTime") or "")[11:16]
            status = entry.get("text1", "")
            if status == BUSY_STATUS and start_t and end_t:
                result[iso].busy.append((start_t, end_t))

        last_iso = max(col_dates)
        next_cursor = date.fromisoformat(last_iso) + timedelta(days=1)
        if next_cursor <= cursor:
            break
        cursor = next_cursor

    for day in result.values():
        day.busy.sort()

    return result


def classify_day(day: DaySlots) -> dict:
    """
    Returns {"status": "OPEN_NO_BOOKINGS" | "OPEN_GAP" | "FULL", "gaps": [...]}.
    OPEN_* means a plausible free slot per the gap heuristic above.
    """
    if not day.busy:
        return {"status": "OPEN_NO_BOOKINGS", "gaps": []}

    def to_minutes(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    gaps = []
    for (_, prev_end), (next_start, _) in zip(day.busy, day.busy[1:]):
        gap = to_minutes(next_start) - to_minutes(prev_end)
        if gap >= MIN_GAP_MINUTES:
            gaps.append((prev_end, next_start, gap))

    if gaps:
        return {"status": "OPEN_GAP", "gaps": gaps}
    return {"status": "FULL", "gaps": []}


@dataclass
class DoctorScan:
    doctor: Doctor
    days: dict[str, dict]  # iso date -> classify_day() result
    error: Optional[str] = None

    @property
    def free_dates(self) -> list[str]:
        return sorted(d for d, info in self.days.items() if info["status"] != "FULL")

    @property
    def last_published_date(self) -> Optional[str]:
        return max(self.days) if self.days else None


def scan_speciality(installation_id: str, speciality_substring: str,
                     horizon_days: int = 120) -> list[DoctorScan]:
    today = date.today()
    doctors = get_doctors_by_speciality(installation_id, speciality_substring)
    scans = []
    for doc in doctors:
        try:
            service_id = find_leaf_service_id(installation_id, doc.user_ref, today)
            if not service_id:
                scans.append(DoctorScan(doctor=doc, days={}, error="no bookable service found"))
                continue
            schedule = walk_schedule(installation_id, doc.user_ref, service_id, today, horizon_days)
            days = {iso: classify_day(slots) for iso, slots in schedule.items()}
            scans.append(DoctorScan(doctor=doc, days=days))
        except SpzApiError as e:
            scans.append(DoctorScan(doctor=doc, days={}, error=str(e)))
    return scans


def new_installation_id() -> str:
    return str(uuid.uuid4())
