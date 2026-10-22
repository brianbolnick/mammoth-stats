# mammoth-stats

Franchise stats engine for @utmammothstats. Deterministic Python, stdlib only, data from the public NHL APIs.

- `bot.py` engine. `check` prints new tweets ready to copy; `seed`, `replay GAMEID`, `replay-season`, `status` for setup and verification.
- `state.json` franchise ladders, career totals, NHL all-time lists, and a record of tweets already sent so none repeat.
- `ROUTINE.md` the runbook the cloud routine follows: run `check`, push any new tweets to Brian's phone, commit state.

Design: whole-ladder diffs pre-game vs post-game, so a pass only counts when someone ahead ends up behind. Totals are overwritten from NHL aggregates six hours after each final. The all-time list refreshes before any postgame pass if older than 12 hours.
