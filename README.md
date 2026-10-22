# mammoth-stats

Franchise stats engine for @utmammothstats. Deterministic Python, stdlib only, data from the public NHL APIs.

- `bot.py` engine. `seed`, `check`, `replay GAMEID`, `replay-season`, `mark`, `skip`, `post`, `status`.
- `state.json` franchise ladders (franchiseId 40, Utah only), career totals, NHL all-time lists, ledgers (processed games, surfaced posts with ids, milestone alerts, recent ladder pairs).
- `ROUTINE.md` the runbook the cloud routine follows.

Design: whole-ladder diffs pre-game vs post-game, so a pass only counts when someone ahead ends up behind. Approaching-milestone alerts post at most twice per milestone (first entry into the window, then at gap 1). Totals are overwritten from NHL aggregates six hours after each final. The all-time list refreshes before any postgame pass if older than 12 hours.

Posting through the X API needs `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET` (OAuth 1.0a, Read and Write app). Until then, posting is manual and `mark` records what went out.
