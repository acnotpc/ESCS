"""Date-scoped provisional lists; explicit evidence, existing audience, one writer."""
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from team_sync import LONDON, aware, rest_summary

START = "[ESCS PROVISIONAL TEAMS]"
END = "[/ESCS PROVISIONAL TEAMS]"


def service_title(day):
    return date.fromisoformat(day).isoformat() + " - Teams List"


def title_date(title):
    # Existing posts have control-room author initials (e.g. '- MB').
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}) - Teams List(?: - [A-Z]{1,5})?", title)
    if not match:
        return None
    try:
        return date.fromisoformat(match[1]).isoformat()
    except ValueError:
        return None


def post_date(post):
    day = title_date(post.get("TITLE", ""))
    if day:
        return day
    # Bitrix micro posts can generate TITLE from the full message; the visible
    # first line is the dated heading. Keep this fallback limited to micro posts.
    if post.get("MICRO") != "Y":
        return None
    first = post.get("DETAIL_TEXT", "").splitlines()[0].strip() if post.get("DETAIL_TEXT") else ""
    first = re.sub(r"\[/?(?:b|i|u)\]", "", first, flags=re.I)
    if not post.get("TITLE", "").startswith(first):
        return None
    return title_date(first)


class Interval(BaseModel):
    start: str
    end: str
    purpose: str = Field(default="other commitment", pattern=r"^(meeting|training|other commitment)$")

    def checked(self):
        if aware(self.end) <= aware(self.start):
            raise ValueError("Invalid interval")
        return self


class Duty(BaseModel):
    first_meeting: str
    final_return: str | None = None


class Candidate(BaseModel):
    staff_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=100)
    active: bool
    marker: str = Field(pattern=r"^(blue|light_blue|pink|sick|holiday|training|unknown)$")
    regular_group: int | None = Field(default=None, ge=1, le=4)
    availability: Interval | None = None
    availability_note: str | None = Field(default=None, max_length=120)
    commitments_checked: bool = False
    duties_checked: bool = False
    commitments: list[Interval] = Field(default_factory=list)
    duties: list[Duty] = Field(default_factory=list)


class Plan(BaseModel):
    service_date: str
    observed_at: str
    source_reference: str = Field(min_length=1, max_length=200)
    roster_complete: bool
    candidates: list[Candidate] = Field(max_length=150)

    def checked(self, now):
        day = date.fromisoformat(self.service_date)
        today = now.astimezone(LONDON).date()
        if not today <= day <= today + timedelta(days=2):
            raise ValueError("Date outside rolling two-day horizon")
        observed = aware(self.observed_at)
        age = (now.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
        if age < -120 or age > 86400:
            raise ValueError("Availability evidence requires refresh")
        if not self.roster_complete:
            raise ValueError("Complete roster evidence is required")
        ids = [x.staff_id for x in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate staff IDs")
        for c in self.candidates:
            if re.search(r"[\[\]\r\n<>|]", c.name) or (c.availability_note and re.search(r"[\[\]\r\n<>|]", c.availability_note)):
                raise ValueError("Plain staff display name required")
            for interval in ([c.availability] if c.availability else []) + c.commitments:
                interval.checked()
            if c.availability and aware(c.availability.start).astimezone(LONDON).date() != day:
                raise ValueError("Availability must start on service date")
            for duty in c.duties:
                aware(duty.first_meeting)
                if duty.final_return:
                    rest_summary(duty.first_meeting, duty.final_return)
        return self


def eligible_window(c):
    """Trim known commitments/rest; never infer availability or an actual return."""
    if not c.active or c.marker not in ("blue", "light_blue"):
        return None, "Excluded by rota"
    if c.marker == "light_blue" and not c.commitments:
        return None, "Required for another commitment; purpose and time need verification"
    if not c.availability or not c.commitments_checked or not c.duties_checked:
        return None, "Availability, commitments or duty history need verification"
    start, end = aware(c.availability.start), aware(c.availability.end)
    for duty in c.duties:
        ds = aware(duty.first_meeting)
        if not duty.final_return:
            if ds < end:
                return None, "Final meeting-point return unconfirmed"
            continue
        de = aware(duty.final_return)
        summary = rest_summary(duty.first_meeting, duty.final_return)
        if ds < end and de > start:
            return None, "Overlapping duty"
        if de <= start and summary["earliest_next_meeting"]:
            start = max(start, aware(summary["earliest_next_meeting"]))
        elif ds >= end:
            # A future duty also reserves its start; do not assume it can move.
            end = min(end, ds)
    # Remove busy intervals. Keep the largest remaining continuous window.
    windows = [(start, end)] if start < end else []
    for busy in c.commitments:
        bs, be = aware(busy.start), aware(busy.end)
        split = []
        for ws, we in windows:
            if be <= ws or bs >= we:
                split.append((ws, we))
            else:
                if ws < bs:
                    split.append((ws, bs))
                if be < we:
                    split.append((be, we))
        windows = split
    if not windows:
        return None, "No free window after commitments/rest"
    return max(windows, key=lambda w: (w[1].astimezone(timezone.utc)-w[0].astimezone(timezone.utc)).total_seconds()), None


def plan_lines(plan):
    groups = {i: [] for i in range(1, 5)}
    pending_groups = {i: [] for i in range(1, 5)}
    spares, pending_spares = [], []
    for c in plan.candidates:
        window, reason = eligible_window(c)
        caveat = ""
        if c.marker == "light_blue":
            required = []
            for busy in c.commitments:
                start, end = [aware(x).astimezone(LONDON) for x in (busy.start, busy.end)]
                required.append(f"{busy.purpose} {start:%d %b %H:%M}–{end:%d %b %H:%M}")
            caveat = " | Required for: " + ("; ".join(required) if required else "another commitment — purpose/time to confirm")
        if c.availability_note:
            caveat += " | " + c.availability_note
        if window:
            start, end = [x.astimezone(LONDON) for x in window]
            text = f"{c.name} ({start:%d %b %H:%M}–{end:%d %b %H:%M})" + caveat
            (groups[c.regular_group] if c.regular_group else spares).append(text)
        elif c.active and c.marker in ("blue", "light_blue", "unknown"):
            text = c.name + (f": {reason}" if reason != "Availability, commitments or duty history need verification" else "") + caveat
            if c.availability:
                start, end = [aware(x).astimezone(LONDON) for x in (c.availability.start, c.availability.end)]
                text += f" | Rota window {start:%d %b %H:%M}–{end:%d %b %H:%M}"
            (pending_groups[c.regular_group] if c.regular_group else pending_spares).append(text)
    lines = ["PROVISIONAL — staffing pool, not a job allocation or dispatch clearance.",
             "Rota checked: " + aware(plan.observed_at).astimezone(LONDON).strftime("%d %b %Y %H:%M %Z"),
             "Rota evidence expires: " + (aware(plan.observed_at).astimezone(timezone.utc)+timedelta(hours=24)).astimezone(LONDON).strftime("%d %b %Y %H:%M %Z")]
    for group, staff in groups.items():
        if staff:
            lines.append(f"Staff Group {group}: " + "; ".join(staff))
        if pending_groups[group]:
            lines.append(f"Staff Group {group} (provisional — checks pending): " + "; ".join(pending_groups[group]))
    lines.append("SPARE STAFF: " + ("; ".join(spares) if spares else "None verified"))
    if pending_spares:
        lines.append("SPARE STAFF (provisional — checks pending): " + "; ".join(pending_spares))
    if any(pending_groups.values()) or pending_spares:
        lines.append("Pending checks: confirm availability times, all commitments, duty history, required rest and crew release before allocation.")
    if not any(groups.values()) and not spares:
        lines.append("No staff cleared for the provisional pool from the supplied evidence.")
    lines.append("Before dispatch: recheck FMS, all bookings, RAC/chat release, driver/vehicle and meeting-point travel. "
                 "Keep the first meeting across chained jobs; >12h duty requires 11h rest after final return. "
                 "Aim for ≤16h; offer accommodation if exceeded. Meeting travel ideally ≤30 minutes.")
    return lines


def replace_plan_block(original, lines):
    block = START + "\n" + "\n".join(lines) + "\n" + END
    if START not in original and END not in original:
        return original.rstrip() + "\n\n" + block
    if original.count(START) != 1 or original.count(END) != 1 or original.index(START) >= original.index(END):
        raise ValueError("Ambiguous provisional block")
    return original[:original.index(START)] + block + original[original.index(END)+len(END):]


class DailyTeams:
    def __init__(self, sync, now=lambda: datetime.now(timezone.utc)):
        self.sync, self.now = sync, now
        self.enabled = os.getenv("BITRIX_DAILY_TEAMS_ENABLED") == "true"
        self.write = os.getenv("BITRIX_DAILY_TEAMS_WRITE_ENABLED") == "true"
        self.author_id = int(os.getenv("BITRIX_DAILY_TEAMS_AUTHOR_ID", "1037"))
        # Exact existing audience, verified on the current Feed post. No UA default.
        self.dest = json.loads(os.getenv("BITRIX_DAILY_TEAMS_DEST", '["SG127"]'))
        self.last_run, self.error = None, None
        sync.daily = self

    def ready(self):
        store = self.sync.ready()
        if not self.dest or any(not re.fullmatch(r"(?:SG|U|DR)\d+", x) for x in self.dest):
            raise ValueError("Explicit restricted Feed recipients required")
        store.db.executescript("""
          CREATE TABLE IF NOT EXISTS daily_posts(service_date TEXT PRIMARY KEY, post_id INTEGER UNIQUE);
          CREATE TABLE IF NOT EXISTS daily_plans(service_date TEXT PRIMARY KEY, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS daily_creates(service_date TEXT PRIMARY KEY, state TEXT NOT NULL);
        """)
        return store

    async def discover(self):
        # Read complete pagination in the existing audience; missing page = no writes.
        result, seen = {}, set()
        for page in range(200):
            posts = await self.sync.call(self.sync.settings, "log.blogpost.get", {"LOG_RIGHTS": self.dest, "start": page*50})
            if not isinstance(posts, list):
                raise ValueError("Invalid Feed page")
            for post in posts:
                pid = int(post["ID"])
                if pid in seen:
                    raise ValueError("Feed changed during pagination; retry")
                seen.add(pid)
                day = post_date(post)
                if day and int(post.get("AUTHOR_ID", 0)) == self.author_id:
                    result.setdefault(day, []).append(post)
            if len(posts) < 50:
                break
        else:
            raise ValueError("Incomplete Feed pagination")
        return result

    def save_plan(self, plan):
        plan.checked(self.now())
        store = self.ready()
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO daily_plans VALUES (?,?)", (plan.service_date, plan.model_dump_json()))

    def import_config(self, raw):
        """Import a current operator-verified snapshot via existing service admin.

        No public tool, new credential or automatic colour inference. Retain the
        original observation time, and never overwrite a newer protected import.
        """
        if not raw:
            return 0
        if len(raw.encode()) > 65536:
            raise ValueError("Roster snapshot too large")
        items = json.loads(raw)
        if not isinstance(items, list) or not 1 <= len(items) <= 3:
            raise ValueError("Expected up to three dated roster plans")
        plans = [Plan.model_validate(item) for item in items]
        if len({p.service_date for p in plans}) != len(plans):
            raise ValueError("Duplicate snapshot dates")
        now = self.now()
        today = now.astimezone(LONDON).date()
        current = []
        for plan in plans:
            day = date.fromisoformat(plan.service_date)
            observed = aware(plan.observed_at)
            if day < today or (now.astimezone(timezone.utc)-observed.astimezone(timezone.utc)).total_seconds() > 86400:
                continue
            current.append(plan.checked(now))
        store = self.ready()
        imported = 0
        with store.db:
            for plan in current:
                old = store.db.execute("SELECT data FROM daily_plans WHERE service_date=?", (plan.service_date,)).fetchone()
                if old:
                    previous = Plan.model_validate_json(old[0])
                    if aware(previous.observed_at) > aware(plan.observed_at):
                        continue
                    if aware(previous.observed_at) == aware(plan.observed_at):
                        if previous != plan:
                            raise ValueError("Conflicting roster snapshot")
                        continue
                store.db.execute("INSERT OR REPLACE INTO daily_plans VALUES (?,?)", (plan.service_date,plan.model_dump_json()))
                imported += 1
        return imported

    async def run(self):
        if not self.enabled:
            return {"enabled": False}
        async with self.sync.lock:
            store = self.ready()
            try:
                self.import_config(os.getenv("BITRIX_DAILY_TEAMS_ROSTER_SNAPSHOT", ""))
                found = await self.discover()
                today = self.now().astimezone(LONDON).date()
                results = []
                for day in [(today+timedelta(days=i)).isoformat() for i in range(3)]:
                    matches = found.get(day, [])
                    if len(matches) > 1:
                        with store.db:
                            store.db.execute("DELETE FROM daily_posts WHERE service_date=?", (day,))
                        results.append({"date": day, "state": "duplicate_requires_review"})
                        continue
                    post = matches[0] if matches else None
                    if post:
                        with store.db:
                            store.db.execute("INSERT OR REPLACE INTO daily_posts VALUES (?,?)", (day, int(post["ID"])))
                        # Retarget only bindings for this service date; never copy yesterday's crew.
                        for row in store.db.execute("SELECT data FROM bindings").fetchall():
                            from team_sync import Binding
                            b = Binding.model_validate_json(row[0])
                            if b.daily_route and b.service_date == day and b.post_id != int(post["ID"]):
                                old_id = b.post_id
                                b.post_id = int(post["ID"])
                                store.bind(b)
                                with store.db:
                                    store.db.execute("INSERT OR IGNORE INTO pending VALUES (?,NULL)", (b.post_id,))
                                    if not any(json.loads(x[0])["post_id"] == old_id for x in store.db.execute("SELECT data FROM bindings")):
                                        store.db.execute("DELETE FROM pending WHERE post_id=?", (old_id,))
                    else:
                        with store.db:
                            store.db.execute("DELETE FROM daily_posts WHERE service_date=?", (day,))
                    raw = store.db.execute("SELECT data FROM daily_plans WHERE service_date=?", (day,)).fetchone()
                    if not raw:
                        results.append({"date":day, "post_id":int(post["ID"]) if post else None, "state":"awaiting_verified_roster"})
                        continue
                    plan = Plan.model_validate_json(raw[0])
                    try:
                        plan.checked(self.now())
                    except ValueError:
                        results.append({"date":day, "state":"evidence_requires_refresh"})
                        continue
                    lines = plan_lines(plan)
                    if not self.write:
                        results.append({"date":day, "state":"preview", "lines":lines})
                        continue
                    if not post:
                        previous = store.db.execute("SELECT state FROM daily_creates WHERE service_date=?", (day,)).fetchone()
                        if previous:
                            results.append({"date":day, "state":"creation_requires_reconciliation"})
                            continue
                        # Durable intent before the external call: an uncertain timeout never retries add.
                        with store.db:
                            store.db.execute("INSERT INTO daily_creates VALUES (?,?)", (day, "creating"))
                        pid = int(await self.sync.call(self.sync.settings, "log.blogpost.add", {
                            "POST_TITLE":service_title(day), "POST_MESSAGE":replace_plan_block(service_title(day), lines),
                            "DEST":self.dest, "USER_ID":self.author_id, "PARSE_PREVIEW":"N"}))
                        posts = await self.sync.call(self.sync.settings, "log.blogpost.get", {"POST_ID":pid})
                        post = next(x for x in posts if int(x["ID"]) == pid)
                        if post_date(post) != day or int(post["AUTHOR_ID"]) != self.author_id:
                            raise ValueError("Created post requires verification")
                    else:
                        before = post["DETAIL_TEXT"]
                        after = replace_plan_block(before, lines)
                        if before != after:
                            fresh = await self.sync.call(self.sync.settings, "log.blogpost.get", {"POST_ID":int(post["ID"])})
                            if next(x for x in fresh if int(x["ID"]) == int(post["ID"])) != post:
                                raise ValueError("Manual post edit; retry")
                            await self.sync.call(self.sync.settings, "log.blogpost.update", {"POST_ID":int(post["ID"]),"POST_TITLE":post["TITLE"],"POST_MESSAGE":after})
                    verified = await self.sync.call(self.sync.settings, "log.blogpost.get", {"POST_ID":int(post["ID"])})
                    check = next(x for x in verified if int(x["ID"]) == int(post["ID"]))
                    if check["DETAIL_TEXT"] != replace_plan_block(post["DETAIL_TEXT"], lines):
                        raise ValueError("Publication requires verification")
                    with store.db:
                        store.db.execute("INSERT OR REPLACE INTO daily_posts VALUES (?,?)", (day,int(post["ID"])))
                    results.append({"date":day,"post_id":int(post["ID"]),"state":"provisional"})
                self.last_run, self.error = self.now().isoformat(), None
                logging.getLogger("uvicorn.error").info("Daily teams refresh: %s", "; ".join(
                    f"{x['date']}={x['state']} (post={x.get('post_id', 'none')})" for x in results))
                return {"enabled":True,"write_enabled":self.write,"days":results}
            except Exception:
                self.error = "Daily lists require retry or control-room review"
                logging.getLogger("uvicorn.error").warning(self.error)
                raise RuntimeError(self.error) from None


def build_daily_router(daily, authorize):
    router = APIRouter(prefix="/team-sync/daily")

    @router.put("/plans", dependencies=[Depends(authorize)])
    async def save(plan: Plan):
        try:
            async with daily.sync.lock:
                daily.save_plan(plan)
        except ValueError:
            raise HTTPException(422,"Complete, current and date-matched roster evidence required") from None
        return {"saved":True,"service_date":plan.service_date,"lines":plan_lines(plan)}

    @router.post("/refresh", dependencies=[Depends(authorize)])
    async def refresh():
        try:
            return await daily.run()
        except RuntimeError:
            raise HTTPException(503,"Daily lists require retry or control-room review") from None

    @router.get("/status", dependencies=[Depends(authorize)])
    async def status():
        return {"enabled":daily.enabled,"write_enabled":daily.write,"last_run":daily.last_run,"error":daily.error}

    return router
