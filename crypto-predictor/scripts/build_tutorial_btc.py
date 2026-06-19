"""Build tutorial_btc.html and tutorial_btc.pdf from tutorial_btc.md."""
import asyncio
import base64
from pathlib import Path

import markdown
from playwright.async_api import async_playwright

ROOT = Path("/home/popeye/crypto-predictor")
MD = ROOT / "tutorial_btc.md"
HTML = ROOT / "tutorial_btc.html"
PDF = ROOT / "tutorial_btc.pdf"

CSS = """
body{max-width:820px;margin:2rem auto;padding:0 1.5rem;
     font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     color:#1f2328;line-height:1.55;background:#fff}
h1,h2,h3{color:#0c4a6e;margin-top:2rem}
h1{border-bottom:2px solid #0c4a6e;padding-bottom:.3rem}
h2{border-bottom:1px solid #d0d7de;padding-bottom:.2rem}
code{background:#f6f8fa;padding:1px 5px;border-radius:3px;font-size:.9em;
     font-family:Menlo,Consolas,monospace}
pre code{display:block;padding:.7rem;overflow-x:auto}
table{border-collapse:collapse;margin:1rem 0;width:100%}
th,td{border:1px solid #d0d7de;padding:.45rem .7rem;text-align:left}
th{background:#f6f8fa}
img{max-width:100%;border:1px solid #d0d7de;border-radius:6px;
    margin:.6rem 0;box-shadow:0 1px 3px rgba(0,0,0,.08)}
blockquote{border-left:4px solid #0969da;padding:.4rem 1rem;color:#57606a;
           background:#f6f8fa;margin:1rem 0}
a{color:#0969da}
hr{border:0;border-top:1px solid #d0d7de;margin:2rem 0}
@media print { body{max-width:none;margin:0;padding:1rem} h1,h2{page-break-after:avoid} img{page-break-inside:avoid} }
"""


def embed_images(html: str) -> str:
    """Replace <img src="tutorial_btc_assets/X.png"> with base64 data URIs."""
    import re
    def repl(m):
        path = ROOT / m.group(1)
        if not path.exists():
            return m.group(0)
        data = base64.b64encode(path.read_bytes()).decode()
        return f'<img src="data:image/png;base64,{data}"'
    return re.sub(r'<img src="(tutorial_btc_assets/[^"]+)"', repl, html)


def build_html() -> str:
    body = markdown.markdown(MD.read_text(), extensions=["tables", "fenced_code"])
    body = embed_images(body)
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<title>BTC Predictor — Guía rápida</title>
<style>{CSS}</style>
</head><body>
{body}
</body></html>"""


async def main():
    html_str = build_html()
    HTML.write_text(html_str)
    print(f"HTML: {HTML.stat().st_size} bytes")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(f"file://{HTML}", wait_until="networkidle")
        await page.pdf(
            path=str(PDF),
            format="Letter",
            margin={"top": "0.6in", "bottom": "0.6in", "left": "0.6in", "right": "0.6in"},
            print_background=True,
        )
        await browser.close()
    print(f"PDF: {PDF.stat().st_size} bytes")


asyncio.run(main())
