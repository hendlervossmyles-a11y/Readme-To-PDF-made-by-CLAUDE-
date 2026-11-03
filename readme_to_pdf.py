#!/usr/bin/env python3
"""
readme_to_pdf.py - Convert a README.md into a PDF, screenshots included.

Image paths in the README are resolved relative to the README's own folder
(and, as a fallback, the repo root), so the script works anywhere in a repo:

    ![login screen](docs/login.png)
    ![login screen](./images/login.png)
    ![login screen](/docs/login.png)         <- repo-root relative (GitHub style)
    <img src="docs/login.png" width="400">   <- raw HTML images work too

Setup (pure Python, no system libraries needed):
    pip install markdown-it-py reportlab pillow

Usage:
    python readme_to_pdf.py                       # finds README.md in cwd or repo root
    python readme_to_pdf.py path/to/README.md     # explicit file
    python readme_to_pdf.py -o docs.pdf --a4      # custom output name / A4 paper

Output defaults to README.pdf next to the README.
Not supported: SVG images (e.g. shields.io badges) are replaced by their alt text.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import tempfile
import textwrap
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    from markdown_it import MarkdownIt
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        HRFlowable, Image, ListFlowable, ListItem, Paragraph, Preformatted,
        SimpleDocTemplate, Spacer, Table, TableStyle,
    )
except ImportError:
    sys.exit("Missing dependencies. Install them with:\n"
             "    pip install markdown-it-py reportlab pillow")

# ----------------------------------------------------------------- constants
BORDER = colors.HexColor("#d0d7de")
CODE_BG = colors.HexColor("#f6f8fa")
HEAD_BG = colors.HexColor("#eef1f4")
LINK = "#0366d6"
LIST_INDENT = 18
QUOTE_INDENT = 15
CODE_CHUNK_LINES = 40          # code blocks are split so long ones can span pages
HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
VOID = {"br", "img", "hr", "source", "input", "meta", "link", "col", "wbr"}
BLOCKISH = {"div", "section", "article", "details", "summary", "center",
            "figure", "figcaption", "picture", "header", "footer", "nav"}
EMOJI_RE = re.compile("[\U0001F000-\U0001FFFF\uFE0F\u200D]")
SWAPS = {"\u2705": "\u2713", "\u274C": "\u2717", "\u2714\uFE0F": "\u2714"}


def clean_text(text: str, unicode_ok: bool) -> str:
    """Drop emoji and, when only core PDF fonts are available, non-Latin-1 characters."""
    for k, v in SWAPS.items():
        text = text.replace(k, v)
    text = EMOJI_RE.sub("", text)
    if not unicode_ok:
        text = text.encode("cp1252", "ignore").decode("cp1252")
    return text


def warn(msg: str) -> None:
    print(f"  warning: {msg}", file=sys.stderr)


# ------------------------------------------------- anchors / table of contents
def slugify(text: str) -> str:
    """GitHub-style heading id: lowercase, punctuation dropped, spaces -> hyphens."""
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def collect_anchors(body: str) -> tuple[list[str], set[str]]:
    """Pre-scan the rendered HTML so links can point at headings that come later.

    Returns (slug for each heading in document order, every valid link target).
    Duplicate headings get -1, -2 ... suffixes, exactly like GitHub.
    """
    slugs: list[str] = []
    seen: dict[str, int] = {}
    known: set[str] = set()
    for m in re.finditer(r"<h([1-6])[^>]*>(.*?)</h\1>", body, re.S):
        base = slugify(m.group(2))
        n = seen.get(base, 0)
        seen[base] = n + 1
        slug = base if n == 0 else f"{base}-{n}"
        slugs.append(slug)
        known.add(slug)
    for m in re.finditer(r"<[a-zA-Z][^>]*?(?<![\w-])(?:name|id)\s*=\s*[\"']([^\"']+)[\"']", body):
        known.add(m.group(1).lower())
    return slugs, known


class Heading(Paragraph):
    """A heading paragraph that remembers its level/title for the PDF outline."""

    def __init__(self, text, style, level, slug, title):
        super().__init__(text, style)
        self.level, self.slug, self.title = level, slug, title


class Doc(SimpleDocTemplate):
    """Adds clickable bookmarks (the PDF viewer's sidebar outline) for every heading."""

    _last_level = -1

    def afterFlowable(self, flowable):
        if isinstance(flowable, Heading):
            key = f"outline-{flowable.slug}-{id(flowable)}"
            level = min(flowable.level - 1, self._last_level + 1)   # outline can't skip levels
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(flowable.title or "(untitled)", key, level=level)
            self._last_level = level


# --------------------------------------------------------------------- fonts
def setup_fonts() -> dict:
    """Use a Unicode TrueType family if one is installed, else core PDF fonts."""
    dj = Path("/usr/share/fonts/truetype/dejavu")
    win = Path(r"C:\Windows\Fonts")
    mac = Path("/System/Library/Fonts/Supplemental")
    candidates = [
        dict(regular=dj / "DejaVuSans.ttf", bold=dj / "DejaVuSans-Bold.ttf",
             italic=dj / "DejaVuSans-Oblique.ttf", bi=dj / "DejaVuSans-BoldOblique.ttf",
             mono=dj / "DejaVuSansMono.ttf"),
        dict(regular=win / "arial.ttf", bold=win / "arialbd.ttf",
             italic=win / "ariali.ttf", bi=win / "arialbi.ttf", mono=win / "consola.ttf"),
        dict(regular=mac / "Arial.ttf", bold=mac / "Arial Bold.ttf",
             italic=mac / "Arial Italic.ttf", bi=mac / "Arial Bold Italic.ttf",
             mono=mac / "Courier New.ttf"),
    ]
    for c in candidates:
        if all(c[k].is_file() for k in ("regular", "bold", "italic", "bi")):
            try:
                pdfmetrics.registerFont(TTFont("BodyX", str(c["regular"])))
                pdfmetrics.registerFont(TTFont("BodyX-B", str(c["bold"])))
                pdfmetrics.registerFont(TTFont("BodyX-I", str(c["italic"])))
                pdfmetrics.registerFont(TTFont("BodyX-BI", str(c["bi"])))
                pdfmetrics.registerFontFamily(
                    "BodyX", normal="BodyX", bold="BodyX-B",
                    italic="BodyX-I", boldItalic="BodyX-BI")
                mono, mono_uni = "Courier", False
                if c["mono"].is_file():
                    pdfmetrics.registerFont(TTFont("MonoX", str(c["mono"])))
                    mono, mono_uni = "MonoX", True
                return dict(regular="BodyX", bold="BodyX-B", mono=mono,
                            uni=True, mono_uni=mono_uni)
            except Exception:
                pass
    return dict(regular="Helvetica", bold="Helvetica-Bold", mono="Courier",
                uni=False, mono_uni=False)


def make_styles(F: dict) -> dict:
    def P(name, **kw):
        kw.setdefault("fontName", F["regular"])
        return ParagraphStyle(name, **kw)

    S = {
        "body": P("body", fontSize=10, leading=14.5, spaceAfter=6),
        "li": P("li", fontSize=10, leading=14, spaceAfter=2),
        "quote": P("quote", fontSize=10, leading=14, spaceAfter=4,
                   textColor=colors.HexColor("#57606a")),
        "cell": P("cell", fontSize=9, leading=12),
        "th": P("th", fontName=F["bold"], fontSize=9, leading=12),
        "alt": P("alt", fontSize=8.5, leading=11, textColor=colors.grey, spaceAfter=4),
        "pre": ParagraphStyle("pre", fontName=F["mono"], fontSize=8.5, leading=11),
    }
    sizes = {"h1": 22, "h2": 17, "h3": 13.5, "h4": 11.5, "h5": 10.5, "h6": 10}
    for h, sz in sizes.items():
        S[h] = P(h, fontName=F["bold"], fontSize=sz, leading=sz * 1.25,
                 spaceBefore=sz * 0.7, spaceAfter=sz * 0.35)
    return S


# --------------------------------------------------------- image resolution
def find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / ".git").exists():
            return p
    return start


def make_resolver(readme_dir: Path, repo_root: Path, tmpdir: Path):
    counter = [0]

    def resolve(src: str) -> str | None:
        if not src or src.startswith("data:"):
            return None
        if src.startswith(("http://", "https://")):
            if Path(urlparse(src).path).suffix.lower() == ".svg":
                return None
            try:
                counter[0] += 1
                dest = tmpdir / f"remote_{counter[0]}"
                req = urllib.request.Request(src, headers={"User-Agent": "readme_to_pdf"})
                with urllib.request.urlopen(req, timeout=10) as r, open(dest, "wb") as f:
                    f.write(r.read())
                return str(dest)
            except Exception as e:
                warn(f"could not download {src} ({e.__class__.__name__})")
                return None
        rel = unquote(urlparse(src).path)
        if Path(rel).suffix.lower() == ".svg":
            warn(f"SVG not supported, skipping {src}")
            return None
        stripped = rel.lstrip("/")
        order = ([repo_root, readme_dir] if rel.startswith("/") else [readme_dir, repo_root])
        for base in order:
            cand = (base / stripped).resolve()
            if cand.is_file():
                return str(cand)
        warn(f"image not found: {src}")
        return None

    return resolve


# ------------------------------------------------------------ HTML -> PDF
class Builder(HTMLParser):
    """Turns the HTML produced by `markdown` into reportlab flowables."""

    def __init__(self, S, F, resolve, frame_w, slugs=(), known=frozenset()):
        super().__init__(convert_charrefs=True)
        self.slugs, self.slug_i, self.known = list(slugs), 0, set(known)
        self.carry = ""            # anchors waiting for the next paragraph to attach to
        self.S, self.F, self.resolve, self.frame_w = S, F, resolve, frame_w
        self.root: list = []
        self.containers = [self.root]
        self.inline: list[str] = []
        self.style_stack = ["body"]
        self.fmt = dict(b=0, i=0, code=0, u=0, strike=0, sup=0, sub=0)
        self.links: list[str] = []
        self.in_pre = False
        self.pre_buf: list[str] = []
        self.lists: list[dict] = []
        self.quote = 0
        self.indent = 0
        self.table: dict | None = None
        self.skip = 0

    # ---- helpers
    def add(self, flow):
        self.containers[-1].append(flow)

    def clean(self, text: str, mono: bool = False) -> str:
        return clean_text(text, self.F["mono_uni"] if mono else self.F["uni"])

    def flush(self):
        text = "".join(self.inline).strip()
        self.inline = []
        if not text:
            return
        if re.fullmatch(r'(<a name="[^"]*"/>)+', text):
            self.carry += text     # a bare <a name="x"></a>: attach to the next paragraph
            return
        text, self.carry = self.carry + text, ""
        style = self.style_stack[-1]
        if style == "body":
            style = "quote" if self.quote else ("li" if self.lists else "body")
        if style in HEADINGS:
            slug = self.slugs[self.slug_i] if self.slug_i < len(self.slugs) else ""
            self.slug_i += 1
            plain = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
            anchor = f'<a name="{slug}"/>' if slug else ""
            try:
                self.add(Heading(anchor + text, self.S[style], int(style[1]), slug, plain))
                return
            except Exception:
                self.add(Paragraph(html.escape(plain), self.S[style]))
                return
        try:
            self.add(Paragraph(text, self.S[style]))
        except Exception:
            self.add(Paragraph(html.escape(re.sub(r"<[^>]+>", "", text)), self.S[style]))

    # ---- images
    def add_image(self, a: dict):
        self.flush()
        src, alt = a.get("src", ""), a.get("alt", "")
        path = self.resolve(src)
        if path:
            try:
                img = Image(path)
                w, h = img.imageWidth, img.imageHeight
                # ListFlowable adds ~9pt more indent per nesting level than we track
                maxw = (self.frame_w - self.indent - (12 if self.quote else 0)
                        - 9 * len(self.lists))
                wattr = a.get("width", "")
                m = re.match(r"\s*(\d+)\s*(px)?\s*$", wattr)
                pct = re.match(r"\s*(\d+(?:\.\d+)?)\s*%\s*$", wattr)
                if m:
                    wd = int(m.group(1)) * 0.75                  # px -> pt
                elif pct:
                    wd = maxw * float(pct.group(1)) / 100
                else:
                    wd = w * 0.75
                wd = min(wd, maxw)
                ht = wd * h / w
                if ht > 8.2 * inch:
                    ht = 8.2 * inch
                    wd = ht * w / h
                img.drawWidth, img.drawHeight, img.hAlign = wd, ht, "LEFT"
                self.add(img)
                self.add(Spacer(1, 6))
                return
            except Exception as e:
                warn(f"cannot embed {src} ({e.__class__.__name__}: {e})")
        label = alt or src
        if label:
            self.add(Paragraph(f"[image: {html.escape(self.clean(label))}]", self.S["alt"]))

    # ---- code blocks
    def end_pre(self):
        text = "".join(self.pre_buf).strip("\n")
        self.pre_buf, self.in_pre = [], False
        if not text.strip():
            return
        width = self.frame_w - self.indent - (12 if self.quote else 0)
        maxchars = max(20, int((width - 18) / (self.S["pre"].fontSize * 0.62)))
        lines: list[str] = []
        for raw in self.clean(text, mono=True).split("\n"):
            raw = raw.expandtabs(4).rstrip()
            if len(raw) <= maxchars:
                lines.append(raw)
                continue
            ind = len(raw) - len(raw.lstrip(" "))
            lines.extend(textwrap.wrap(
                raw, maxchars, subsequent_indent=" " * min(ind + 2, maxchars // 2),
                drop_whitespace=False, break_long_words=True, break_on_hyphens=False))
        chunks = ["\n".join(lines[i:i + CODE_CHUNK_LINES])
                  for i in range(0, len(lines), CODE_CHUNK_LINES)]
        t = Table([[Preformatted(c, self.S["pre"])] for c in chunks], colWidths=[width])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
            ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, 0), 6), ("BOTTOMPADDING", (0, -1), (-1, -1), 6),
        ]))
        self.add(t)
        self.add(Spacer(1, 8))

    # ---- tables
    def end_table(self):
        t, self.table = self.table, None
        rows = t["rows"]
        if not rows:
            return
        n = max(len(r) for r in rows)
        rows = [r + [""] * (n - len(r)) for r in rows]
        PAD = 10                                   # left + right cell padding
        # Desired width per column: wide enough for its text (up to ~50 chars) or its
        # widest image, whichever is larger.
        want = [40.0] * n
        for r in rows:
            for j, cell in enumerate(r):
                if not isinstance(cell, list):
                    continue
                chars = sum(len(f.getPlainText()) for f in cell if isinstance(f, Paragraph))
                img_w = max([f.drawWidth for f in cell if isinstance(f, Image)] or [0])
                want[j] = max(want[j], min(max(chars, 6), 50) * 5.2 + PAD, img_w + PAD)
        avail = self.frame_w - self.indent
        scale = avail / sum(want)                  # shrink to fit the page (or stretch text tables)
        widths = [w * scale for w in want]
        # Images must never be wider than their column or neighbouring cells paint over them.
        for r in rows:
            for j, cell in enumerate(r):
                if not isinstance(cell, list):
                    continue
                room = widths[j] - PAD
                for f in cell:
                    if isinstance(f, Image) and f.drawWidth > room > 0:
                        ratio = room / f.drawWidth
                        f.drawWidth, f.drawHeight = room, f.drawHeight * ratio
        cells = [[c if c else "" for c in r] for r in rows]
        hdr = t["hdr"]
        cmds = [("GRID", (0, 0), (-1, -1), 0.5, BORDER), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if hdr:
            cmds.append(("BACKGROUND", (0, 0), (-1, hdr - 1), HEAD_BG))
        tbl = Table(cells, colWidths=widths, repeatRows=hdr)
        tbl.setStyle(TableStyle(cmds))
        self.add(tbl)
        self.add(Spacer(1, 8))

    # ---- parser callbacks
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
            return
        if self.in_pre or self.skip:
            return
        f = self.fmt
        if tag in HEADINGS:
            self.flush()
            self.style_stack.append(tag)
        elif tag == "p" or tag in BLOCKISH:
            self.flush()
        elif tag == "br":
            self.inline.append("<br/>")
        elif tag in ("strong", "b"):
            f["b"] += 1
        elif tag in ("em", "i"):
            f["i"] += 1
        elif tag in ("code", "kbd", "tt"):
            f["code"] += 1
        elif tag in ("u", "ins"):
            f["u"] += 1
        elif tag in ("del", "s", "strike"):
            f["strike"] += 1
        elif tag in ("sup", "sub"):
            f[tag] += 1
        elif tag == "a":
            self.links.append(a.get("href", "") or "")
        elif tag == "img":
            self.add_image(a)
        elif tag == "hr":
            self.flush()
            self.add(HRFlowable(width="100%", thickness=0.6, color=BORDER,
                                spaceBefore=6, spaceAfter=8))
        elif tag == "pre":
            self.flush()
            self.in_pre, self.pre_buf = True, []
        elif tag in ("ul", "ol"):
            self.flush()
            try:
                start = int(a.get("start", 1))
            except ValueError:
                start = 1
            self.lists.append(dict(kind=tag, items=[], start=start))
            self.indent += LIST_INDENT
        elif tag == "li":
            self.flush()
            self.containers.append([])
        elif tag == "blockquote":
            self.flush()
            self.containers.append([])
            self.quote += 1
            self.indent += QUOTE_INDENT
        elif tag == "table":
            self.flush()
            self.table = dict(rows=[], row=None, hdr=0, th=False)
        elif tag == "tr" and self.table is not None:
            self.table["row"], self.table["th"] = [], False
        elif tag in ("td", "th") and self.table is not None:
            self.flush()
            self.containers.append([])
            self.style_stack.append("th" if tag == "th" else "cell")
            if tag == "th":
                self.table["th"] = True
        # explicit anchors: <a name="x">, <a id="x">, <div id="x"> ... can be link targets
        for key in ("id", "name"):
            val = (a.get(key) or "").strip().lower()
            if val and re.fullmatch(r"[^\"'<>&\s]+", val):
                self.inline.append(f'<a name="{val}"/>')

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if tag in VOID or self.skip:
            return
        if tag == "pre":
            if self.in_pre:
                self.end_pre()
            return
        if self.in_pre:
            return
        f = self.fmt
        if tag in HEADINGS:
            self.flush()
            if len(self.style_stack) > 1:
                self.style_stack.pop()
            if tag in ("h1", "h2"):
                self.add(HRFlowable(width="100%", thickness=0.6, color=BORDER,
                                    spaceBefore=0, spaceAfter=8))
        elif tag == "p" or tag in BLOCKISH:
            self.flush()
        elif tag in ("strong", "b"):
            f["b"] = max(0, f["b"] - 1)
        elif tag in ("em", "i"):
            f["i"] = max(0, f["i"] - 1)
        elif tag in ("code", "kbd", "tt"):
            f["code"] = max(0, f["code"] - 1)
        elif tag in ("u", "ins"):
            f["u"] = max(0, f["u"] - 1)
        elif tag in ("del", "s", "strike"):
            f["strike"] = max(0, f["strike"] - 1)
        elif tag in ("sup", "sub"):
            f[tag] = max(0, f[tag] - 1)
        elif tag == "a":
            if self.links:
                self.links.pop()
        elif tag == "li":
            self.flush()
            if len(self.containers) > 1:
                flows = self.containers.pop()
                if self.lists:
                    self.lists[-1]["items"].append(
                        ListItem(flows or [Paragraph(" ", self.S["li"])]))
        elif tag in ("ul", "ol"):
            self.flush()
            if not self.lists:
                return
            lst = self.lists.pop()
            self.indent -= LIST_INDENT
            if lst["items"]:
                depth = len(self.lists)
                common = dict(leftIndent=LIST_INDENT, bulletFontName=self.F["regular"],
                              bulletFontSize=10)
                if lst["kind"] == "ul":
                    fl = ListFlowable(lst["items"], bulletType="bullet",
                                      start="\u2022" if depth == 0 else "\u2013", **common)
                else:
                    fl = ListFlowable(lst["items"], bulletType="1",
                                      bulletFormat="%s.", start=lst["start"], **common)
                self.add(fl)
                if depth == 0:
                    self.add(Spacer(1, 4))
        elif tag == "blockquote":
            self.flush()
            if self.quote and len(self.containers) > 1:
                flows = self.containers.pop()
                self.quote -= 1
                self.indent -= QUOTE_INDENT
                if flows:
                    t = Table([[flows]], colWidths=[self.frame_w - self.indent])
                    t.setStyle(TableStyle([
                        ("LINEBEFORE", (0, 0), (0, -1), 3, BORDER),
                        ("LEFTPADDING", (0, 0), (-1, -1), 12),
                        ("TOPPADDING", (0, 0), (-1, -1), 2),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ]))
                    self.add(t)
                    self.add(Spacer(1, 6))
        elif tag in ("td", "th") and self.table is not None:
            self.flush()
            if len(self.containers) > 1:
                flows = self.containers.pop()
                if len(self.style_stack) > 1:
                    self.style_stack.pop()
                if self.table["row"] is not None:
                    self.table["row"].append(flows)
        elif tag == "tr" and self.table is not None:
            t = self.table
            if t["row"] is not None:
                t["rows"].append(t["row"])
                if t["th"] and t["hdr"] == len(t["rows"]) - 1:
                    t["hdr"] += 1
                t["row"] = None
        elif tag == "table" and self.table is not None:
            self.end_table()

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_pre:
            self.pre_buf.append(data)
            return
        data = re.sub(r"[ \t\r\n\f]+", " ", data)
        if not data:
            return
        f = self.fmt
        data = self.clean(data, mono=bool(f["code"]))
        if not data:
            return
        txt = html.escape(data, quote=False)
        if f["code"]:
            txt = f'<font name="{self.F["mono"]}" color="#b3224f">{txt}</font>'
        if f["sup"]:
            txt = f"<super>{txt}</super>"
        if f["sub"]:
            txt = f"<sub>{txt}</sub>"
        if f["strike"]:
            txt = f"<strike>{txt}</strike>"
        if f["u"]:
            txt = f"<u>{txt}</u>"
        if f["i"]:
            txt = f"<i>{txt}</i>"
        if f["b"]:
            txt = f"<b>{txt}</b>"
        if self.links and self.links[-1]:
            href = self.links[-1]
            if href.startswith(("http://", "https://", "mailto:")):
                txt = f'<link href="{html.escape(href, quote=True)}" color="{LINK}">{txt}</link>'
            elif href.startswith("#") and unquote(href[1:]).lower() in self.known:
                # internal link (table of contents): jump to the heading inside the PDF
                dest = html.escape(unquote(href[1:]).lower(), quote=True)
                txt = f'<link href="#{dest}" color="{LINK}">{txt}</link>'
            else:
                txt = f'<font color="{LINK}">{txt}</font>'
        self.inline.append(txt)


# -------------------------------------------------------------- top level
def locate_readme(arg: str | None) -> Path:
    if arg:
        p = Path(arg).expanduser().resolve()
        if not p.is_file():
            sys.exit(f"File not found: {p}")
        return p
    cwd = Path.cwd()
    for base in (cwd, find_repo_root(cwd)):
        for f in sorted(base.iterdir()):
            if f.is_file() and f.name.lower() in ("readme.md", "readme.markdown"):
                return f.resolve()
    sys.exit("No README.md found in the current folder or the repo root.")


def convert(readme: Path, output: Path, pagesize) -> None:
    readme_dir = readme.parent
    repo_root = find_repo_root(readme_dir)
    text = readme.read_text(encoding="utf-8-sig")
    # CommonMark (same rules as GitHub): 2-space nested lists, tables, ~~strike~~, raw HTML
    body = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"]).render(text)

    F = setup_fonts()
    S = make_styles(F)
    margin = 0.75 * inch
    frame_w = pagesize[0] - 2 * margin

    m = re.search(r"^#\s+(.+?)\s*#*\s*$", text, re.M)
    title = re.sub(r"[`*_\[\]]", "", m.group(1)) if m else readme_dir.name
    title_pdf = clean_text(title, F["uni"]).strip()

    with tempfile.TemporaryDirectory() as tmp:
        slugs, known = collect_anchors(body)
        builder = Builder(S, F, make_resolver(readme_dir, repo_root, Path(tmp)), frame_w,
                          slugs, known)
        builder.feed(body)
        builder.close()
        builder.flush()
        for extra in builder.containers[1:]:      # close anything left open
            builder.root.extend(extra)
        story = builder.root or [Paragraph("(empty README)", S["body"])]

        def footer(canvas, doc):
            canvas.saveState()
            canvas.setFont(F["regular"], 8)
            canvas.setFillColor(colors.grey)
            canvas.drawString(margin, 0.45 * inch, title_pdf[:90])
            canvas.drawRightString(pagesize[0] - margin, 0.45 * inch, f"Page {doc.page}")
            canvas.restoreState()

        doc = Doc(
            str(output), pagesize=pagesize, title=title_pdf,
            leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=0.8 * inch)
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
    print(f"Created {output}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Convert README.md to PDF (with screenshots).")
    ap.add_argument("readme", nargs="?", help="path to README.md (default: auto-detect)")
    ap.add_argument("-o", "--output", help="output PDF path (default: README.pdf beside it)")
    ap.add_argument("--a4", action="store_true", help="use A4 paper instead of US Letter")
    args = ap.parse_args()

    readme = locate_readme(args.readme)
    output = Path(args.output).expanduser().resolve() if args.output else readme.with_suffix(".pdf")
    convert(readme, output, A4 if args.a4 else letter)


if __name__ == "__main__":
    main()