# Instructions for AI coding agents

You're helping someone launch a website for a yo-yo, kendama, diabolo, spinning top, or juggling contest, a
mixed skill toy contest, or a casual trick battle from this template. The human-facing guide is [README.md](README.md). Read it, then follow this.

## Goal
Get a correct, live contest site in one session with as few human steps as possible.

## Steps
1. **Collect facts from the user**:
   - preset (`yoyo-contest`, `kendama-contest`, `diabolo-contest`, `spintop-contest`, `juggling-contest`,
     `skill-toy-contest`, `trick-battle`)
   - contest name, edition, date (and end date), hours, city, region, and time zone
   - admission for spectators, the organizer, and any presenting sponsor
   - registration open and close dates, the sign-up link, and fees per division
   - divisions (if they differ from the preset): code, name, routine length, fee, age limits, styles,
     and whether each one uses the competitor's own music, house music, or none; any combo prices
   - each division's format and its details: trick list and tries per trick (ladder), bracket rules and
     who picks winners (judges, crowd, or an audience poll on a stream), judging criteria and points
     (panel), unit and lower/higher is better (timed or scored), team size and per-team fee, and rounds
     (prelims → finals, how many advance)
   - guest events and the day's schedule
   - music file rules and deadline (only if some division uses the competitor's own music)
   - what competitors should bring (gear), if it differs from the preset
   - venue name, space, address, parking, vendor rules, and hotel blocks
   - sponsors by tier, partners, and sponsorship package prices and slots
   - a shared contact email and social links

   Never invent facts. Leave a value empty (`""`), or use "TBD", rather than guess.
2. **Edit `site.jsonc` only** for content. `examples/vsyc-26.jsonc` is a complete real contest; use it
   as a model. To change preset sections (divisions, gear, schedule, rules, FAQ, terms), copy the section
   into `site.jsonc`; don't edit the preset. Set a section to `false` to drop it. The division fields are
   documented in README "Divisions" and "Division formats". Use the same format words as the
   registration app (`freestyle`, `panel`, `timed`/`scored`, `ladder`, `bracket`, `showcase`, plus `team`
   and `rounds`) so the site and the sign-up form describe each division the same way. If the schedule
   names rounds, use `"division"` + `"round"` items so the build checks them against the divisions.
3. **Check every stage.** Run the build with `--today` set before registration opens, while it's open,
   after it closes, on contest day, and the day after. Each build prints the status line. Confirm
   the dates with the user.
4. **Build and check:** `python3 build.py && python3 scripts/check_site.py`. Fix every WARNING (contrast,
   missing photos, bad dates) and every check failure.
5. **Deploy (GitHub Pages):** the human must create the repository from the template and set
   **Settings → Pages → Source: GitHub Actions**. Then push to `main`. The workflow builds, checks,
   deploys, and rebuilds daily. Confirm the run is green and the page loads.
6. **Report** the live URL and what's left: the sign-up form, sponsor logos, venue photos, a custom
   domain, a review of the terms, and two-factor login.

## After the contest
Fill in `results` (podium per division, stats, links) and `wrap` (home page numbers and links) with
facts the user gives you. For next year, update the dates and clear `results` and `wrap`.

## Rules
- **Competitor privacy:** list minors by first name and last initial unless a parent opted in. Never
  publish ages of minors, home addresses, personal phone numbers, or registration data. Photos only
  with the user's confirmation of permission; resize to ~1200px, under 500 KB, and strip EXIF/GPS.
- **Terms:** the preset terms aren't legal advice. Tell the user to have them reviewed. Don't invent
  new legal promises.
- **Rules accuracy:** the yo-yo preset summarizes the NYYL freestyle rules and links to the source.
  Don't add numbers (multipliers, routine lengths) the user hasn't confirmed from the current rules.
  The other presets describe typical formats, not any organization's official rules. Never present
  them as an official ruleset, and don't invent a league, sanctioning body, or point system. Trick
  lists, judging criteria, and points in presets and demos are examples: confirm the real ones with the
  user before publishing them.
- **Toy wording** (gear, equipment, music, safety) belongs in the preset or `site.jsonc`, never in
  `build.py`. Keep `build.py`'s own text neutral so every toy works.
- **Security:**
  - No inline `<script>`, `<style>`, `style=""`, or `on*=` handlers. The CSP blocks them and the check fails.
  - No `http://` links, no trackers, embeds, or third-party scripts unless the user asks. If they do,
    update `Site.csp()` in `build.py`.
  - The site never handles payments or personal data; it links out to the sign-up tool.
- **Look:** square corners and flat cards with no shadows, like the VSYC-26 pages. Change it through
  `theme.corners`, not `style.css`, unless asked.
- **Consistency:** every page shares one header (with the status bar) and footer. The check fails if they differ.
- **Pages:** each page is one `page_<slug>()` method in `build.py`, listed in `self.pages`.
- **Presets:** a new preset is one `presets/<name>.json` with the same sections as the others. Add a
  demo in `examples/`, list it in `scripts/build_showcase.py`, the showcase page, README, and here, and
  build it at every stage with `--today`.
- **Showcase:** `showcase/`, `examples/`, and `scripts/build_showcase.py` only run in the original
  template repository. Ignore them (or delete them) in a user's copy.
- **Smoke test:** `scripts/smoke_test.js` and `.github/workflows/smoke-test.yml` click through every page of the built showcase
  and its examples at phone width. They only run in the original template repository. Run it after changing `build.py`
  or `assets/`: `python3 scripts/build_showcase.py && node scripts/smoke_test.js` (needs Node and Playwright).
- **No dependencies:** keep `build.py` and `scripts/check_site.py` standard-library Python 3.9+.

## Useful commands
```sh
python3 build.py --serve                     # build + preview at http://localhost:8000/
python3 build.py --today 2026-11-15          # preview another day (any stage)
python3 build.py --base-url https://x.org/   # build for a specific address
python3 scripts/check_site.py                # must print "OK"
```
