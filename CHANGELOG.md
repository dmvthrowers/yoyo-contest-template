# Changelog

What changed in each release of this template. Newest first. Copies of the template can use it to see what is new before they update (see [docs/UPDATING.md](docs/UPDATING.md)).

## Unreleased

Nothing yet. Changes that merge after 1.0.0 are listed here until the next tag.

## 1.0.0

The first tagged release: a contest website built from one settings file.

- Home, schedule, register, rules, venue, sponsors, results, FAQ and terms pages from `site.jsonc` and a preset, with a status line that follows the contest's stage (before, open, closed, today, past).
- Seven presets (yo-yo, kendama, diabolo, spinning top, juggling, mixed skill toys, trick battle).
- Data-driven divisions with fees, age limits, music rules, team entries and rounds, and division formats: freestyle, panel, timed, scored, ladder, bracket and showcase.
- Fees, schedule and rules are checked against the divisions, so they can't drift apart.
- Results and a wrap-up home page after the contest; a daily rebuild moves the site from stage to stage.
- The build fails on an empty contact email; Escape closes the phone menu.
- Ten example sites, including the real VSYC-26 contest.

