# mammoth-stats

Franchise stats engine for @utmammothstats. Deterministic Python, stdlib only, data from the public NHL APIs (plus ESPN's injury report for projected lineups).

Runs on GitHub Actions every 15 minutes (`.github/workflows/bot.yml` -> `python3 bot.py tick`). Every tweet is pushed to the phone through ntfy (`NTFY_TOPIC` secret) with a one-tap compose link; posting stays manual.

What a day looks like:
- **Off day, 10am MT:** one post, the best of: top-3 NHL team ranks (PP, PK, goals for/against, shots, faceoffs), division standing, points pace vs the franchise record, and record splits (scoring first, leading after two, one-goal games, home/road, when a given player scores). Families cool down so the feed rotates.
- **Game day, 11am MT:** pregame posts from the projected lineup (last game's skaters minus anyone ESPN lists as Out, Day-To-Day or IR). When the NHL posts the official lineup, anything new goes out, and any morning tweet about a scratched player gets a "skip or delete" push.
- **During the game:** a watcher polls the gamecenter every 15s. A goal is tweetable once its scorer and assists have held for 10 minutes; then career/franchise/NHL facts, career firsts, and "Utah is W-L-OTL when X scores / when scoring first / when leading after two" go out, at most three per poll.
- **Postgame:** 10 minutes after the final, the full pass (milestones, ladders, records, streaks). Anything already pushed live is never repeated.

`bot.py` is the engine. `tick` is the scheduled entry; `check`, `live`, `daily`, `live-replay GAMEID`, `replay GAMEID`, `replay-season`, `backfill`, `status` run pieces by hand.
`state.json` holds franchise ladders, career totals, NHL all-time lists, per-game summaries, and a ledger of every tweet surfaced so none repeats.

Design: whole-ladder diffs pre-game vs post-game, so a pass only counts when someone ahead ends up behind. Totals are overwritten from NHL aggregates six hours after each final. The all-time list refreshes before any postgame pass if older than 12 hours.
