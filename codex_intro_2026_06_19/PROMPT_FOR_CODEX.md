# Prompt to paste into Codex

Copy and paste the block below into Codex, alongside (or after attaching) this entire folder.

---

> I'm sharing a hobby/educational weather-prediction project for a fresh review. It is **not** a bug hunt — I'd like you to read enough of it to form an opinion and then tell me what stands out, good or bad.
>
> Read these in order:
>
> 1. `README_FOR_CODEX.md` — the project intro and reading order
> 2. `data/SUMMARY.md` — live production numbers (we run this 24/7 on a Raspberry Pi)
> 3. `code/predictor.py` → function `build_snapshot` — the main pipeline
> 4. `code/bias_tracker.py` — most-iterated module
> 5. `code/external_models.py` — combined ceiling on external influence
> 6. `code/peak_window.py` — newest module (empirical clock from last 7 days)
> 7. `code/routes_summary.md` — what the user sees daily
>
> Skim the rest of `code/` only if something there is load-bearing for an observation you want to make. `predictor_web.py` is deliberately not in the folder because it's 4k lines of Flask render-blocks with no analytical logic; `routes_summary.md` covers what it does.
>
> Tests are in `tests/`. 149 unit tests, no DB or network. Run with `pytest tests/ -q` if you want.
>
> **What to give me back:**
>
> Pick whichever of the following resonate (skip the rest):
>
> - Architectural smells in the snapshot pipeline
> - Math/stats choices that feel off (Bayesian σ-reweight, sign-nudge smoothstep, combined ceiling, isotonic gating)
> - Operational risks for 24/7 unattended Pi deployment (DB growth, error handling, retry behavior)
> - Features or instrumentation that would materially improve the project with low effort
> - Anything that looks over-engineered for the educational goal
>
> Format: **3–6 specific observations**, each with a file:line reference, ranked by how much they'd matter if addressed. Brief is better than thorough. If you have one strong opinion and nothing else, that's also fine.
>
> Context I want you to hold:
>
> - User is a climate-data analyst, not a quant. Goal is intuition-building, not maximizing edge.
> - No real money — Kalshi is read-only public API; "bets" are simulated at $10 flat with auto-entry at |edge|≥5pp.
> - Project has been live ~63 days; just migrated to Pi for 24/7 ops; 13 new stations added today (no settled bets yet).
> - I'm in Puerto Rico; stations span US timezones. The clock widget shows both.

---

## How to share the folder with Codex

Three options, pick whichever your Codex access supports:

1. **Direct upload (preferred if available)** — zip and attach:
   ```bash
   cd /home/popeye/predictor-pi
   zip -r codex_intro_2026_06_19.zip codex_intro_2026_06_19/
   ```
   Then attach the zip to your Codex chat.

2. **GitHub** — the repo is at `github.com/tiwini/predictor-pi` (private). Grant Codex's account read access, then point it at:
   ```
   github.com/tiwini/predictor-pi/tree/main/codex_intro_2026_06_19
   ```

3. **Paste manually** — for very small Codex contexts: paste `README_FOR_CODEX.md` + `data/SUMMARY.md` + the prompt. Codex will ask for specific files if it needs them. Less ideal but works.

## Folder size sanity check

Total: ~135 KB of code + 4 KB of docs. Well within any modern context window.
