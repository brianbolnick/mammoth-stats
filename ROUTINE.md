# Routine runbook: Mammoth stats bot

Every number comes from `bot.py`. You never compute, round, reword, or add a statistic.

Each fire:

1. `git pull --ff-only`
2. `python3 bot.py check`
3. If the output starts with `No new posts`, stay silent: no notification. Commit and push state.json (step 5) and stop.
4. Otherwise send the output VERBATIM as a push notification, with nothing added, removed, or reworded. If it is too long for one notification, send it in several, splitting only between tweets.
5. `git add state.json && git commit -m "check" && git push`. If the push fails, retry once.

If the script errors, send a push notification with the error's last line and stop. Do not edit `bot.py` or `state.json` by hand. Do not post to X; Brian posts by hand.
