"""Read the bounded, static FMS HTML download without executing it.

Only explicit per-person inline colours establish individual markers. Surrounding
group styles are never substituted. Names still require unique active FMS IDs.
"""
from datetime import datetime
from html.parser import HTMLParser
import re

from fms_xlsx import COLOURS, GROUP, MAX_UPLOAD, build_export_router, parse_rota_cells


class Node:
    def __init__(self, tag, attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def text(self):
        return "".join(c.text() if isinstance(c, Node) else c for c in self.children)

    def find(self, tag):
        for child in self.children:
            if isinstance(child, Node):
                if child.tag == tag:
                    yield child
                yield from child.find(tag)


class StaticHTML(HTMLParser):
    VOID = {"area", "base", "br", "col", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack, self.nodes = [self.root], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "iframe", "object", "embed"}:
            raise ValueError("Active HTML content is not supported")
        if len(attrs) != len({key for key, _ in attrs}):
            raise ValueError("Duplicate HTML attributes")
        self.nodes += 1
        if self.nodes > 20000 or len(self.stack) > 64:
            raise ValueError("HTML export is too complex")
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if len(self.stack) < 2 or self.stack[-1].tag != tag:
            raise ValueError("Malformed HTML export")
        self.stack.pop()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_data(self, text):
        self.stack[-1].children.append(text)


def inline_colour(style):
    # CSS cascade uses the last declaration. A later unsupported shorthand must
    # not be silently displaced by an earlier recognised background-color.
    colour = None
    for declaration in (style or "").split(";"):
        key, separator, value = declaration.partition(":")
        if not separator or key.strip().lower() not in {"background", "background-color"}:
            continue
        value = value.strip()
        match = re.fullmatch(r"#([a-fA-F0-9]{3}|[a-fA-F0-9]{6})", value)
        rgb = match[1] if match else None
        colour = ("".join(c * 2 for c in rgb) if len(rgb) == 3 else rgb).upper() if rgb else None
    return colour


def read_html_cells(data):
    if not data or len(data) > MAX_UPLOAD:
        raise ValueError("Export exceeds the upload limit")
    if b"<!ENTITY" in data.upper():
        raise ValueError("Unsupported HTML declarations")
    parser = StaticHTML()
    try:
        parser.feed(data.decode("utf-8-sig"))
        parser.close()
    except UnicodeError as exc:
        raise ValueError("Expected a UTF-8 HTML export") from exc
    if len(parser.stack) != 1:
        raise ValueError("Incomplete HTML export")
    tables = [n for n in parser.root.find("table") if n.attrs.get("id") == "tblWeeklyView"]
    if len(tables) != 1 or list(tables[0].find("table")):
        raise ValueError("Expected one FMS weekly-view table")
    rows = list(tables[0].find("tr"))
    if not rows:
        raise ValueError("Missing rota rows")
    header = [n for n in rows[0].children if isinstance(n, Node) and n.tag in {"td", "th"}]
    dated = [(i, c) for i, c in enumerate(header) if c.attrs.get("date")]
    if [i for i, _ in dated] != list(range(1, 8)):
        raise ValueError("Expected seven dated columns")
    if any(c.attrs.get("colspan", "1") != "1" or c.attrs.get("rowspan", "1") != "1" for c in header):
        raise ValueError("Unsupported rota header layout")
    cells = {}
    for column, (_, cell) in zip("BCDEFGH", dated):
        try:
            day = datetime.strptime(cell.attrs["date"], "%d/%m/%Y")
        except ValueError as exc:
            raise ValueError("Invalid rota date") from exc
        cells[column + "1"] = {"text": day.strftime("%a. %b. %d, %Y"), "colour": None}
    output_row = 2
    for source_row, row in enumerate(rows[1:], 2):
        row_cells = [n for n in row.children if isinstance(n, Node) and n.tag in {"td", "th"}]
        if len(row_cells) not in {8, 9} or any(c.attrs.get("colspan", "1") != "1" or c.attrs.get("rowspan", "1") != "1" for c in row_cells):
            raise ValueError("Unsupported rota cell layout")
        heading = row_cells[0].text().strip()
        group = bool(GROUP.fullmatch(heading))
        entries = []
        for source_column, cell in enumerate(row_cells[1:8], 2):
            row_entries = []
            divs = [n for n in cell.children if isinstance(n, Node) and n.tag == "div"]
            for number, entry in enumerate(divs, 1):
                person = "oU" in (entry.attrs.get("class") or "").split()
                if group and not person:
                    continue
                colour = inline_colour(entry.attrs.get("style"))
                row_entries.append({"text": entry.text().strip(), "colour": colour,
                                    "individual_colour_verified": person and colour in COLOURS,
                                    "source_locator": f"#tblWeeklyView tr:nth-child({source_row}) td:nth-child({source_column}) div:nth-of-type({number})"})
            # Unsupported text layouts must not silently look like empty groups.
            if cell.text().strip() and not divs:
                raise ValueError("Unsupported rota entry layout")
            if group and not row_entries:
                raise ValueError("Missing individual staff group entries")
            entries.append(row_entries)
        height = max(1, max(len(e) for e in entries))
        cells[f"A{output_row}"] = {"text": heading, "colour": None}
        for offset in range(height):
            for column, items in zip("BCDEFGH", entries):
                if offset < len(items):
                    cells[f"{column}{output_row + offset}"] = items[offset]
        output_row += height
    return cells


def parse_html_export(data, staff, observed_at, now=None):
    cells = read_html_cells(data)
    return parse_rota_cells(data, staff, observed_at, now, "FMS HTML weekly view", cells, source_format="html")


def build_html_router(daily, authorize, fms_get, team_id):
    return build_export_router(daily, authorize, fms_get, team_id, "/html-import", parse_html_export)


async def preview_html_rota(html: str, observed_at: str, service_date: str,
                            fms_get, now=None):
    """Read-only connector intake: no database, plan or Feed writes."""
    from datetime import date, timedelta
    from fms_xlsx import MAX_UPLOAD
    requested = date.fromisoformat(service_date)
    if requested.isoformat() != service_date:
        raise ValueError("Service date must be YYYY-MM-DD")
    data = html.encode("utf-8")
    if len(data) > MAX_UPLOAD:
        raise ValueError("Export exceeds the upload limit")
    # Reject malformed input before spending an upstream API request.
    read_html_cells(data)
    rows = await fms_get("staff/list", {"teamId": "4h2o8q6h"})
    staff = rows if isinstance(rows, list) else [rows]
    result = parse_html_export(data, staff, observed_at, now)
    if service_date not in result["days"]:
        raise ValueError("Service date is outside the exported week")
    following = (requested + timedelta(days=1)).isoformat()
    coverage = list(result["days"])
    result["days"] = {d: result["days"][d] for d in (service_date, following)
                      if d in result["days"]}
    return {**result, "export_date_coverage": coverage,
            "next_day_covered": following in result["days"],
            "saved_evidence": False, "plans_changed": False,
            "posts_changed": False}
