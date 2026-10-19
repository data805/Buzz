"""Generate the Buzz logo SVGs and render PNGs (via headless Chrome).

Run from anywhere:  python3 voice-agent/branding/build_logos.py
Colours follow the BongoHive AI Lab sub-brand: Neural Slate + Warm Innovation Gold.
"""
import pathlib
import subprocess
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SVG_DIR, PNG_DIR = HERE / "svg", HERE / "png"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

SLATE = "#364250"   # Neural Slate 500 (AI Lab)
GOLD = "#E8A000"    # Warm Innovation Gold 500 (master accent)
WHITE = "#FFFFFF"
BLACK = "#111111"

VARIANTS = {
    # name: (main, accent)
    "color": (SLATE, GOLD),
    "reverse": (WHITE, GOLD),
    "black": (BLACK, BLACK),
    "white": (WHITE, WHITE),
}

ICON_VIEW = (-8, 26, 216, 186)   # x, y, w, h
WORD_VIEW = (0, 0, 290, 100)


def icon(main, accent):
    stinger = []
    # Three tapered segments whose sides converge on a single point (100, 206).
    for y0, y1 in ((166, 176), (182, 191), (197, 206)):
        dx0, dx1 = 26 - (y0 - 166) * 0.65, 26 - (y1 - 166) * 0.65
        stinger.append(f"{100-dx0:.1f},{y0} {100+dx0:.1f},{y0} {100+dx1:.1f},{y1} {100-dx1:.1f},{y1}")
    bars = "".join(
        f'<line x1="{x}" y1="{110-h/2}" x2="{x}" y2="{110+h/2}"/>'
        for x, h in ((80, 14), (90, 30), (100, 46), (110, 30), (120, 14))
    )
    return f"""
  <g fill="none" stroke="{main}" stroke-width="10" stroke-linejoin="round" stroke-linecap="round">
    <polygon points="100,66 138.1,88 138.1,132 100,154 61.9,132 61.9,88"/>
    <polyline points="44.5,89 22,76 -0.5,89 -0.5,115 22,128 44.5,115"/>
    <polyline points="155.5,89 178,76 200.5,89 200.5,115 178,128 155.5,115"/>
    <polyline stroke-width="6" points="88,58 88,50 75,39.5"/>
    <polyline stroke-width="6" points="112,58 112,50 125,39.5"/>
  </g>
  <g stroke="{accent}" stroke-width="7" stroke-linecap="round">{bars}</g>
  <g fill="{accent}"><circle cx="71" cy="36" r="7"/><circle cx="129" cy="36" r="7"/></g>
  <g fill="{main}" stroke="{main}" stroke-width="2" stroke-linejoin="round">
    {''.join(f'<polygon points="{p}"/>' for p in stinger)}
  </g>"""


def wordmark(main):
    # Monoline "Buzz" echoing the geometric BongoHive logotype. Cap height 100, stroke 10.
    return f"""
  <g fill="none" stroke="{main}" stroke-width="10" stroke-linejoin="round" stroke-linecap="butt">
    <path d="M8 48 V6 H44 A21 21 0 0 1 44 48 Z M8 48 V94 H48 A23 23 0 0 0 48 48 H8"/>
    <path d="M96 36 V70 A24 24 0 0 0 144 70 V36 M144 36 V99"/>
    <path d="M170 36 H214 L170 94 H214"/>
    <path d="M240 36 H284 L240 94 H284"/>
  </g>"""


def svg(view, body, title):
    x, y, w, h = view
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x} {y} {w} {h}" '
            f'width="{w}" height="{h}" role="img" aria-label="{title}"><title>{title}</title>{body}\n</svg>\n')


def place(inner_view, body, dx, dy, scale=1.0):
    x, y, _, _ = inner_view
    return f'<g transform="translate({dx} {dy}) scale({scale}) translate({-x} {-y})">{body}</g>'


def build():
    SVG_DIR.mkdir(exist_ok=True)
    PNG_DIR.mkdir(exist_ok=True)
    out = {}
    iw, ih = ICON_VIEW[2], ICON_VIEW[3]
    for name, (main, accent) in VARIANTS.items():
        out[f"buzz-icon-{name}"] = svg(ICON_VIEW, icon(main, accent), "Buzz")

        # Horizontal: icon left, wordmark vertically centred on the hexagon.
        wh = 0.8
        hz = place(ICON_VIEW, icon(main, accent), 0, 0) \
            + place(WORD_VIEW, wordmark(main), iw + 30, 110 - 26 - 50 * wh, wh)
        out[f"buzz-logo-horizontal-{name}"] = svg((0, 0, iw + 30 + 290 * wh, ih), hz, "Buzz")

        # Stacked: icon above a centred wordmark.
        ws, gap = 0.55, 22
        st = place(ICON_VIEW, icon(main, accent), 0, 0) \
            + place(WORD_VIEW, wordmark(main), (iw - 290 * ws) / 2, ih + gap, ws)
        out[f"buzz-logo-stacked-{name}"] = svg((0, 0, iw, ih + gap + 100 * ws), st, "Buzz")

    for stem, text in out.items():
        (SVG_DIR / f"{stem}.svg").write_text(text)
    return out


def render(svg_path, png_path, width, bg="transparent"):
    """Rasterise an SVG at a given pixel width using headless Chrome."""
    text = svg_path.read_text()
    w, h = (float(v) for v in text.split('viewBox="')[1].split('"')[0].split()[2:])
    height = round(width * h / w)
    html = (f'<html><body style="margin:0;background:{bg}">'
            f'<img src="{svg_path.as_uri()}" style="display:block;width:{width}px;height:{height}px"></body></html>')
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(html)
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--default-background-color=00000000", "--allow-file-access-from-files",
                    f"--window-size={width},{height}", f"--screenshot={png_path}", f.name],
                   check=True, capture_output=True)


def render_cobrand(variant, png_path, bg):
    """Buzz | BongoHive AI Lab, side by side. Equal heights line up both cap 'B's."""
    buzz = SVG_DIR / f"buzz-logo-horizontal-{variant}.svg"
    bh = HERE / "bongohive" / ("bongohive-ai-lab-white.png" if variant == "reverse" else "bongohive-ai-lab.png")
    rule = "rgba(255,255,255,.35)" if variant == "reverse" else "rgba(54,66,80,.3)"
    h, pad = 240, 96
    html = f"""<html><body style="margin:0;background:{bg}">
<div style="display:flex;align-items:center;gap:72px;padding:{pad}px;width:max-content">
  <img src="{buzz.as_uri()}" style="height:{h}px">
  <div style="width:3px;height:{h * 1.1}px;background:{rule}"></div>
  <img src="{bh.as_uri()}" style="height:{h}px">
</div></body></html>"""
    width = round(pad * 2 + h * (478 / 186) + 72 * 2 + 3 + h * (3020 / 704))
    height = round(pad * 2 + h * 1.1)
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(html)
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--default-background-color=00000000", "--allow-file-access-from-files",
                    f"--window-size={width},{height}", f"--screenshot={png_path}", f.name],
                   check=True, capture_output=True)


if __name__ == "__main__":
    build()
    for p in sorted(SVG_DIR.glob("*.svg")):
        widths = {"icon": [512, 1024], "horizontal": [1600], "stacked": [1000]}
        kind = next(k for k in widths if k in p.stem)
        for w in widths[kind]:
            suffix = f"-{w}" if kind == "icon" else ""
            render(p, PNG_DIR / f"{p.stem}{suffix}.png", w)
    render(SVG_DIR / "buzz-icon-color.svg", PNG_DIR / "favicon-64.png", 64)
    render_cobrand("color", PNG_DIR / "buzz-x-bongohive-light.png", "#FFFFFF")
    render_cobrand("color", PNG_DIR / "buzz-x-bongohive-transparent.png", "transparent")
    render_cobrand("reverse", PNG_DIR / "buzz-x-bongohive-dark.png", SLATE)
    print("done")
