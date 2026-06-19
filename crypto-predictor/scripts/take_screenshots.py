"""Take screenshots of BTC predictor pages using playwright."""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

OUT = Path("/home/popeye/crypto-predictor/tutorial_btc_assets")
OUT.mkdir(exist_ok=True)
BASE = "http://127.0.0.1:8001"

SHOTS = [
    ("index.png",          f"{BASE}/?symbol=BTCUSDT",                                    (1100, 2600)),
    ("hourly_call.png",    f"{BASE}/hourly-call",                                        (1100, 2000)),
    ("intra15_empty.png",  f"{BASE}/intra15",                                            (1100, 1100)),
    ("intra15_market.png", f"{BASE}/intra15?strike=75155.74&yes_cents=87&no_cents=13",  (1100, 1500)),
    ("calibration.png",    f"{BASE}/calibration?symbol=BTCUSDT",                         (1100, 3000)),
]


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for name, url, (w, h) in SHOTS:
            ctx = await browser.new_context(viewport={"width": w, "height": h})
            page = await ctx.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(800)
            out = OUT / name
            await page.screenshot(path=str(out), full_page=True)
            print(f"{name}: {out.stat().st_size} bytes")
            await ctx.close()
        await browser.close()


asyncio.run(main())
