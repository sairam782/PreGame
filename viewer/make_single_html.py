#!/usr/bin/env python3
"""Build the viewer as ONE self-contained HTML file, or refresh the data inside an edited copy.

The single file holds the page, its styles, its script and the public snapshot data. Double-click it to open it in
any browser: no server, no install, no internet. Teammates edit it freely (text, layout, styles, script); the data
sits in one marked block, so a refresh replaces only that block and keeps every design change.

    python make_single_html.py                         # writes dist/pregame-viewer.html from static/ + fixtures/
    python make_single_html.py --artifact out.html     # also writes a copy for a hosted page (no <html>/<head> wrapper)
    python make_single_html.py --refresh-data edited.html [--out new.html]
                                                       # swaps fresh data into a teammate's edited file

The data comes from fixtures/, which must be the PUBLIC snapshot (`server.py --snapshot --public`): no answer key.
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
FIXTURES = HERE / "fixtures"
START = "<!-- PREGAME DATA START: this block is replaced when the data is refreshed; don't edit it by hand -->"
END = "<!-- PREGAME DATA END -->"
EDIT_NOTE = """<!--
  Pregame mission control: one self-contained file. Open it by double-clicking; nothing else is needed.
  Edit freely: the text and layout below, the two <style> blocks, and the page script at the bottom.
  Keep the block between PREGAME DATA START and PREGAME DATA END as it is: it holds the snapshot data and is
  replaced when the data is refreshed. Keep the plain words (unseen test meetings, current and proposed version,
  adopted, audit log, test gate), show every number with what it counts ("7 of 24 preps"), and load nothing from
  other websites except Google Fonts.
-->"""


def load_fixtures(folder):
    meta = json.loads((folder / "_meta.json").read_text(encoding="utf-8"))
    if not meta.get("public"):
        sys.exit("fixtures/ is not the public snapshot. Run: python server.py --snapshot --public")
    data = {}
    for f in sorted(folder.glob("*.json")):
        data[f.stem] = json.loads(f.read_text(encoding="utf-8"))
    return data


def data_block(data):
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f'{START}\n<script type="application/json" id="pregame-data">{text}</script>\n{END}'


def page_parts():
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    title = re.search(r"<title>(.*?)</title>", index, re.S).group(1).strip()
    body = re.search(r"<body>(.*)</body>", index, re.S).group(1).strip()
    tokens = (STATIC / "theme" / "tokens.css").read_text(encoding="utf-8")
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8").replace("</script", "<\\/script")
    return title, body, tokens, styles, script


def build(data, wrapper=True):
    title, body, tokens, styles, script = page_parts()
    inner = (f"<title>{title}</title>\n{EDIT_NOTE}\n"
             f'<style id="theme-tokens">\n{tokens}\n</style>\n<style id="page-styles">\n{styles}\n</style>\n')
    content = f"{body}\n{data_block(data)}\n<script id=\"page-script\">\n{script}\n</script>\n"
    if not wrapper:  # a hosted page adds its own <html>/<head>/<body>
        return inner + content
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            f"{inner}</head>\n<body>\n{content}</body>\n</html>\n")


def refresh(edited_path, data, out_path):
    html = Path(edited_path).read_text(encoding="utf-8")
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
    if len(pattern.findall(html)) != 1:
        sys.exit("The file must contain exactly one data block between the PREGAME DATA START and END markers.")
    new = pattern.sub(lambda _m: data_block(data), html)
    Path(out_path).write_text(new, encoding="utf-8")
    return new


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(HERE / "dist" / "pregame-viewer.html"))
    ap.add_argument("--artifact", help="also write a copy without the <html>/<head>/<body> wrapper, for a hosted page")
    ap.add_argument("--refresh-data", metavar="EDITED_HTML", help="replace only the data block in an edited file")
    ap.add_argument("--fixtures", default=str(FIXTURES))
    a = ap.parse_args()
    data = load_fixtures(Path(a.fixtures))
    if a.refresh_data:
        out = a.out if a.out != str(HERE / "dist" / "pregame-viewer.html") else a.refresh_data
        html = refresh(a.refresh_data, data, out)
        print(f"refreshed the data in {out} ({len(html):,} bytes); everything outside the data block is unchanged")
        return
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    html = build(data)
    Path(a.out).write_text(html, encoding="utf-8")
    print(f"wrote {a.out} ({len(html):,} bytes, {len(data)} data files embedded)")
    if a.artifact:
        Path(a.artifact).write_text(build(data, wrapper=False), encoding="utf-8")
        print(f"wrote {a.artifact} (hosted-page copy)")


if __name__ == "__main__":
    main()
