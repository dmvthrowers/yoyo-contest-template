#!/usr/bin/env python3
"""Build the contest website from site.jsonc + a preset into _site/.

    python3 build.py                       # build into _site/
    python3 build.py --serve               # build, then preview at http://localhost:8000/
    python3 build.py --today 2026-09-20    # preview the site as it looks on another day

No installs needed: standard-library Python 3.9+ only.

How it fits together:
  site.jsonc        your contest settings (the only file most people edit)
  presets/*.json    starting divisions, rules, gear, schedule, FAQ, and terms for each kind of
                    contest (yo-yo, kendama, diabolo, spin top, mixed skill toys, trick battle)
  assets/           stylesheet, script, and your images (copied as-is)
  content/*.html    optional extra HTML added to the bottom of a page (e.g. content/venue.html)
  build.py          this file: the contest's phase (registration open, today, wrap-up),
                    then one small function per page, near the bottom
"""
import argparse
import datetime as dt
import html
import http.server
import json
import math
import os
import re
import shutil
import struct
import sys
import zlib
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "_site"
warnings = []


# ---------------------------------------------------------------- config

def load_jsonc(path):
    """JSON that allows whole-line // comments and trailing commas."""
    # Blank out comment lines (instead of removing them) so error line numbers match the file.
    lines = ["" if l.lstrip().startswith("//") else l for l in path.read_text(encoding="utf-8").splitlines()]
    text = re.sub(r",(\s*[}\]])", r"\1", "\n".join(lines))
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        sys.exit(f"\n{path.name} has a typo near line {e.lineno}: {e.msg}.\n"
                 "Check for a missing comma or quote on that line or the one above it.\n")


def merge(base, override):
    """Settings in site.jsonc win over the preset. Empty strings/lists don't erase preset values."""
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            out[k] = merge(base.get(k), v) if k in base else v
        return out
    if override in (None, "", []) and base not in (None, "", []):
        return base
    return override


def load_config(config_path=ROOT / "site.jsonc"):
    site = load_jsonc(config_path)
    name = site.get("preset") or "yoyo-contest"
    preset_path = ROOT / "presets" / f"{name}.json"
    if not preset_path.exists():
        choices = ", ".join(sorted(p.stem for p in (ROOT / "presets").glob("*.json")))
        sys.exit(f'\nUnknown preset "{name}" in site.jsonc. Choose one of: {choices}\n')
    preset = json.loads(preset_path.read_text(encoding="utf-8"))
    return merge(preset, site)


# ---------------------------------------------------------------- helpers

esc = html.escape


def ext_link(url, label, cls=""):
    c = f' class="{cls}"' if cls else ""
    return f'<a{c} href="{esc(url)}" rel="noopener noreferrer">{esc(label)}</a>'


def mailto(email, subject=""):
    if not (email or "").strip():
        sys.exit('\ncontact.email is empty in site.jsonc. Add a shared club email address '
                 '(not a personal one) so the contact links have somewhere to go.\n')
    q = f"?subject={esc(subject)}" if subject else ""
    return f'<a href="mailto:{esc(email)}{q}">{esc(email)}</a>'


def hex_rgb(color):
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", c):
        sys.exit(f'\nTheme color "{color}" must be a hex color like "#102040".\n')
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def luminance(rgb):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = luminance(hex_rgb(a)), luminance(hex_rgb(b))
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def darken(color, f=0.72):
    return "#%02x%02x%02x" % tuple(int(v * f) for v in hex_rgb(color))


MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHS_FULL = ["January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAYS_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th", -1: "last"}


def fmt_date(d, style):
    if style == "intl":
        return f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} {d.year}"
    return f"{DAYS[d.weekday()]}, {MONTHS[d.month - 1]} {d.day}, {d.year}"


def fmt_day(d, style):
    """Short date for the date block: "October 18" or "18 October"."""
    return f"{d.day} {MONTHS_FULL[d.month - 1]}" if style == "intl" else f"{MONTHS_FULL[d.month - 1]} {d.day}"


def parse_time(t, what):
    if not t:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(t).strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        warnings.append(f'{what} "{t}" should be a 24-hour time like "13:00". Ignored.')
        return None
    return dt.time(int(m.group(1)), int(m.group(2)))


def fmt_time(t, style, meridiem=True):
    if style == "intl":
        return f"{t.hour:02d}:{t.minute:02d}"
    h = t.hour % 12 or 12
    s = f"{h}:{t.minute:02d}" if t.minute else str(h)
    return f"{s} {'AM' if t.hour < 12 else 'PM'}" if meridiem else s


def fmt_time_range(start, end, style):
    if not start:
        return ""
    if not end:
        return fmt_time(start, style)
    if style == "intl":
        return f"{fmt_time(start, style)}–{fmt_time(end, style)}"
    if (start.hour < 12) == (end.hour < 12):
        return f"{fmt_time(start, style, meridiem=False)}–{fmt_time(end, style)}"
    return f"{fmt_time(start, style)} – {fmt_time(end, style)}"



def image_size(path):
    """Width/height of PNG, GIF, JPEG, or WebP files without any libraries."""
    data = path.read_bytes()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", data[6:10])
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8X":
            return (int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1)
        if kind == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if kind == b"VP8L":
            b = int.from_bytes(data[21:25], "little")
            return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
    if data[:2] == b"\xff\xd8":
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


def png(width, height, pixel):
    """Tiny PNG writer: pixel(x, y) -> (r, g, b)."""
    rows = b"".join(b"\x00" + bytes(c for x in range(width) for c in pixel(x, y)) for y in range(height))
    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def yoyo_png(size_w, size_h, primary, accent, background):
    """A flat yo-yo on a string (accent rim, primary body, hub) for social previews and app icons."""
    p, a, bg = hex_rgb(primary), hex_rgb(accent), hex_rgb(background)
    wide = size_w > size_h
    cx, cy = (size_w * 0.70, size_h * 0.56) if wide else (size_w / 2, size_h * 0.56)
    r = min(size_w, size_h) * (0.30 if wide else 0.36)
    string_w = max(2, int(r * 0.05))
    stripe = size_h - max(8, size_h // 30)
    def pixel(x, y):
        if wide and y >= stripe:
            return a
        d2 = (x - cx) ** 2 + (y - cy) ** 2
        if d2 <= (r * 0.16) ** 2:
            return a
        if d2 <= (r * 0.78) ** 2:
            return p
        if d2 <= r * r:
            return a
        if abs(x - cx) <= string_w and y < cy:
            return p
        return bg
    return png(size_w, size_h, pixel)


def star_points(cx, cy, outer, inner):
    return tuple((round(cx + (outer if i % 2 == 0 else inner) * math.sin(math.pi * i / 5), 2),
                  round(cy - (outer if i % 2 == 0 else inner) * math.cos(math.pi * i / 5), 2)) for i in range(10))


# Toy silhouettes for the generated logo and icons, drawn in a 100x100 box. Pick one with
# theme.emblem: "yoyo", "kendama", "top", "diabolo", "juggling", or "star" (any skill toy).
# Each part is (shape, numbers, hole); holes are cut out in the background color.
TOYS = {
    "kendama": [("circle", (50, 23, 21), False), ("circle", (50, 23, 4), True),
                ("poly", ((46, 48), (50, 40), (54, 48)), False),
                ("rect", (20, 48, 60, 13), False), ("rect", (42, 61, 16, 36), False)],
    "top": [("rect", (44, 3, 12, 22), False), ("poly", ((10, 25), (90, 25), (90, 38), (50, 97), (10, 38)), False),
            ("rect", (10, 31, 80, 4), True)],
    "diabolo": [("poly", ((3, 18), (43, 42), (43, 58), (3, 82)), False),
                ("poly", ((97, 18), (57, 42), (57, 58), (97, 82)), False),
                ("rect", (38, 45, 24, 10), False), ("rect", (9, 22, 4, 56), True), ("rect", (87, 22, 4, 56), True)],
    "juggling": [("circle", (50, 24, 18), False), ("circle", (24, 70, 18), False), ("circle", (76, 70, 18), False)],
    "star": [("poly", star_points(50, 53, 48, 20), False)],
}
EMBLEMS = ["yoyo"] + list(TOYS)


def in_poly(x, y, pts):
    inside = False
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def toy_part_at(toy, u, v):
    """What covers point (u, v) of a toy's 100x100 box: "fg", "hole", or None."""
    if not (0 <= u <= 100 and 0 <= v <= 100):
        return None
    for shape, n, hole in reversed(TOYS[toy]):
        if shape == "circle":
            hit = (u - n[0]) ** 2 + (v - n[1]) ** 2 <= n[2] ** 2
        elif shape == "rect":
            hit = n[0] <= u <= n[0] + n[2] and n[1] <= v <= n[1] + n[3]
        else:
            hit = in_poly(u, v, list(n))
        if hit:
            return "hole" if hole else "fg"
    return None


def toy_svg(toy, fill, hole_fill):
    out = []
    for shape, n, hole in TOYS[toy]:
        f = hole_fill if hole else fill
        if shape == "circle":
            out.append(f'<circle cx="{n[0]}" cy="{n[1]}" r="{n[2]}" fill="{f}"/>')
        elif shape == "rect":
            out.append(f'<rect x="{n[0]}" y="{n[1]}" width="{n[2]}" height="{n[3]}" fill="{f}"/>')
        else:
            points = " ".join(f"{x:g},{y:g}" for x, y in n)
            out.append(f'<polygon points="{points}" fill="{f}"/>')
    return "".join(out)


def toy_png(toy, size_w, size_h, primary, accent, background):
    """The emblem's toy for social previews and app icons: the classic yo-yo, or a flat silhouette."""
    if toy not in TOYS:
        return yoyo_png(size_w, size_h, primary, accent, background)
    p, a, bg = hex_rgb(primary), hex_rgb(accent), hex_rgb(background)
    wide = size_w > size_h
    cx, cy = (size_w * 0.70, size_h * 0.52) if wide else (size_w / 2, size_h / 2)
    box = min(size_w, size_h) * (0.62 if wide else 0.70)
    x0, y0, scale = cx - box / 2, cy - box / 2, box / 100
    stripe = size_h - max(8, size_h // 30)
    def pixel(x, y):
        if wide and y >= stripe:
            return a
        return p if toy_part_at(toy, (x - x0) / scale, (y - y0) / scale) == "fg" else bg
    return png(size_w, size_h, pixel)


# ---------------------------------------------------------------- page building


def long_date(d, style):
    if style == "intl":
        return f"{DAYS_FULL[d.weekday()]} {d.day} {MONTHS_FULL[d.month - 1]} {d.year}"
    return f"{DAYS_FULL[d.weekday()]}, {MONTHS_FULL[d.month - 1]} {d.day}, {d.year}"


def readable_on(bg, color, f=0.85):
    """Darken a color until it reads on bg (4.5:1), for labels in the accent color."""
    for _ in range(12):
        if contrast(color, bg) >= 4.5:
            return color
        color = darken(color, f)
    return color


def parse_date(value, what, required=False):
    if not value:
        if required:
            sys.exit(f'\n{what} is missing. Use a date like "2026-09-19".\n')
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        sys.exit(f'\n{what} "{value}" should be a date like "2026-09-19".\n')


# ---------------------------------------------------------------- page building

class Site:
    def __init__(self, cfg, base_url):
        self.cfg = cfg
        self.c = cfg["contest"]
        self.base_url = base_url                                   # "" when unknown
        self.base_path = urlparse(base_url).path or "/" if base_url else "/"
        self.theme = cfg["theme"]
        self.name = self.c["name"]
        self.style = cfg["site"].get("date_format", "us")
        self.tz = None
        tzname = cfg["site"].get("timezone")
        if tzname:
            try:
                from zoneinfo import ZoneInfo
                self.tz = ZoneInfo(tzname)
            except Exception:
                warnings.append(f'Time zone "{tzname}" not found; using the computer\'s clock. '
                                'Use a name like "America/New_York".')
        self.today = dt.datetime.now(self.tz).date()
        self.date = parse_date(self.c.get("date"), "contest.date", required=True)
        self.end_date = parse_date(self.c.get("end_date"), "contest.end_date") or self.date
        if self.end_date < self.date:
            sys.exit("\ncontest.end_date is before contest.date.\n")
        reg = cfg.get("registration") or {}
        self.reg_opens = parse_date(reg.get("opens"), "registration.opens")
        self.reg_closes = parse_date(reg.get("closes"), "registration.closes")
        self.start, self.end = parse_time(self.c.get("start"), "contest.start"), parse_time(self.c.get("end"), "contest.end")
        self.pages = [("index", "Home"), ("schedule", "Schedule"), ("register", "Register"), ("rules", "Rules"),
                      ("venue", "Venue"), ("sponsors", "Sponsors"), ("results", "Results"), ("faq", "FAQ")]
        self.footer_pages = self.pages + [("terms", "Terms")]

    # --- where are we in the contest's life?
    def phase(self):
        """One of: cancelled, postponed, before, open, closed, today, past."""
        status = (self.c.get("status") or "scheduled").lower()
        if status in ("cancelled", "postponed"):
            return status
        if self.today > self.end_date:
            return "past"
        if self.date <= self.today <= self.end_date:
            return "today"
        reg = self.cfg.get("registration") or {}
        if not reg.get("url") and not self.reg_opens:
            return "closed" if reg.get("closed") else "before"
        if self.reg_opens and self.today < self.reg_opens:
            return "before"
        if (self.reg_closes and self.today > self.reg_closes) or reg.get("closed"):
            return "closed"
        return "open"

    def days_to_go(self):
        return (self.date - self.today).days

    def when(self, long=False):
        f = long_date if long else fmt_date
        if self.end_date != self.date:
            return f"{f(self.date, self.style)} – {f(self.end_date, self.style)}"
        return f(self.date, self.style)

    def hours(self):
        return fmt_time_range(self.start, self.end, self.style)

    def year(self):
        return str(self.c.get("year") or self.date.year)

    def status_line(self):
        """Short status text for the top bar and the home page banner."""
        p, reg = self.phase(), self.cfg.get("registration") or {}
        days = self.days_to_go()
        countdown = "Tomorrow!" if days == 1 else f"{days} days to go"
        if p == "cancelled":
            return "This contest has been cancelled."
        if p == "postponed":
            return "This contest has been postponed. New date coming soon."
        if p == "past":
            return "That's a wrap. Thank you!"
        if p == "today":
            return f"Today! {self.hours()}".strip() if self.hours() else "Today!"
        if p == "open":
            closes = f" · closes {fmt_date(self.reg_closes, self.style)}" if self.reg_closes else ""
            return f"Registration open{closes} · {countdown}"
        if p == "before":
            opens = f"Registration opens {fmt_date(self.reg_opens, self.style)}" if self.reg_opens else "Registration opens soon"
            return f"{opens} · {countdown}"
        return f"Registration closed · {countdown}"

    # --- shared text
    def fill(self, text):
        c, reg = self.c, self.cfg.get("registration") or {}
        org = (c.get("organizer") or {}).get("name") or self.name
        values = {"name": self.name, "short": c.get("short_name") or self.name, "year": self.year(),
                  "date": self.when(long=True), "hours": self.hours() or "all day",
                  "venue": (self.cfg.get("venue") or {}).get("name", ""), "city": c.get("city", ""),
                  "place": self.place(), "admission": c.get("admission", ""), "organizer": org,
                  "email": self.cfg["contact"]["email"],
                  "music_deadline": fmt_date(parse_date((reg.get("music") or {}).get("deadline"), "registration.music.deadline"), self.style)
                  if (reg.get("music") or {}).get("deadline") else "the music deadline",
                  "closes": fmt_date(self.reg_closes, self.style) if self.reg_closes else "the deadline",
                  "rules_name": self.cfg["rules"].get("ruleset_name", "our rules"),
                  "divisions": ", ".join(d["name"] for d in self.divisions() if d.get("name")),
                  "division_count": str(len(self.divisions()))}
        return re.sub(r"\{(\w+)\}", lambda mt: values.get(mt.group(1), mt.group(0)), text)

    def place(self):
        return ", ".join(x for x in (self.c.get("city"), self.c.get("region")) if x)

    def url(self, page):
        if not self.base_url:
            return ""
        return self.base_url if page == "index" else f"{self.base_url}{page}.html"

    def csp(self):
        return ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "font-src 'self'; frame-src 'none'; connect-src 'self'; base-uri 'self'; "
                "form-action 'none'; object-src 'none'; upgrade-insecure-requests")

    def nav(self, current, pages, root):
        items = []
        for slug, label in pages:
            cur = ' aria-current="page"' if slug == current else ""
            items.append(f'<li><a href="{root}{slug}.html"{cur}>{esc(label)}</a></li>')
        return "\n        ".join(items)

    def top_bar(self, root):
        p = self.phase()
        target, label = {"open": ("register", "Register"), "past": ("results", "Results"),
                         "today": ("schedule", "Schedule")}.get(p, ("index", "Details"))
        venue = (self.cfg.get("venue") or {}).get("name", "")
        bits = " · ".join(x for x in (self.when(), venue) if x)
        return (f'<div class="top-bar"><div class="top-bar-inner"><p><strong>{esc(self.c.get("short_name") or self.name)}</strong> '
                f'{esc(bits)} <span class="top-status">{esc(self.status_line())}</span></p>'
                f'<a href="{root}{target}.html">{esc(label)}</a></div></div>')

    def socials(self):
        return [s for s in self.cfg["contact"].get("social") or [] if s.get("url")]

    def footer(self, current, root):
        c = self.cfg
        org = self.c.get("organizer") or {}
        org_html = ""
        if org.get("name"):
            org_html = "<p>Organized by " + (ext_link(org["url"], org["name"]) if org.get("url") else esc(org["name"])) + "</p>"
        socials = " &bull; ".join(ext_link(s["url"], s["name"]) for s in self.socials())
        source = c["site"].get("source_url")
        source_html = (f'<p class="footer-source"><a href="{esc(source)}" rel="noopener noreferrer">'
                       f'Website source code</a></p>') if source else ""
        credit_html = ('<p class="footer-credit">Site template by Brandon Rogers &amp; '
                       '<a href="https://dmvthrowers.club/" rel="noopener noreferrer">DMV Throwers</a></p>'
                       ) if c["site"].get("credit", True) else ""
        return f"""<footer class="site-footer">
  <div class="wrap">
    <p class="footer-name">{esc(self.name)} {esc(self.year())}</p>
    <p class="footer-when">{esc(" · ".join(x for x in (self.when(), (c.get("venue") or {}).get("name", ""), self.place()) if x))}</p>
    <nav class="footer-nav" aria-label="Footer navigation">
      <ul>
        {self.nav(current, self.footer_pages, root)}
      </ul>
    </nav>
    <p>{mailto(c["contact"]["email"])}</p>
    {f'<p>{socials}</p>' if socials else ''}
    {org_html}
    {source_html}
    <p class="footer-note">&copy; {self.today.year} {esc((org.get("name") or self.name))}</p>
    {credit_html}
  </div>
</footer>"""

    def layout(self, slug, title, description, body, jsonld=None, robots="index, follow"):
        root = self.base_path if slug == "404" else ""
        full = f"{self.name} {self.year()}"
        page_title = full if slug == "index" else f"{title} — {full}"
        head_urls = []
        if self.base_url:
            if slug != "404":
                head_urls.append(f'<link rel="canonical" href="{esc(self.url(slug))}">')
                head_urls.append(f'<meta property="og:url" content="{esc(self.url(slug))}">')
            head_urls.append(f'<meta property="og:image" content="{esc(self.base_url)}og-card.png">')
            head_urls.append('<meta property="og:image:width" content="1200">')
            head_urls.append('<meta property="og:image:height" content="630">')
        ld = []
        if self.base_url and slug not in ("index", "404"):
            ld.append({"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Home", "item": self.url("index")},
                {"@type": "ListItem", "position": 2, "name": title, "item": self.url(slug)}]})
        if isinstance(jsonld, list):
            ld.extend(jsonld)
        elif jsonld:
            ld.append(jsonld)
        ld_html = ""
        if ld:
            payload = json.dumps(ld if len(ld) > 1 else ld[0], indent=2, ensure_ascii=False).replace("</", "<\\/")
            ld_html = f'\n  <script type="application/ld+json">\n{payload}\n  </script>'
        sub = " · ".join(x for x in (self.when(), self.place()) if x)
        page = f"""<!DOCTYPE html>
<html lang="{esc(self.cfg['site'].get('language') or 'en')}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(page_title)}</title>
  <meta name="description" content="{esc(description)}">
  <meta name="robots" content="{robots}">
  <meta name="referrer" content="strict-origin-when-cross-origin">
  <meta name="theme-color" content="{esc(self.theme['primary'])}">
  <meta http-equiv="Content-Security-Policy" content="{self.csp()}">
  {chr(10).join('  ' + h for h in head_urls).strip()}
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="{esc(full)}">
  <meta property="og:title" content="{esc(page_title)}">
  <meta property="og:description" content="{esc(description)}">
  <meta name="twitter:card" content="summary_large_image">
  <link rel="icon" href="{root}favicon.svg" type="image/svg+xml">
  <link rel="apple-touch-icon" href="{root}apple-touch-icon.png">
  <link rel="manifest" href="{root}site.webmanifest">
  <link rel="stylesheet" href="{root}theme.css">
  <link rel="stylesheet" href="{root}style.css">{ld_html}
</head>
<body>
<a class="skip-link" href="#main-content">Skip to main content</a>
<header class="site-header">
  {self.top_bar(root)}
  <div class="header-inner">
    <a class="brand" href="{root}index.html">
      <img class="brand-mark" src="{root}emblem.svg" alt="" width="44" height="44">
      <span class="brand-text"><span class="brand-name">{esc(self.c.get("short_name") or self.name)}</span><span class="brand-sub">{esc(sub)}</span></span>
    </a>
    <button class="nav-toggle" type="button" aria-label="Menu" aria-expanded="false" aria-controls="site-nav">&#9776;</button>
  </div>
  <nav class="site-nav" id="site-nav" aria-label="Main navigation">
    <ul>
      {self.nav(slug, self.pages, root)}
    </ul>
  </nav>
</header>
<main id="main-content">
{body}{self.extra_content(slug)}
</main>
{self.footer(slug, root)}
<script src="{root}site.js" defer></script>
</body>
</html>
"""
        return re.sub(r"\n\s*\n(\s*<meta property=\"og:type\")", r"\n\1", page)

    def extra_content(self, slug):
        """Optional hand-written HTML in content/<page>.html, added at the end of that page."""
        f = ROOT / "content" / f"{slug}.html"
        if not f.exists():
            return ""
        return f'\n<section class="section section-extra">\n  <div class="wrap">\n{f.read_text(encoding="utf-8")}\n  </div>\n</section>'

    def page_head(self, eyebrow, title, lede=None):
        lede = lede if lede is not None else " · ".join(x for x in (f"{self.name} {self.year()}", self.when(), self.place()) if x)
        return f"""<section class="page-head">
  <div class="wrap">
    <p class="eyebrow">{esc(eyebrow)}</p>
    <h1>{esc(title)}</h1>
    <p>{esc(lede)}</p>
  </div>
</section>"""

    def section(self, eyebrow, title, inner, alt=False, narrow=False, center=True):
        c = " center" if center else ""
        return f"""
<section class="section{' section-alt' if alt else ''}">
  <div class="wrap{' narrow' if narrow else ''}">
    {f'<p class="eyebrow{c}">{esc(eyebrow)}</p>' if eyebrow else ''}
    <h2 class="{c.strip()}">{esc(title)}</h2>
    {inner}
  </div>
</section>"""

    def bullets(self, items, cls="checklist"):
        return f'<ul class="{cls}">' + "".join(f"<li>{esc(self.fill(i))}</li>" for i in items) + "</ul>"

    def rule_blocks(self, blocks):
        out = []
        for b in blocks:
            out.append(f'<div class="rule-block"><h3>{esc(b["title"])}</h3>{self.bullets(b.get("items") or [])}</div>')
        return '<div class="rule-blocks">' + "".join(out) + "</div>"

    def link_list(self, links, cls="btn btn-outline"):
        items = "".join(f"<li>{ext_link(l['url'], l.get('label') or l['title'], cls)}</li>" for l in links if l.get("url"))
        return f'<ul class="btn-list">{items}</ul>' if items else ""

    def stats_html(self, stats):
        if not stats:
            return ""
        items = "".join(f'<li><strong>{esc(s["value"])}</strong><span>{esc(s["label"])}</span></li>' for s in stats)
        return f'<section class="stats" aria-label="By the numbers"><div class="wrap"><ul>{items}</ul></div></section>'

    def register_button(self, cls="btn btn-accent"):
        reg = self.cfg.get("registration") or {}
        if self.phase() == "open" and reg.get("url"):
            return ext_link(reg["url"], reg.get("label") or "Register now", cls)
        return ""

    def photos(self, items):
        figs = []
        for ph in items:
            src = ROOT / "assets" / ph["src"]
            if not src.exists():
                warnings.append(f'Photo not found: assets/{ph["src"]}')
                continue
            size = image_size(src)
            if not size:
                warnings.append(f'Could not read the size of {ph["src"]}; use a JPG, PNG, GIF, or WebP file.')
                continue
            if src.stat().st_size > 500_000:
                warnings.append(f'{ph["src"]} is {src.stat().st_size // 1024} KB; resize it under 500 KB so pages load fast.')
            if not ph.get("alt"):
                warnings.append(f'{ph["src"]} has no "alt" description; screen-reader users need one.')
            cap = f'<figcaption>{esc(ph["caption"])}</figcaption>' if ph.get("caption") else ""
            figs.append(f'<figure><img src="{esc(ph["src"])}" alt="{esc(ph.get("alt", ""))}" width="{size[0]}" '
                        f'height="{size[1]}" loading="lazy" decoding="async">{cap}</figure>')
        return ('<div class="gallery-grid">\n' + "\n".join(figs) + "\n</div>") if figs else ""

    def event_jsonld(self):
        venue = self.cfg.get("venue") or {}
        status = {"cancelled": "EventCancelled", "postponed": "EventPostponed"}.get(
            (self.c.get("status") or "").lower(), "EventScheduled")
        def stamp(d, t):
            if not t:
                return d.isoformat()
            return dt.datetime.combine(d, t, tzinfo=self.tz).isoformat() if self.tz else dt.datetime.combine(d, t).isoformat()
        ev = {"@context": "https://schema.org", "@type": "SportsEvent", "name": f"{self.name} {self.year()}",
              "description": self.c.get("description", ""), "startDate": stamp(self.date, self.start),
              "endDate": stamp(self.end_date, self.end), "eventStatus": f"https://schema.org/{status}",
              "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
              "sport": self.cfg["rules"].get("sport") or "Skill toys",
              "location": {"@type": "Place", "name": venue.get("name") or self.place(),
                           "address": venue.get("address") or self.place()}}
        org = self.c.get("organizer") or {}
        if org.get("name"):
            ev["organizer"] = {"@type": "Organization", "name": org["name"]}
            if org.get("url"):
                ev["organizer"]["url"] = org["url"]
        if self.c.get("free_to_watch"):
            ev["isAccessibleForFree"] = True
        sponsors = [s for s in self.cfg.get("sponsors") or [] if s.get("name")]
        if sponsors:
            ev["sponsor"] = [{"@type": "Organization", "name": s["name"], **({"url": s["url"]} if s.get("url") else {})}
                             for s in sponsors]
        if self.base_url:
            ev["url"] = self.base_url
            ev["image"] = self.base_url + "og-card.png"
        return ev

    # ------------------------------------------------------------ pages (one function each)

    def page_index(self):
        c, cfg = self.c, self.cfg
        p = self.phase()
        venue = cfg.get("venue") or {}
        eyebrow = " · ".join(x for x in (c.get("edition"), c.get("tradition")) if x)
        presented = c.get("presented_by") or {}
        presented_html = ""
        if presented.get("name"):
            nm = ext_link(presented["url"], presented["name"]) if presented.get("url") else esc(presented["name"])
            presented_html = f'<p class="presented">Brought to you by {nm}</p>'
        facts = [("Date", self.when(long=True)), ("Time", self.hours()),
                 ("Venue", " · ".join(x for x in (venue.get("name"), venue.get("space")) if x)),
                 ("Admission", c.get("admission", ""))]
        facts_html = "".join(f'<div><span class="label">{esc(k)}</span><p>{esc(v)}</p></div>' for k, v in facts if v)
        buttons = []
        if self.register_button():
            buttons.append(self.register_button())
        if p == "past":
            buttons.append('<a class="btn btn-accent" href="results.html">See the Results</a>')
        buttons.append('<a class="btn btn-ghost" href="schedule.html">Schedule</a>')
        for l in (c.get("hero_links") or []):
            if l.get("url"):
                buttons.append(ext_link(l["url"], l["label"], "btn btn-ghost"))
        wrap = cfg.get("wrap") or {}
        wrap_html = ""
        if p == "past" and wrap.get("stats"):
            wrap_html = (f'<section class="banner"><div class="wrap"><p>{esc(wrap.get("title") or "That’s a wrap · Thank you")}</p></div></section>'
                         + self.stats_html(wrap["stats"]))
        else:
            wrap_html = f'<section class="banner banner-{esc(p)}"><div class="wrap"><p>{esc(self.status_line())}</p></div></section>'
        about = "".join(f"<p>{esc(self.fill(x))}</p>" for x in c.get("about") or [])
        chips = "".join(f"<li>{esc(x)}</li>" for x in c.get("features") or [])
        chips_html = f'<ul class="chips" aria-label="Highlights">{chips}</ul>' if chips else ""
        links_html = ""
        if p == "past" and wrap.get("links"):
            links_html = self.link_list(wrap["links"])
        elif c.get("links"):
            links_html = self.link_list(c["links"])
        reg = cfg.get("registration") or {}
        fee_summary = reg.get("fee_summary", "")
        table = [("Date", self.when()), ("Hours", self.hours()), ("Venue", venue.get("name", "")),
                 ("Edition", c.get("edition", "")), ("Entry fee", fee_summary),
                 ("Spectators", c.get("admission", "")), ("Organizer", (c.get("organizer") or {}).get("name", ""))]
        table_html = "".join(f'<tr><th scope="row">{esc(k)}</th><td>{esc(v)}</td></tr>' for k, v in table if v)
        top = [s for s in cfg.get("sponsors") or [] if s.get("name")][:8]
        sponsor_html = ""
        if top:
            names = "".join(f'<li><span class="label">{esc(s.get("tier", ""))}</span>'
                            + (ext_link(s["url"], s["name"]) if s.get("url") else esc(s["name"])) + "</li>" for s in top)
            sponsor_html = self.section("Thank you", "Our Sponsors",
                                        f'<ul class="sponsor-strip">{names}</ul><p class="center"><a class="more" href="sponsors.html">All sponsors</a></p>', alt=True)
        body = f"""<section class="hero">
  <div class="wrap">
    {f'<p class="eyebrow">{esc(eyebrow)}</p>' if eyebrow else ''}
    <img class="hero-mark" src="emblem.svg" alt="{esc(self.name)} logo" width="120" height="120">
    <h1>{esc(self.name)}</h1>
    <p class="hero-year">{esc(self.year())}</p>
    {presented_html}
    <div class="hero-facts">{facts_html}</div>
    <div class="btn-row">{"".join(buttons)}</div>
  </div>
</section>
{wrap_html}
<section class="section">
  <div class="wrap narrow">
    <p class="eyebrow">About the event</p>
    <h2>{esc(self.fill(c.get("about_title") or "About the Contest"))}</h2>
    {about}
    {chips_html}
    {links_html}
  </div>
</section>

<section class="section section-alt">
  <div class="wrap narrow">
    <h2 class="center">Quick Facts</h2>
    <div class="table-wrap"><table class="facts-table"><tbody>{table_html}</tbody></table></div>
    <p class="center"><a class="btn btn-primary" href="register.html">Divisions &amp; Registration</a></p>
  </div>
</section>
{sponsor_html}"""
        return "Home", c.get("description", ""), body, self.event_jsonld()

    def page_schedule(self):
        cfg = self.cfg
        rows = []
        for item in cfg.get("schedule") or []:
            title, auto = self.schedule_round(item)
            link = f' {ext_link(item["url"], item.get("url_label") or "More", "more")}' if item.get("url") else ""
            words = self.fill(item.get("text", "")) or auto
            text = f'<p>{esc(words)}{link}</p>' if words or link else ""
            rows.append(f'<li><span class="slot-time">{esc(item.get("time", ""))}</span>'
                        f'<div class="slot-body"><h3>{esc(title)}</h3>{text}</div></li>')
        timeline = f'<ol class="timeline">{"".join(rows)}</ol>' if rows else \
            '<p class="muted center">The schedule will be posted closer to the contest.</p>'
        notes = "".join(f'<p class="note">{esc(self.fill(n))}</p>' for n in cfg.get("schedule_notes") or [])
        body = f"""{self.page_head("Day of event", "Schedule")}

<section class="section">
  <div class="wrap narrow">
    <h2>{esc(self.when(long=True))}</h2>
    {timeline}
    {notes}
  </div>
</section>"""
        return "Schedule", f"{self.name} {self.year()} schedule: {self.when()}.", body, None

    def schedule_round(self, item):
        """A schedule item's title, and default text for a division's round ("Top 10 advance to Finals.").
        Items can set "division" (a code) and "round" (one of its rounds) instead of a title."""
        d = next((x for x in self.divisions() if item.get("division") and x.get("code") == item["division"]), None)
        rnd = item.get("round")
        title = item.get("title") or " · ".join(x for x in ((d or {}).get("name"), rnd) if x)
        auto = ""
        rounds = rounds_of(d)
        names = [r["name"] for r in rounds]
        if rnd in names:
            i = names.index(rnd)
            n = positive_int(rounds[i].get("advance"))
            if n and i + 1 < len(rounds):
                auto = f"Top {n} advance to {rounds[i + 1]['name']}."
        return title, auto

    # --- divisions, fees, and music (all from the "divisions" list; see README "Divisions")
    def divisions(self):
        return [d for d in (self.cfg.get("divisions") or []) if isinstance(d, dict)]

    def check_divisions(self):
        seen = set()
        for i, d in enumerate(self.cfg.get("divisions") or [], 1):
            if not isinstance(d, dict) or not d.get("name"):
                sys.exit(f'\nDivision {i} in "divisions" needs a "name", like {{ "code": "1A", "name": "1A Division" }}.\n')
            code = d.get("code")
            if code and code in seen:
                warnings.append(f'Two divisions use the code "{code}". Give each division its own code.')
            seen.add(code)
            if d.get("ages") and not age_text(d["ages"]):
                warnings.append(f'Division "{d["name"]}": "ages" should be text like "Under 13", or {{ "min": 8, "max": 12 }}.')
            if d.get("music") not in (None, True, False, "house"):
                warnings.append(f'Division "{d["name"]}": "music" should be true, false, or "house". Ignored.')
            self.check_division_format(d)
        codes = {d.get("code") for d in self.divisions()}
        for f in (self.cfg.get("registration") or {}).get("fees") or []:
            if f.get("division") and f["division"] not in codes:
                warnings.append(f'registration.fees lists division "{f["division"]}", but no division has that code.')
        self.check_schedule_rounds()

    def check_division_format(self, d):
        """Warnings for the format fields (format, tricks, attempts, bracket, criteria, unit,
        better, team, rounds). Bad values are ignored, so the build still works."""
        def warn(msg):
            warnings.append(f'Division "{d["name"]}": {msg}')
        fmt = d.get("format")
        if fmt is not None and not isinstance(fmt, str):
            warn(f'"format" should be text, like one of: {", ".join(FORMATS)}. Ignored.')
        key = format_key(d)
        if "tricks" in d and tricks_of(d) is None:
            warn('"tricks" should be a list of trick names, like ["Big Cup", "Spike"]. Ignored.')
        elif tricks_of(d) and key != "ladder":
            warn(f'"tricks" are listed only for trick ladders, but "format" is "{fmt}". Ignored.')
        if "attempts" in d and positive_int(d["attempts"]) is None:
            warn('"attempts" should be a whole number like 3. Ignored.')
        b = d.get("bracket")
        if b is not None:
            if not isinstance(b, (dict, bool)):
                warn('"bracket" should be { "third_place": true, "match_format": "two 30-second rounds" }. Ignored.')
            elif isinstance(b, dict):
                if "third_place" in b and not isinstance(b["third_place"], bool):
                    warn('"bracket.third_place" should be true or false. Ignored.')
                if b.get("elimination") not in (None, "single", "double"):
                    warn('"bracket.elimination" should be "single" or "double". Using "single".')
                for k in ("match_format", "vote", "decided_by", "poll"):
                    if k in b and not isinstance(b[k], str) and not (k == "vote" and b[k] is False):
                        warn(f'"bracket.{k}" should be text. Ignored.')
                if "third_place_by_votes" in b and not isinstance(b["third_place_by_votes"], bool):
                    warn('"bracket.third_place_by_votes" should be true or false. Ignored.')
                if b.get("third_place") is True and b.get("third_place_by_votes") is True:
                    warn('"bracket" sets both "third_place" (a match) and "third_place_by_votes". Pick one; showing the match.')
                if b.get("third_place_by_votes") is True and bracket_decider(b) != "audience":
                    warn('"bracket.third_place_by_votes" needs "decided_by": "audience". Ignored.')
                if "stream_url" in b and not (isinstance(b["stream_url"], str) and b["stream_url"].startswith("https://")):
                    warn('"bracket.stream_url" should be a link starting with https://. Ignored.')
            if key != "bracket":
                warn(f'"bracket" is shown only for brackets, but "format" is "{fmt}". Ignored.')
        if "criteria" in d:
            crit = d["criteria"]
            if not isinstance(crit, list) or not all(isinstance(c, (dict, str)) for c in crit):
                warn('"criteria" should be a list like [{ "label": "Choreography", "points": 25 }]. Ignored.')
            else:
                for c in crit:
                    if isinstance(c, dict) and not c.get("label"):
                        warn('every item in "criteria" needs a "label". That item is ignored.')
                    if isinstance(c, dict) and "points" in c and number(c["points"]) is None:
                        warn(f'"points" for criterion "{c.get("label", "")}" should be a number like 25. Ignored.')
                if key == "showcase":
                    warn('showcase divisions are not judged, so "criteria" are ignored.')
                elif key not in ("panel", "freestyle"):
                    warn(f'"criteria" are shown only for judged routines (panel or freestyle), but "format" is "{fmt}". Ignored.')
        if "rules" in d and rules_of(d) is None:
            warn('"rules" should be a list of short sentences, like ["One-minute routines, head to head."]. Ignored.')
        if "unit" in d and not (isinstance(d["unit"], str) and d["unit"].strip()):
            warn('"unit" should be text like "seconds" or "catches". Ignored.')
        if "better" in d and d["better"] not in ("lower", "higher"):
            warn('"better" should be "lower" or "higher". Ignored.')
        if "team" in d:
            t = d["team"]
            if not isinstance(t, dict):
                warn('"team" should be { "label": "Doubles", "min": 2, "max": 2, "fee": "per pair" }. Ignored.')
            else:
                lo, hi = t.get("min"), t.get("max")
                if lo is not None and positive_int(lo) is None or hi is not None and positive_int(hi) is None:
                    warn('"team.min" and "team.max" should be whole numbers like 2. Ignored.')
                elif lo and hi and lo > hi:
                    warn(f'"team.min" ({lo}) is more than "team.max" ({hi}).')
                for k in ("label", "fee"):
                    if k in t and not isinstance(t[k], str):
                        warn(f'"team.{k}" should be text. Ignored.')
        if "rounds" in d:
            rounds = d["rounds"]
            if not isinstance(rounds, list) or not all(isinstance(r, dict) and r.get("name") for r in rounds):
                warn('"rounds" should be a list like [{ "name": "Prelims", "advance": 10 }, { "name": "Finals" }]. Ignored.')
            else:
                last = None
                for j, r in enumerate(rounds):
                    if "advance" not in r:
                        continue
                    n = positive_int(r["advance"])
                    if n is None:
                        warn(f'"advance" for round "{r["name"]}" should be a whole number like 10. Ignored.')
                    elif j == len(rounds) - 1:
                        warn(f'"{r["name"]}" is the last round, so no one advances from it. "advance" ignored.')
                    elif last is not None and n >= last:
                        warn(f'round "{r["name"]}" advances {n}, but the round before it advanced only {last}.')
                    else:
                        last = n

    def check_schedule_rounds(self):
        """Schedule items can name a division (by code) and one of its rounds; check both exist."""
        by_code = {d.get("code"): d for d in self.divisions() if d.get("code")}
        for item in self.cfg.get("schedule") or []:
            if not isinstance(item, dict) or not (item.get("division") or item.get("round")):
                continue
            what = f'Schedule item "{item.get("title") or item.get("time", "")}"'
            d = by_code.get(item.get("division"))
            if item.get("division") and not d:
                warnings.append(f'{what} names division "{item["division"]}", but no division has that code.')
                continue
            if item.get("round"):
                names = [r["name"] for r in rounds_of(d)] if d else \
                    [r["name"] for x in self.divisions() for r in rounds_of(x)]
                if item["round"] not in names:
                    whose = f'division "{d["name"]}" has' if d else "no division has a"
                    have = f' Its rounds are: {", ".join(names)}.' if d and names else ""
                    warnings.append(f'{what} names round "{item["round"]}", but {whose} {"no round" if d else "round"} by that name.{have}')

    # --- division formats: short phrases for the card, and details for the Rules page
    def team_text(self, d):
        """"Doubles · 2 players" from the "team" field."""
        t = team_of(d)
        if not t:
            return ""
        lo, hi = positive_int(t.get("min")), positive_int(t.get("max"))
        if lo and hi and lo <= hi:
            size = f"{lo} players" if lo == hi else f"{lo}–{hi} players"
        elif lo or hi:
            size = f"{lo}+ players" if lo else f"Up to {hi} players"
        else:
            size = ""
        return " · ".join(x for x in (t.get("label") if isinstance(t.get("label"), str) else "Team", size) if x)

    def rounds_text(self, d):
        """"Prelims → Finals (top 10 advance)" from the "rounds" field."""
        rounds = rounds_of(d)
        if not rounds:
            return ""
        adv = [positive_int(r.get("advance")) for r in rounds[:-1]]
        if all(adv):
            names = " → ".join(r["name"] for r in rounds)
            tops = ", then ".join(f"top {n}" for n in adv)
            return f"{names} ({tops} advance)" if tops else names
        return " → ".join(r["name"] + (f" (top {n} advance)" if n else "") for r, n in zip(rounds, adv + [None]))

    def format_bits(self, d):
        """Short facts about how the division runs, e.g. ["12 tricks", "3 tries per trick"]."""
        key, bits = format_key(d), []
        attempts = positive_int(d.get("attempts"))
        if key == "ladder":
            tricks = tricks_of(d) or []
            if tricks:
                bits.append(f"{len(tricks)} tricks")
            if attempts:
                bits.append(f"{attempts} {'try' if attempts == 1 else 'tries'} per trick")
        elif key == "bracket" and (isinstance(d.get("bracket"), dict) or d.get("bracket") is True):
            b = bracket_of(d)
            elim = "double" if b.get("elimination") == "double" else "single"
            bits.append(f"{elim} elimination battles")
            decider = bracket_decider(b)
            if decider:
                bits.append({"judges": "judges vote", "crowd": "crowd vote", "audience": "audience vote"}.get(decider, decider))
            if isinstance(b.get("match_format"), str) and b["match_format"]:
                bits.append(b["match_format"])
            if b.get("third_place") is True:
                bits.append("3rd-place match")
            elif b.get("third_place_by_votes") is True and decider == "audience":
                bits.append("3rd place by vote totals")
        elif key in ("timed", "scored"):
            unit = d.get("unit") if isinstance(d.get("unit"), str) else ""
            if unit.strip():
                bits.append(f"measured in {unit.strip()}")
            if d.get("better") in ("lower", "higher"):
                bits.append("lowest wins" if d["better"] == "lower" else "highest wins")
            if attempts:
                bits.append(f"best of {attempts} attempts" if attempts > 1 else "1 attempt")
        elif key == "showcase":
            bits.append("not judged")
        if key in ("panel", "freestyle"):
            crit = criteria_of(d)
            if crit:
                total = sum(number(c.get("points")) or 0 for c in crit)
                bits.append(f"{len(crit)} judging criteria" + (f" · {total:g} points" if total else ""))
        return bits

    def format_text(self, d):
        """The format's explanation: the division's "format_text", or a short default for known formats."""
        if isinstance(d.get("format_text"), str):
            return self.fill(d["format_text"])
        return FORMATS.get(format_key(d), "")

    def has_format_details(self, d):
        """Does this division get its own block under "How Each Division Works" on the Rules page?"""
        key = format_key(d)
        return bool((key == "ladder" and tricks_of(d)) or (key in ("panel", "freestyle") and criteria_of(d))
                    or rounds_of(d) or team_of(d) or rules_of(d) or (key == "bracket" and isinstance(d.get("bracket"), dict))
                    or (key in ("timed", "scored") and (positive_int(d.get("attempts")) or d.get("unit") or d.get("better")))
                    or (key == "ladder" and positive_int(d.get("attempts"))))

    def format_anchor(self, d):
        return "format-" + re.sub(r"[^a-z0-9]+", "-", (d.get("code") or d["name"]).lower()).strip("-")

    def format_block(self, d):
        """One division's details on the Rules page: format, rounds, team size, trick list, criteria."""
        facts = []
        label = format_label(d)
        bits = self.format_bits(d)
        if label or bits:
            facts.append(" · ".join(x for x in [label] + bits if x))
        if rounds_of(d):
            facts.append("Rounds: " + self.rounds_text(d))
            for r in rounds_of(d):
                extra = " · ".join(x for x in (r.get("length"), self.fill(r.get("text", "")) if isinstance(r.get("text"), str) else "") if x)
                if extra:
                    facts.append(f'{r["name"]}: {extra}')
        if team_of(d):
            fee = team_of(d).get("fee")
            facts.append(self.team_text(d) + (f" · entry fee {fee}" if isinstance(fee, str) and fee else ""))
        if d.get("length") and not any(r.get("length") for r in rounds_of(d)):
            facts.append(d["length"])
        b = bracket_of(d) if format_key(d) == "bracket" else {}
        watch = ""
        if bracket_decider(b) == "audience":
            stream = b.get("stream_url") if isinstance(b.get("stream_url"), str) and b["stream_url"].startswith("https://") else ""
            poll = b.get("poll") if isinstance(b.get("poll"), str) else ""
            how = f"{poll} on the stream" if poll and stream else (poll or ("live poll on the stream" if stream else ""))
            facts.append("Winners picked by the audience" + (f": {how}" if how else ""))
            if stream:
                watch = f'<p>{ext_link(stream, "Watch the stream", "more")}</p>'
        facts += [self.fill(r) for r in rules_of(d) or []]
        explain = self.format_text(d)
        out = [f'<div class="rule-block" id="{self.format_anchor(d)}"><h3>{esc(d["name"])}</h3>']
        if explain:
            out.append(f"<p>{esc(explain)}</p>")
        if facts:
            out.append('<ul class="checklist">' + "".join(f"<li>{esc(f)}</li>" for f in facts) + "</ul>")
        tricks = tricks_of(d) if format_key(d) == "ladder" else None
        if tricks:
            attempts = positive_int(d.get("attempts"))
            tries = f" · {attempts} {'try' if attempts == 1 else 'tries'} per trick" if attempts else ""
            out.append(f'<p class="label format-label">Trick list{esc(tries)}</p>'
                       '<ol class="trick-list">' + "".join(f"<li>{esc(t)}</li>" for t in tricks) + "</ol>")
        if watch:
            out.append(watch)
        crit = criteria_of(d) if format_key(d) in ("panel", "freestyle") else []
        if crit:
            has_points = any(number(c.get("points")) is not None for c in crit)
            head = '<th scope="col">Points</th>' if has_points else ""
            rows = "".join(f'<tr><th scope="row">{esc(c["label"])}</th>'
                           + (f'<td>{esc(fmt_points(c.get("points")))}</td>' if has_points else "") + "</tr>" for c in crit)
            total = sum(number(c.get("points")) or 0 for c in crit)
            foot = f'<tfoot><tr><th scope="row">Total</th><td>{total:g}</td></tr></tfoot>' if has_points and total else ""
            out.append(f'<div class="table-wrap"><table class="criteria"><caption>Judging criteria</caption>'
                       f'<thead><tr><th scope="col">Criterion</th>{head}</tr></thead><tbody>{rows}</tbody>{foot}</table></div>')
        out.append("</div>")
        return "".join(out)

    def division_fee(self, d):
        if d.get("fee"):
            return d["fee"]
        for f in (self.cfg.get("registration") or {}).get("fees") or []:
            if d.get("code") and f.get("division") == d["code"]:
                return f.get("fee", "")
        return ""

    def fee_rows(self):
        """(name, fee, note) rows for the fee table: registration.fees if set, otherwise each
        division's "fee", then combo deals, then spectators."""
        reg = self.cfg.get("registration") or {}
        by_code = {d.get("code"): d for d in self.divisions() if d.get("code")}
        explicit = [f for f in reg.get("fees") or [] if isinstance(f, dict)]
        def team_note(d, text=""):
            return " · ".join(x for x in (self.team_text(d) if d else "", text) if x)
        if explicit:
            rows = []
            for f in explicit:
                d = by_code.get(f.get("division")) if f.get("division") else None
                rows.append((f.get("name") or (d or {}).get("name") or f.get("division", ""),
                             self.team_fee(d, f.get("fee", "")) if d else f.get("fee", ""), team_note(d, f.get("text", ""))))
        else:
            rows = [(d["name"], self.team_fee(d, d["fee"]), team_note(d)) for d in self.divisions() if d.get("fee")]
        rows += [(c.get("name", ""), c.get("fee", ""), c.get("text", "")) for c in reg.get("combos") or [] if isinstance(c, dict)]
        if not explicit and reg.get("spectators"):
            rows.append(("Spectators", reg["spectators"], ""))
        return rows

    def team_fee(self, d, fee):
        """"$30" + team.fee "per pair" -> "$30 per pair"."""
        per = team_of(d or {}).get("fee")
        if fee and isinstance(per, str) and per and per.lower() not in fee.lower():
            return f"{fee} {per}"
        return fee

    def music_plan(self):
        """(show the music section?, names of the divisions that need music when only some do)."""
        music = (self.cfg.get("registration") or {}).get("music") or {}
        if not isinstance(music, dict) or not music.get("steps"):
            return False, []
        divs = self.divisions()
        if not any("music" in d for d in divs):
            return True, []                         # no division says either way: music for everyone
        own = [d for d in divs if d.get("music") is True]
        if not own:
            return False, []
        return True, ([d["name"] for d in own] if len(own) < len(divs) else [])

    def division_card(self, d):
        styles = [s for s in d.get("styles") or [] if s]
        mixed = any(x.get("music") is not True for x in self.divisions())     # only some divisions use your own music
        pills = [format_label(d), self.team_text(d)]
        if styles:
            pills.append(" · ".join(s if isinstance(s, str) else (s.get("code") or s.get("name", "")) for s in styles))
        pills += [age_text(d.get("ages")), *(d.get("tags") or []), d.get("length"),
                  "House music" if d.get("music") == "house" else ("Own music" if d.get("music") is True and mixed else "")]
        pills_html = "".join(f"<li>{esc(t)}</li>" for t in pills if t)
        style_items = "".join(f'<li><strong>{esc(s.get("code") or s.get("name", ""))}</strong> '
                              + esc(" · ".join(x for x in ((s.get("name") if s.get("code") else ""), self.fill(s.get("text", ""))) if x))
                              + "</li>" for s in styles if isinstance(s, dict) and (s.get("text") or (s.get("code") and s.get("name"))))
        fee = self.team_fee(d, self.division_fee(d))
        how = [" · ".join(self.format_bits(d)), self.rounds_text(d)]
        how[0] = how[0][:1].upper() + how[0][1:]
        how_html = "".join(f'<p class="division-format">{esc(x)}</p>' for x in how if x)
        if self.has_format_details(d):
            how_html += f'<p class="division-more"><a class="more" href="rules.html#{self.format_anchor(d)}">How it works</a></p>'
        return (f'\n  <div class="card division"><span class="division-code">{esc(d.get("code", ""))}</span>'
                f'<h3>{esc(d["name"])}</h3><p>{esc(self.fill(d.get("text") or d.get("description") or "") or self.format_text(d))}</p>'
                + (f'<ul class="style-list">{style_items}</ul>' if style_items else "") + how_html
                + (f'<ul class="pill-row">{pills_html}</ul>' if pills_html else "")
                + (f'<p class="division-fee"><span class="label">Entry fee</span>{esc(fee)}</p>' if fee else "") + "</div>")

    def page_register(self):
        cfg = self.cfg
        p = self.phase()
        reg = cfg.get("registration") or {}
        cards = [self.division_card(d) for d in self.divisions()]
        div_note = "".join(f'<p class="note">{esc(self.fill(n))}</p>' for n in cfg.get("divisions_notes") or [])
        divisions = (f'<div class="cards cards-3" id="divisions">{"".join(cards)}\n</div>{div_note}' if cards else
                     f'<p class="center muted" id="divisions">Divisions will be announced soon.</p>{div_note}')
        gear = cfg.get("gear") or {}
        gear_html = ""
        if isinstance(gear, dict) and gear.get("items"):
            intro = f'<p>{esc(self.fill(gear["intro"]))}</p>' if gear.get("intro") else ""
            gear_html = self.section("Competitors", gear.get("title") or "What to Bring",
                                     intro + self.bullets(gear["items"]), alt=True, narrow=True, center=False)
        guests = ""
        for g in cfg.get("guest_events") or []:
            link = f'<p>{ext_link(g["url"], g.get("url_label") or "Rules & sign-up", "btn btn-primary")}</p>' if g.get("url") else ""
            meta = " · ".join(x for x in (g.get("time"), g.get("host")) if x)
            guests += self.section("Guest event", f'{g["name"]} Signs Up Separately',
                                   f'<p class="center">{esc(meta)}</p><p>{esc(self.fill(g.get("text", "")))}</p><div class="center">{link}</div>',
                                   alt=True, narrow=True)
        state = {"open": ("Registration Is Open", reg.get("open_text", "Sign up online. Your spot is confirmed when payment goes through.")),
                 "before": ("Registration Opens Soon", (f"Registration opens {long_date(self.reg_opens, self.style)}." if self.reg_opens else "Registration opens soon.") + " Check back here or follow us for the announcement."),
                 "closed": ("Registration Is Closed", reg.get("closed_text", "Registration for this contest is closed. Spectators are always welcome.")),
                 "today": ("Contest Day", reg.get("today_text", "Online registration is closed. Ask at the registration desk about day-of entries.")),
                 "past": ("Registration Is Closed", reg.get("past_text", "Thank you to every competitor who took the stage. Registration for the next contest will open here.")),
                 "cancelled": ("Contest Cancelled", "This contest has been cancelled. Registered competitors will be contacted about refunds."),
                 "postponed": ("Contest Postponed", "This contest has been postponed. Registration details will be updated with the new date.")}[p]
        fees = "".join(f'<tr><th scope="row">{esc(name)}</th><td>{esc(fee)}'
                       + (f' <span class="fee-note">{esc(self.fill(note))}</span>' if note else "") + "</td></tr>"
                       for name, fee, note in self.fee_rows())
        fees_html = f'<div class="table-wrap"><table class="fees"><thead><tr><th scope="col">Entry</th><th scope="col">Fee</th></tr></thead><tbody>{fees}</tbody></table></div>' if fees else ""
        closes = f'<p class="center"><strong>Registration closes {esc(long_date(self.reg_closes, self.style))}.</strong></p>' if p == "open" and self.reg_closes else ""
        btn = self.register_button("btn btn-accent btn-big")
        payment = "".join(f"<p>{esc(self.fill(x))}</p>" for x in reg.get("payment") or [])
        music = reg.get("music") or {}
        music_html = ""
        show_music, only = self.music_plan()
        if show_music:
            steps = "".join(f'<li class="step"><p>{esc(self.fill(s))}</p></li>' for s in music["steps"])
            up = ext_link(music["upload_url"], music.get("upload_label") or "Upload your music", "btn btn-primary") \
                if music.get("upload_url") and p in ("open", "closed") else ""
            only_html = f'<p class="center intro">Music is needed only for: {esc(", ".join(only))}.</p>' if only else ""
            music_html = self.section("Competitors", music.get("title") or "Music Upload",
                                      f'{only_html}<ol class="steps">{steps}</ol><p class="center">{up}</p>', alt=True)
        body = f"""{self.page_head("Competition", "Divisions & Registration")}
{self.section("Competition", "Divisions", divisions)}
{gear_html}{guests}
<section class="section section-reg">
  <div class="wrap narrow">
    <p class="eyebrow center">Competitor registration</p>
    <h2 class="center">{esc(state[0])}</h2>
    <p class="center">{esc(self.fill(state[1]))}</p>
    {closes}
    <p class="center">{btn}</p>
    {fees_html}
    {payment}
    <p class="center"><a class="more" href="terms.html">Refund policy &amp; terms</a></p>
  </div>
</section>
{music_html}"""
        return "Register", f"Divisions, entry fees, and registration for {self.name} {self.year()}.", body, None

    def page_rules(self):
        cfg = self.cfg
        R = cfg["rules"]
        sources = " &middot; ".join(ext_link(s["url"], s["title"]) for s in R.get("sources") or [] if s.get("url"))
        src_html = f'<p class="sources">Sources: {sources}</p>' if sources else ""
        scoring = ""
        if R.get("scoring"):
            intro = f'<p class="intro">{esc(self.fill(R.get("scoring_intro", "")))}</p>' if R.get("scoring_intro") else ""
            notes = "".join(f'<p class="note">{esc(self.fill(n))}</p>' for n in R.get("scoring_notes") or [])
            scoring = self.section("Judging", R.get("scoring_title", "How Scoring Works"),
                                   intro + self.rule_blocks(R["scoring"]) + notes, alt=True, center=False)
        notes = "".join(f'<p class="note">{esc(self.fill(n))}</p>' for n in R.get("notes") or [])
        formats = ""
        detailed = [d for d in self.divisions() if self.has_format_details(d)]
        if detailed:
            formats = "\n" + self.section("Divisions", R.get("formats_title") or "How Each Division Works",
                                           '<div class="rule-blocks">' + "".join(self.format_block(d) for d in detailed) + "</div>",
                                           alt=not scoring, center=False)
        body = f"""{self.page_head("Contest rules", "Rules")}
{self.section("Contest rules", R.get("title", "Contest Ruleset"), (f'<p class="intro">{esc(self.fill(R.get("intro", "")))}</p>' if R.get("intro") else "") + self.rule_blocks(R.get("sections") or []) + notes + src_html, center=False)}
{scoring}{formats}"""
        return "Rules", f"Rules and judging for {self.name} {self.year()}.", body, None

    def page_venue(self):
        v = self.cfg.get("venue") or {}
        def rows(items):
            return "".join(f'<div class="fact-row"><span class="label">{esc(i["label"])}</span><p>{esc(self.fill(i["text"]))}</p></div>'
                           for i in items)
        facts = [{"label": "Address", "text": v.get("address", "")}] if v.get("address") else []
        if v.get("space"):
            facts.append({"label": "Event space", "text": v["space"]})
        facts.append({"label": "Date & time", "text": " · ".join(x for x in (self.when(long=True), self.hours()) if x)})
        facts += v.get("facts") or []
        links = []
        if v.get("map_url"):
            links.append(ext_link(v["map_url"], "Get directions", "btn btn-primary"))
        if v.get("website"):
            links.append(ext_link(v["website"], "Venue website", "btn btn-outline"))
        rules = self.section("Venue rules", "Good to Know", f'<div class="fact-list">{rows(v["rules"])}</div>', alt=True, narrow=True, center=False) if v.get("rules") else ""
        photos = self.photos(v.get("photos") or [])
        photos_html = self.section("Venue photos", v.get("name", "The Venue"), photos) if photos else ""
        hotels = []
        for h in v.get("hotels") or []:
            meta = "".join(f'<div class="fact-row"><span class="label">{esc(k)}</span><p>{esc(h[key])}</p></div>'
                           for k, key in (("Group rate", "rate"), ("Book by", "book_by"), ("Distance", "distance")) if h.get(key))
            book = f'<p>{ext_link(h["url"], h.get("url_label") or "Book your room", "btn btn-accent")}</p>' if h.get("url") else ""
            hotels.append(f'<div class="card"><h3>{esc(h["name"])}</h3><p>{esc(h.get("text", ""))}</p><div class="fact-list">{meta}</div>{book}</div>')
        hotels_html = self.section("Accommodations", "Where to Stay", f'<div class="cards cards-2">{"".join(hotels)}</div>') if hotels else ""
        body = f"""{self.page_head("Venue", v.get("name") or "Venue")}

<section class="section">
  <div class="wrap narrow">
    <div class="fact-list">{rows(facts)}</div>
    <p class="btn-row">{"".join(links)}</p>
  </div>
</section>
{rules}
{photos_html}
{hotels_html}"""
        return "Venue", f"Venue, parking, and hotels for {self.name} {self.year()}: {v.get('name', '')}.", body, None

    def page_sponsors(self):
        cfg = self.cfg
        p = self.phase()
        S = cfg.get("sponsorship") or {}
        cards = []
        for s in cfg.get("sponsors") or []:
            perks = self.bullets(s.get("perks") or []) if s.get("perks") else ""
            name = ext_link(s["url"], s["name"]) if s.get("url") else esc(s["name"])
            cards.append(f'\n  <div class="card sponsor"><span class="label">{esc(s.get("tier", ""))}</span><h3>{name}</h3>'
                         f'<p>{esc(s.get("text", ""))}</p>{perks}</div>')
        current = (f'<p class="center intro">{esc(self.fill(S.get("thanks", "Thank you to every sponsor and supporter. They keep entry fees low and the event free to watch.")))}</p>'
                   f'<div class="cards cards-3">{"".join(cards)}\n</div>') if cards else \
            '<p class="center muted">Sponsors will be announced soon. Want to be first? See below.</p>'
        partners = ""
        if cfg.get("partners"):
            items = "".join(f'<li><span class="label">{esc(x.get("tier", "Partner"))}</span>'
                            + (ext_link(x["url"], x["name"]) if x.get("url") else esc(x["name"]))
                            + (f' <span class="muted">{esc(x["text"])}</span>' if x.get("text") else "") + "</li>"
                            for x in cfg["partners"])
            partners = self.section("Partners & friends", "Partners & Friends",
                                    f'<p class="center intro">{esc(S.get("partners_intro", "Not sponsors. Partners help us put the contest on; friends are the clubs who show up for it."))}</p><ul class="plain-list partner-list">{items}</ul>',
                                    alt=True, narrow=True)
        tiers = []
        for t in S.get("tiers") or []:
            status = f'<p class="tier-status">{esc(t["status"])}</p>' if t.get("status") else ""
            tiers.append(f'\n  <div class="card tier"><span class="label">{esc(t.get("slots", ""))}</span><h3>{esc(t["name"])}</h3>'
                         f'<p class="tier-price">{esc(t.get("price", ""))}</p>{self.bullets(t.get("perks") or [])}{status}</div>')
        contact = S.get("contact_url")
        cta = ext_link(contact, S.get("contact_label") or "Sponsor inquiry", "btn btn-accent") if contact else \
            f'<a class="btn btn-accent" href="mailto:{esc(cfg["contact"]["email"])}?subject=Sponsorship">Email us about sponsoring</a>'
        goal = f'<p class="center"><strong>Funding goal: {esc(S["goal"])}</strong></p>' if S.get("goal") else ""
        become = self.section("Sponsorship", "Sponsor the Next Contest" if p == "past" else "Become a Sponsor",
                              f'<p class="center intro">{esc(self.fill(S.get("intro", "")))}</p>{goal}'
                              f'<div class="cards cards-3">{"".join(tiers)}\n</div><p class="center">{cta}</p>') if tiers or S.get("intro") else ""
        body = f"""{self.page_head("Our sponsors", "Sponsors")}
{self.section("Our sponsors", "Current Sponsors", current)}
{partners}
{become}"""
        return "Sponsors", f"Sponsors and sponsorship packages for {self.name} {self.year()}.", body, None

    def page_results(self):
        cfg = self.cfg
        R = cfg.get("results") or {}
        p = self.phase()
        blocks = []
        for d in R.get("divisions") or []:
            pod = []
            for r in d.get("podium") or []:
                meta = " · ".join(x for x in (r.get("from"), (f'{r["score"]} pts' if r.get("score") else "")) if x)
                link = f' {ext_link(r["url"], "Profile", "more")}' if r.get("url") else ""
                pod.append(f'<li class="place place-{esc(str(r.get("place", "")))}"><span class="place-label">{esc(r.get("label") or ordinal(r.get("place")))}</span>'
                           f'<strong>{esc(r["name"])}</strong><span class="muted">{esc(meta)}</span>{link}</li>')
            extra = "".join(f'<p class="note">{esc(n)}</p>' for n in d.get("notes") or [])
            video = f'<p>{ext_link(d["video_url"], d.get("video_label") or "Watch the runs", "more")}</p>' if d.get("video_url") else ""
            blocks.append(f'<div class="result-block"><p class="eyebrow">{esc(d.get("division", ""))}</p><h3>{esc(d.get("title", "Podium"))}</h3>'
                          f'<ol class="podium">{"".join(pod)}</ol>{extra}{video}</div>')
        if blocks:
            main = f'<div class="results-grid">{"".join(blocks)}</div>'
        elif p in ("past", "today"):
            main = '<p class="center">Results are being tallied. Check back soon.</p>'
        else:
            main = f'<p class="center">Results go up here after the contest on {esc(self.when(long=True))}.</p>'
        intro = f'<p class="center intro">{esc(self.fill(R["intro"]))}</p>' if R.get("intro") else ""
        privacy = f'<p class="note">{esc(R["privacy_note"])}</p>' if R.get("privacy_note") else ""
        stats = self.stats_html(R.get("stats") or [])
        links = self.link_list(R.get("links") or [])
        links_html = self.section(R.get("links_eyebrow") or "Everyone who competed", R.get("links_title") or "Standings, Photos & Video",
                                  links) if links else ""
        body = f"""{self.page_head("Results", "Results")}
{stats}
{self.section(f"{self.name} {self.year()}", "Champions", intro + main + privacy)}
{links_html}"""
        return "Results", f"Results and champions from {self.name} {self.year()}.", body, None

    def page_faq(self):
        cfg = self.cfg
        groups = {}
        for f in list(cfg.get("faq") or []) + list(cfg.get("faq_extra") or []):
            groups.setdefault(f.get("group") or "More Questions", []).append((self.fill(f["q"]), self.fill(f["a"])))
        blocks, qa_all = [], []
        for title, qa in groups.items():
            qa_all += qa
            items = "".join(f'\n  <details class="faq-item"><summary>{esc(q)}</summary><p>{esc(a)}</p></details>' for q, a in qa)
            blocks.append(f'<h2 class="faq-group">{esc(title)}</h2>\n    <div class="faq-list">{items}\n    </div>')
        ld = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in qa_all]}
        body = f"""{self.page_head("FAQ", "Common Questions")}

<section class="section">
  <div class="wrap narrow">
    {"".join(blocks)}
    <p class="center">Still wondering? Email {mailto(cfg["contact"]["email"])}.</p>
  </div>
</section>"""
        topics = "tickets, divisions, judging, music, parking" if self.music_plan()[0] else "tickets, divisions, judging, parking"
        return "FAQ", f"{self.name} {self.year()} FAQ: {topics}, and more.", body, ld

    def page_terms(self):
        cfg = self.cfg
        T = cfg["terms"]
        toc, secs = [], []
        for i, s in enumerate(T.get("sections") or [], 1):
            anchor = f"terms-{i}"
            toc.append(f'<li><a href="terms.html#{anchor}">{i}. {esc(s["title"])}</a></li>')
            paras = "".join(f"<p>{esc(self.fill(x))}</p>" for x in s.get("paragraphs") or [])
            items = self.bullets(s["items"], "") if s.get("items") else ""
            after = "".join(f"<p>{esc(self.fill(x))}</p>" for x in s.get("after") or [])
            secs.append(f'<h2 id="{anchor}">{i}. {esc(s["title"])}</h2>{paras}{items}{after}')
        eff = f'<p class="label">Effective {esc(T["effective"])}</p>' if T.get("effective") else ""
        body = f"""{self.page_head("Competitor terms", "Terms & Policies")}

<section class="section">
  <div class="wrap narrow prose">
    {eff}
    <p>{esc(self.fill(T.get("intro", "")))}</p>
    <nav class="toc" aria-label="On this page"><p class="label">On this page</p><ol>{"".join(toc)}</ol></nav>
    {"".join(secs)}
    <h2>Questions</h2>
    <p>Email {mailto(cfg["contact"]["email"], "Terms question")}.</p>
  </div>
</section>"""
        return "Terms", f"Refund policy, waiver, photo release, and conduct rules for {self.name} {self.year()}.", body, None

    def page_404(self):
        root = self.base_path
        body = f"""{self.page_head("404", "Page Not Found", "That page doesn't exist. It may have moved.")}

<section class="section">
  <div class="wrap center">
    <p class="btn-row"><a class="btn btn-primary" href="{root}index.html">Go to Home</a>
      <a class="btn btn-primary" href="{root}schedule.html">See the Schedule</a></p>
  </div>
</section>"""
        return "Page Not Found", f"Page not found — {self.name}.", body, None

    # ------------------------------------------------------------ generated files

    def theme_css(self):
        t = self.theme
        on_accent = "#111111" if contrast(t["accent"], "#111111") >= contrast(t["accent"], "#ffffff") else "#ffffff"
        accent_ink = readable_on(t["background"], t["accent"])
        checks = [("white text on your primary color", "#ffffff", t["primary"]),
                  ("text on your background color", t["ink"], t["background"]),
                  ("headings (primary color) on your background color", t["primary"], t["background"]),
                  ("accent color on your primary color", t["accent"], t["primary"]),
                  ("text on your accent color", on_accent, t["accent"])]
        for what, fg, bg in checks:
            ratio = contrast(fg, bg)
            if ratio < 4.5:
                warnings.append(f"Hard to read: {what} has contrast {ratio:.1f}:1 (needs 4.5:1). Try a darker or lighter color.")
        corners = {"sharp": "0", "soft": "6px", "round": "14px"}.get(t.get("corners", "sharp"), "0")
        return f"""/* Generated by build.py from your theme colors. Edit colors in site.jsonc, not here. */
:root {{
  --primary: {t["primary"]};
  --primary-dark: {darken(t["primary"])};
  --accent: {t["accent"]};
  --accent-ink: {accent_ink};
  --on-accent: {on_accent};
  --bg: {t["background"]};
  --ink: {t["ink"]};
  --radius: {corners};
}}
"""

    def emblem(self):
        """Which toy the generated logo shows (theme.emblem)."""
        toy = self.theme.get("emblem") or "star"
        if toy not in EMBLEMS:
            warnings.append(f'theme.emblem "{toy}" should be one of: {", ".join(EMBLEMS)}. Using "star".')
            toy = "star"
        return toy

    def emblem_svg(self):
        """The generated logo: a toy (theme.emblem) in the accent color, with the short name.
        Replace with assets/emblem.svg for your own logo."""
        t = self.theme
        label = (self.c.get("short_name") or "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", self.name)))
        label = re.sub(r"[^A-Za-z0-9]", "", label).upper()[:4]
        size = {1: 34, 2: 28, 3: 22, 4: 17}.get(len(label), 17)
        text = (f'<text x="50" y="72" text-anchor="middle" dominant-baseline="central" font-family="system-ui, -apple-system, '
                f'\'Segoe UI\', Roboto, sans-serif" font-weight="900" font-size="{size * 0.75:.0f}" fill="#ffffff">{esc(label)}</text>')
        toy = self.emblem()
        if toy == "yoyo":       # a yo-yo with a star, ringed in the accent color
            art = (f'<rect x="48.5" y="0" width="3" height="22" fill="{t["accent"]}"/>\n'
                   f'  <circle cx="50" cy="58" r="41" fill="{t["accent"]}"/>\n'
                   f'  <circle cx="50" cy="58" r="35" fill="{t["primary"]}"/>\n'
                   f'  <path d="M50 27 l4 9 10 1 -7.5 6.5 2.5 10 -9 -5.5 -9 5.5 2.5 -10 -7.5 -6.5 10 -1 z" fill="{t["accent"]}"/>')
        else:                   # a round badge with the toy above the short name
            art = (f'<circle cx="50" cy="50" r="48" fill="{t["accent"]}"/>\n'
                   f'  <circle cx="50" cy="50" r="42" fill="{t["primary"]}"/>\n'
                   f'  <g transform="translate(32 15) scale(0.36)">{toy_svg(toy, t["accent"], t["primary"])}</g>')
        return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100" role="img" aria-label="{esc(self.name)}">
  {art}
  {text}
</svg>
"""

    def build(self):
        self.check_divisions()
        if OUT.exists():
            shutil.rmtree(OUT)
        shutil.copytree(ROOT / "assets", OUT)
        (OUT / ".nojekyll").write_text("")
        (OUT / "theme.css").write_text(self.theme_css())
        if not (OUT / "emblem.svg").exists():                       # your own assets/emblem.svg wins
            (OUT / "emblem.svg").write_text(self.emblem_svg())
        if not (OUT / "favicon.svg").exists():
            shutil.copy(OUT / "emblem.svg", OUT / "favicon.svg")
        t = self.theme
        if not (OUT / "og-card.png").exists():                     # your own assets/og-card.png wins
            (OUT / "og-card.png").write_bytes(toy_png(self.emblem(), 1200, 630, t["accent"], t["primary"], t["primary"]))
        if not (OUT / "apple-touch-icon.png").exists():
            (OUT / "apple-touch-icon.png").write_bytes(toy_png(self.emblem(), 180, 180, t["accent"], t["primary"], t["primary"]))
        (OUT / "site.webmanifest").write_text(json.dumps({
            "name": f"{self.name} {self.year()}", "short_name": self.c.get("short_name") or self.name[:24],
            "start_url": "./", "display": "browser", "background_color": t["background"],
            "theme_color": t["primary"],
            "icons": [{"src": "apple-touch-icon.png", "sizes": "180x180", "type": "image/png"},
                      {"src": "emblem.svg", "sizes": "any", "type": "image/svg+xml"}]}, indent=2))
        for slug, _ in self.footer_pages + [("404", "")]:
            title, desc, body, ld = getattr(self, f"page_{slug}")()
            robots = "noindex, follow" if slug == "404" or getattr(self, "noindex", False) else "index, follow"
            (OUT / f"{slug}.html").write_text(self.layout(slug, title, desc, body, ld, robots), encoding="utf-8")
        robots = "User-agent: *\nAllow: /\n"
        if self.base_url:
            robots += f"\nSitemap: {self.base_url}sitemap.xml\n"
            urls = "".join(f"  <url><loc>{esc(self.url(s))}</loc><lastmod>{self.today.isoformat()}</lastmod></url>\n"
                           for s, _ in self.footer_pages)
            (OUT / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                                             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + urls + "</urlset>\n")
        else:
            warnings.append("No site URL set, so canonical links, social previews, and sitemap.xml were skipped. "
                            "(Automatic on GitHub Pages; or set site.url in site.jsonc.)")
        (OUT / "robots.txt").write_text(robots)
        # The base path lets scripts/check_site.py resolve 404.html's absolute links.
        if OUT == ROOT / "_site":
            (ROOT / ".build-base-path").write_text(self.base_path)


# Division formats (README "Division formats"). "format" can be any text; these known ones also
# get a default explanation, and their own fields (tricks, bracket, criteria, unit...) are shown.
FORMATS = {
    "freestyle": "A routine of your own tricks, judged as a whole.",
    "panel": "A panel of judges scores each routine on set criteria.",
    "timed": "The clock decides. Each attempt is timed, and your best one counts.",
    "scored": "A measured result decides the placing. Every attempt is measured, and your best one counts.",
    "ladder": "Called tricks from easier to harder. Land each one within your tries to move up the ladder.",
    "bracket": "Head-to-head battles. Win a battle to move on to the next round.",
    "showcase": "A performance for the crowd. Not judged and not ranked.",
}
FORMAT_LABELS = {"freestyle": "Freestyle", "panel": "Panel judged", "timed": "Timed", "scored": "Scored",
                 "ladder": "Trick ladder", "bracket": "Bracket", "showcase": "Showcase"}
FORMAT_NAMES = {"freestyle": "freestyle", "panel": "panel", "panel judged": "panel", "panel-judged": "panel",
                "judged": "panel", "timed": "timed", "scored": "scored", "manual": "scored", "ladder": "ladder",
                "trick ladder": "ladder", "trick-ladder": "ladder", "bracket": "bracket", "battle": "bracket",
                "battles": "bracket", "battle bracket": "bracket", "showcase": "showcase", "exhibition": "showcase"}


def format_key(d):
    """The known format a division uses ("ladder", "bracket"...), or None for free text.
    With no "format", it's taken from the fields: tricks -> ladder, bracket -> bracket, criteria -> panel."""
    fmt = d.get("format")
    if isinstance(fmt, str) and fmt.strip():
        return FORMAT_NAMES.get(fmt.strip().lower())
    if fmt is None:
        if tricks_of(d):
            return "ladder"
        if isinstance(d.get("bracket"), dict) or d.get("bracket") is True:
            return "bracket"
        if criteria_of(d):
            return "panel"
    return None


def format_label(d):
    """The format pill: what you wrote ("Trick ladder"), or a label for a bare key ("ladder")."""
    fmt, key = d.get("format"), format_key(d)
    if isinstance(fmt, str) and fmt.strip():
        return FORMAT_LABELS[key] if key and fmt == fmt.lower() else fmt
    return FORMAT_LABELS.get(key, "") if fmt is None else ""


def positive_int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def number(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def fmt_points(v):
    n = number(v)
    return f"{n:g}" if n is not None else ""


def tricks_of(d):
    t = d.get("tricks")
    if t is None:
        return []
    return t if isinstance(t, list) and all(isinstance(x, str) and x.strip() for x in t) else None


def bracket_of(d):
    b = d.get("bracket")
    return b if isinstance(b, dict) else {}


def bracket_decider(b):
    """Who picks each battle's winner: "judges" (default), "crowd", "audience", other text, or ""."""
    v = b.get("decided_by", b.get("vote", "judges"))
    return v.strip() if isinstance(v, str) else ""


def criteria_of(d):
    c = d.get("criteria")
    if not isinstance(c, list):
        return []
    return [x if isinstance(x, dict) else {"label": x} for x in c
            if (isinstance(x, dict) and x.get("label")) or (isinstance(x, str) and x)]


def team_of(d):
    return d["team"] if isinstance(d.get("team"), dict) else {}


def rounds_of(d):
    r = (d or {}).get("rounds")
    return r if isinstance(r, list) and all(isinstance(x, dict) and x.get("name") for x in r) else []


def rules_of(d):
    r = d.get("rules")
    if r is None:
        return []
    return r if isinstance(r, list) and all(isinstance(x, str) for x in r) else None


def age_text(ages):
    """Division age limits: text as-is ("Under 13"), or { "min": 8, "max": 12 } -> "Ages 8–12"."""
    if isinstance(ages, str):
        return ages
    if isinstance(ages, dict):
        lo, hi = ages.get("min"), ages.get("max")
        if lo and hi:
            return f"Ages {lo}–{hi}"
        if lo:
            return f"Ages {lo}+"
        if hi:
            return f"Ages {hi} & under"
    return ""


def ordinal(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n or "")
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.environ.get("SITE_URL", ""), help="public address, e.g. https://example.org/")
    ap.add_argument("--serve", action="store_true", help="preview at http://localhost:8000/ after building")
    ap.add_argument("--config", default="site.jsonc", help="settings file (default: site.jsonc)")
    ap.add_argument("--out", default="_site", help="output folder (default: _site)")
    ap.add_argument("--noindex", action="store_true", help="ask search engines not to list the site (demos)")
    ap.add_argument("--today", help="pretend today is YYYY-MM-DD (to preview before/during/after the contest)")
    args = ap.parse_args()
    global OUT
    OUT = (ROOT / args.out).resolve()
    cfg = load_config((ROOT / args.config).resolve())
    base_url = (args.base_url or cfg["site"].get("url") or "").strip()
    if base_url and not base_url.endswith("/"):
        base_url += "/"
    if base_url.startswith("http://"):
        # GitHub Pages reports http:// until "Enforce HTTPS" is on; the site is still served over HTTPS.
        base_url = "https://" + base_url[len("http://"):]
        warnings.append(f"Using {base_url} (https). On GitHub Pages, tick Settings > Pages > Enforce HTTPS.")
    if base_url and urlparse(base_url).scheme != "https":
        warnings.append(f"Site URL {base_url} should start with https://")
    site = Site(cfg, base_url)
    if args.today:
        site.today = dt.date.fromisoformat(args.today)
    site.noindex = args.noindex
    site.build()
    print(f"Built {cfg.get('preset_name', cfg.get('preset'))} site for {site.name} {site.year()} into {OUT.relative_to(ROOT)}/"
          + (f" (address: {base_url})" if base_url else "")
          + f"\n  Status: {site.status_line()}")
    for w in dict.fromkeys(warnings):          # each warning once, in order
        print("  WARNING:", w)
    if args.serve:
        os.chdir(OUT)
        print("Preview: http://localhost:8000/   (Ctrl+C to stop)")
        http.server.ThreadingHTTPServer(("127.0.0.1", 8000), http.server.SimpleHTTPRequestHandler).serve_forever()


if __name__ == "__main__":
    main()
