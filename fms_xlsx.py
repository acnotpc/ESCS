"""Read Findmyshift facility-view exports without inferring missing availability.

Only dated staff-group cells supply availability markers. Colours on headings,
placements and training rooms are never treated as staff availability.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import json
import re
import unicodedata
from xml.etree import ElementTree as ET
from zipfile import ZipFile, BadZipFile

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
MAX_UPLOAD = 2 * 1024 * 1024
MAX_EXPANDED = 12 * 1024 * 1024
# Exact user-confirmed rota colours, not nearest-colour guesses.
COLOURS = {
    "085278": "blue", "2D6C8C": "blue", "126894": "blue",
    "00FFFF": "light_blue", "33FFFF": "light_blue",
    "FF33CC": "pink", "FF0000": "sick", "663300": "training",
    "33CC00": "holiday", "00CC00": "holiday", "00FF00": "holiday",
}
SUFFIX = re.compile(r"\s*\((?:S|P|B|AS|SIA|C1|M|F)\)\s*$", re.I)
GROUP = re.compile(r"staff\s+group\s+([1-4])", re.I)
TIME = re.compile(r"^(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})$")


def name_key(value):
    value = unicodedata.normalize("NFKC", value).replace("’", "'")
    while SUFFIX.search(value):
        value = SUFFIX.sub("", value)
    value = re.sub(r"^\(B\)\s*", "", value, flags=re.I)
    return " ".join(value.split()).casefold()


def _xml(raw):
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("Unsupported XML declarations")
    return ET.fromstring(raw)


def _text(node):
    return "".join(t.text or "" for t in node.findall(".//m:t", NS))


def read_cells(data):
    """Bounded, read-only XLSX extraction. Preserve RGB and cell references."""
    if not data or len(data) > MAX_UPLOAD:
        raise ValueError("Export exceeds the upload limit")
    try:
        with ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or len(names) > 100:
                raise ValueError("Unsupported archive structure")
            if sum(x.file_size for x in archive.infolist()) > MAX_EXPANDED:
                raise ValueError("Export exceeds the expanded size limit")
            workbook = _xml(archive.read("xl/workbook.xml"))
            sheets = workbook.findall("m:sheets/m:sheet", NS)
            if len(sheets) != 1:
                raise ValueError("Expected one facility-view worksheet")
            rels = _xml(archive.read("xl/_rels/workbook.xml.rels"))
            rid = sheets[0].get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            rel = next((r for r in rels if r.get("Id") == rid), None)
            if rel is None or rel.get("TargetMode") == "External":
                raise ValueError("Unsupported worksheet relationship")
            target = rel.get("Target", "")
            if ".." in target.split("/"):
                raise ValueError("Unsupported worksheet relationship")
            path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            sheet = _xml(archive.read(path))
            if sheet.findall("m:conditionalFormatting", NS):
                raise ValueError("Conditional colours require visual verification")
            styles = _xml(archive.read("xl/styles.xml"))
            fills = styles.find("m:fills", NS)
            formats = styles.find("m:cellXfs", NS)
            strings = [_text(si) for si in _xml(archive.read("xl/sharedStrings.xml"))] if "xl/sharedStrings.xml" in names else []
            cells = {}
            for row in sheet.findall("m:sheetData/m:row", NS):
                for cell in row.findall("m:c", NS):
                    ref = cell.get("r", "")
                    if not re.fullmatch(r"[A-Z]{1,3}[1-9]\d{0,4}", ref) or ref in cells:
                        raise ValueError("Invalid or repeated cell reference")
                    if cell.find("m:f", NS) is not None:
                        raise ValueError("Formula-based rota entries require verification")
                    value = cell.find("m:v", NS)
                    if cell.get("t") == "s":
                        text = strings[int(value.text)] if value is not None else ""
                    elif cell.get("t") == "inlineStr":
                        text = _text(cell)
                    else:
                        text = value.text if value is not None else ""
                    style = formats[int(cell.get("s", "0"))]
                    fill = fills[int(style.get("fillId", "0"))].find("m:patternFill", NS)
                    fg = fill.find("m:fgColor", NS) if fill is not None else None
                    rgb = fg.get("rgb", "") if fg is not None and fill.get("patternType") == "solid" else ""
                    if fg is not None and float(fg.get("tint", "0")) != 0:
                        rgb = ""
                    colour = rgb[-6:].upper() if re.fullmatch(r"(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})", rgb) else None
                    cells[ref] = {"text": text.strip(), "colour": colour}
            return sheets[0].get("name"), cells
    except (BadZipFile, KeyError, IndexError, TypeError, ET.ParseError, StopIteration) as exc:
        raise ValueError("Invalid or unsupported FMS export") from exc


def _staff_index(staff):
    index = defaultdict(dict)
    for row in staff:
        sid = row.get("staffId")
        if not sid:
            continue
        canonical = " ".join(filter(None, (row.get("firstName"), row.get("lastName")))).strip()
        for display in (canonical, row.get("displayName"), row.get("name")):
            if display:
                index[name_key(display)][sid] = row
    return index


def _active(row, day):
    # Do not infer an active employee from a name or appearance in a group.
    if row.get("active") is not True:
        return False
    start, finish = row.get("dateStarted"), row.get("dateFinished")
    return (not start or str(start)[:10] <= day) and (not finish or str(finish)[:10] > day)


def _matches(index, value, day):
    rows = list(index.get(name_key(value), {}).values())
    active = [row for row in rows if _active(row, day)]
    # Historical duplicate accounts cannot displace a unique active employee.
    # Multiple active matches remain ambiguous and require a verified staff ID.
    return active if active else rows


def parse_export(data, staff, observed_at, now=None):
    now = now or datetime.now(timezone.utc)
    observed = datetime.fromisoformat(observed_at)
    if observed.tzinfo is None or now.tzinfo is None:
        raise ValueError("Timezone-aware export capture time required")
    age = (now - observed).total_seconds()
    if age < -120 or age > 86400:
        raise ValueError("Export capture must be within the last 24 hours")
    sheet_name, cells = read_cells(data)
    dates = {}
    for ref, cell in cells.items():
        if not ref.endswith("1") or re.sub(r"\D", "", ref) != "1":
            continue
        try:
            day = datetime.strptime(cell["text"], "%a. %b. %d, %Y").date()
        except ValueError:
            continue
        dates[ref[:-1]] = day.isoformat()
    if len(dates) != 7:
        raise ValueError("Expected seven dated columns")
    sequence = [date.fromisoformat(d) for d in dates.values()]
    if any(b - a != timedelta(days=1) for a, b in zip(sequence, sequence[1:])):
        raise ValueError("Expected a continuous dated week")
    index = _staff_index(staff)
    days = {d: {"staff": [], "additional_staff_evidence": [], "issues": []} for d in dates.values()}
    sections = {}
    group_rows = set()
    group = None
    section = ""
    groups_seen = set()
    max_row = max(int(re.sub(r"\D", "", ref)) for ref in cells)
    for r in range(2, max_row + 1):
        heading = cells.get(f"A{r}", {}).get("text", "")
        if heading:
            section = heading
            match = GROUP.fullmatch(heading)
            group = int(match[1]) if match else None
            if group:
                groups_seen.add(group)
        sections[r] = section
        if not group:
            continue
        group_rows.add(r)
        for column, day in dates.items():
            ref = f"{column}{r}"
            cell = cells.get(ref, {"text": "", "colour": None})
            display = cell["text"]
            if display in ("", "-", "—"):
                continue
            matches = _matches(index, display, day)
            if len(matches) != 1:
                days[day]["issues"].append({"cell": ref, "reason": "unmatched_name" if not matches else "ambiguous_name", "name": display})
                continue
            row = matches[0]
            sid = row["staffId"]
            if any(s["staff_id"] == sid for s in days[day]["staff"]):
                raise ValueError("Duplicate staff member in dated groups")
            marker = COLOURS.get(cell["colour"], "unknown")
            active = _active(row, day)
            record = {"staff_id": sid, "name": display, "active": active,
                      "regular_group": group, "marker": marker,
                      "source_cell": ref, "source_colour": cell["colour"],
                      "commitment_evidence": [], "next_day_marker": None}
            if active and marker == "unknown":
                days[day]["issues"].append({"cell": ref, "reason": "availability_marker_unconfirmed", "staff_id": sid})
            days[day]["staff"].append(record)
    if groups_seen != {1, 2, 3, 4}:
        raise ValueError("All four staff groups are required")
    # Keep all non-group appearances as evidence. A time/name association is a
    # candidate commitment only; ordering and overlapping allocations need review.
    for column, day in dates.items():
        by_id = {s["staff_id"]: s for s in days[day]["staff"]}
        running_section = None
        last_time = None
        for r in range(2, max_row + 1):
            if r in group_rows:
                continue
            if sections[r] != running_section:
                running_section, last_time = sections[r], None
            ref = f"{column}{r}"
            text = cells.get(ref, {}).get("text", "")
            time = TIME.fullmatch(text)
            if time:
                last_time = {"cell": ref, "text": text}
            matches = _matches(index, text, day) if text else []
            if len(matches) != 1:
                continue
            sid = matches[0]["staffId"]
            evidence = {
                "section": running_section, "name_cell": ref,
                "time_evidence": last_time,
                "nearby_text": [{"cell": f"{column}{rr}", "text": cells.get(f"{column}{rr}", {}).get("text", "")}
                                for rr in range(max(2, r - 3), min(max_row, r + 3) + 1)
                                if sections[rr] == running_section and cells.get(f"{column}{rr}", {}).get("text")],
            }
            if re.search(r"bank staff|available full time.*overtime", running_section, re.I):
                days[day]["additional_staff_evidence"].append({
                    "staff_id": sid, "name": text, "active": _active(matches[0], day),
                    "overtime": "overtime" in running_section.lower(),
                    "availability_confirmed": False, "evidence": evidence})
            if sid in by_id:
                by_id[sid]["commitment_evidence"].append(evidence)
    # Next-day highlighting is taken from the same exact staff ID and next date.
    for day, evidence in days.items():
        tomorrow = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        tomorrow_rows = {s["staff_id"]: s for s in days.get(tomorrow, {}).get("staff", [])}
        for row in evidence["staff"]:
            if row["staff_id"] in tomorrow_rows:
                row["next_day_marker"] = tomorrow_rows[row["staff_id"]]["marker"]
            if row["active"] and row["marker"] == "light_blue" and not row["commitment_evidence"]:
                evidence["issues"].append({"cell": row["source_cell"], "reason": "light_blue_commitment_missing", "staff_id": row["staff_id"]})
            if row["active"] and row["marker"] in ("blue", "light_blue", "unknown"):
                for entry in row["commitment_evidence"]:
                    if re.search(r"sickness|holiday|training|placement|transport", entry["section"], re.I):
                        evidence["issues"].append({"cell": row["source_cell"], "reason": "other_allocation_requires_review", "staff_id": row["staff_id"], "section": entry["section"]})
        rostered = {s["staff_id"] for s in evidence["staff"]}
        evidence["active_conveyance_outside_groups"] = [
            {"staff_id": s["staffId"], "name": s.get("displayName") or " ".join(filter(None, (s.get("firstName"), s.get("lastName"))))}
            for s in staff if s.get("staffId") and _active(s, day)
            and "conveyance" in (s.get("department") or "").lower()
            and s["staffId"] not in rostered]
        evidence["counts"] = {"active": sum(s["active"] for s in evidence["staff"]),
                              "blue": sum(s["active"] and s["marker"] == "blue" for s in evidence["staff"]),
                              "light_blue": sum(s["active"] and s["marker"] == "light_blue" for s in evidence["staff"]),
                              "next_day_pink": sum(s["active"] and s["next_day_marker"] == "pink" for s in evidence["staff"])}
    return {"source_sha256": sha256(data).hexdigest(), "sheet": sheet_name,
            "observed_at": observed.isoformat(), "days": days,
            "dispatch_ready": False,
            "requires_review": ["availability hours", "commitment intervals", "duty history and release", "map membership and vehicle checks"],
            "automatic_collection_configured": False}


def build_xlsx_router(daily, authorize, fms_get, team_id):
    """Private upload and durable evidence store; never modifies plans or Feed."""
    from fastapi import APIRouter, Depends, HTTPException, Query, Request
    router = APIRouter(prefix="/team-sync/daily")

    @router.post("/xlsx-import", dependencies=[Depends(authorize)])
    async def upload(request: Request, observed_at: str = Query(...),
                     save_evidence: bool = Query(False)):
        buffer = bytearray()
        async for chunk in request.stream():
            if len(buffer) + len(chunk) > MAX_UPLOAD:
                raise HTTPException(413, "Export exceeds the upload limit")
            buffer.extend(chunk)
        data = bytes(buffer)
        try:
            rows = await fms_get("staff/list", {"teamId": team_id})
            staff = rows if isinstance(rows, list) else [rows]
            result = parse_export(data, staff, observed_at, daily.now())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if save_evidence:
            async with daily.sync.lock:
                store = daily.ready()
                with store.db:
                    store.db.execute("CREATE TABLE IF NOT EXISTS fms_colour_exports (digest TEXT PRIMARY KEY, observed_at TEXT NOT NULL, data TEXT NOT NULL)")
                    old = store.db.execute("SELECT observed_at FROM fms_colour_exports WHERE digest=?", (result["source_sha256"],)).fetchone()
                    if old and old[0] != result["observed_at"]:
                        raise HTTPException(409, "Identical export cannot receive a new capture time")
                    store.db.execute("INSERT OR IGNORE INTO fms_colour_exports VALUES (?,?,?)", (result["source_sha256"], result["observed_at"], json.dumps(result)))
        return {**result, "saved_evidence": save_evidence, "plans_changed": False, "posts_changed": False}

    return router
