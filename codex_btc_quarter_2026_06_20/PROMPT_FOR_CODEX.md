# Prompt to paste into Codex

Copy/paste the block below into Codex, after attaching this folder (or zip).

---

> I'm shipping a small new feature today on a hobby BTC prediction project (no real money, runs on a Raspberry Pi). I want your judgment on **5 specific design choices** before I let it run for weeks and accumulate biased data. This is **not a bug-hunt** — it's a design review on ~250 lines of new code.
>
> Read in this order:
>
> 1. `README_FOR_CODEX.md` — what the feature does + the 5 questions + scope
> 2. `code/predictor_web_excerpts.py` — the new `/api/quarter-signal` endpoint and the two frozen helpers it leans on (`_build_intra15`, `_compute_tension`)
> 3. `code/btc_quarter_poller.py` — the new background poller (the heart of the review)
> 4. `code/dashboard_btc_quarter_route.py` — the read-only dashboard route
>
> **Scope:** design + correctness of the new code only. The math inside `_compute_tension` and `_build_intra15` is frozen by prior Codex rounds; don't re-open it. If a question requires changing them, say so as a follow-up.
>
> **What I want back:**
>
> For each of Q1–Q5 in the README:
> - 2–4 sentence judgment
> - One concrete recommendation
> - Severity tag: `[CHANGE BEFORE FIRST DAY]` / `[CHANGE BEFORE FIRST WEEK]` / `[LEAVE]`
>
> If you spot something I didn't ask about (race, SQL issue, etc.), add a Q6 at the end. Don't pad if nothing.
>
> Context to hold:
> - User is a climate-data analyst, not a quant — intuition-building, not edge maximization.
> - Pi 4B 8GB, 101 GB free. ~35k rows/year. DB growth is a non-issue.
> - The feature was already deployed before the review; recommendations that require ALTER TABLE are fine.

---

## How to share the folder with Codex

1. **Zip and attach (preferred)**:
   ```bash
   cd /home/popeye/predictor-pi
   zip -r codex_btc_quarter_2026_06_20.zip codex_btc_quarter_2026_06_20/
   ```
   Attach the zip to your Codex chat.

2. **GitHub**: `tiwini/predictor-pi`, branch `main`, path `codex_btc_quarter_2026_06_20/`.

3. **Paste manually**: README + prompt + `btc_quarter_poller.py` is enough for the core review. Codex will request the rest if needed.

## Size sanity check

Total: ~25 KB of code + 8 KB of docs. Trivial.
