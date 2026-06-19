"""Render tutorial.md → tutorial.html (print-styled) → tutorial.pdf via firefox."""
from __future__ import annotations
import subprocess
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).parent
MD = ROOT / "tutorial.md"
HTML = ROOT / "tutorial.html"
PDF = ROOT / "tutorial.pdf"

CSS = """
@page { size: A4; margin: 18mm 16mm; }
html { font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
       font-size: 10.5pt; color: #1a1a1a; line-height: 1.45; }
body { max-width: 180mm; margin: 0 auto; }
h1 { font-size: 20pt; margin: 0 0 .2em 0; border-bottom: 2px solid #222; padding-bottom: .2em; }
h2 { font-size: 14pt; margin-top: 1.4em; color: #0a3d62; border-bottom: 1px solid #dcdcdc; padding-bottom: .15em; }
h3 { font-size: 11.5pt; margin-top: 1em; color: #2c5777; }
h4 { font-size: 10.5pt; margin-top: .8em; color: #444; }
p  { margin: .4em 0; }
ul, ol { margin: .3em 0 .6em 1.2em; }
li { margin: .15em 0; }
code { font-family: "JetBrains Mono", Menlo, Consolas, monospace; font-size: 9pt;
       background: #f3f3f3; padding: 1px 4px; border-radius: 3px; }
pre  { background: #f7f7f7; border: 1px solid #e1e1e1; padding: 8px 10px;
       border-radius: 4px; overflow-x: auto; font-size: 8.5pt; line-height: 1.3;
       white-space: pre; page-break-inside: avoid; }
pre code { background: transparent; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: .6em 0; font-size: 9.5pt; }
th, td { border: 1px solid #d5d5d5; padding: 4px 8px; text-align: left; vertical-align: top; }
th { background: #f0f4f8; }
hr { border: none; border-top: 1px solid #bbb; margin: 1.2em 0; }
strong { color: #111; }
blockquote { border-left: 3px solid #bbb; margin: .6em 0; padding: .2em .8em; color: #555; }
h2, h3, h4 { page-break-after: avoid; }
"""

def main() -> None:
    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True}).enable("table")
    body = md.render(MD.read_text(encoding="utf-8"))
    html = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<title>Weather Predictor — Tutorial</title>
<style>{CSS}</style></head><body>
{body}
</body></html>
"""
    HTML.write_text(html, encoding="utf-8")
    print(f"wrote {HTML}")

    from weasyprint import HTML as WHTML
    WHTML(string=html, base_url=str(ROOT)).write_pdf(str(PDF))
    print(f"pdf: {PDF} ({PDF.stat().st_size if PDF.exists() else 'missing'} bytes)")

if __name__ == "__main__":
    main()
