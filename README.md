# DonutFlip Connect 4 bot (create mode), public-repo edition

The PUBLIC repo holds code only. Everything personal lives in a separate PRIVATE "data" repo:
`config.json` (stake, limits), `state.enc` (encrypted login session), `stats.enc` (encrypted P/L).

## One-time setup
1. **Public repo:** push this folder, plus your books in `books/`
   (`book_12ply_distances.dat`, `book_8ply.dat`, optional `book.json`).
2. **Private data repo:** create an empty private repo (e.g. `c4-data`).
3. **Token:** Settings -> Developer settings -> fine-grained token, only that repo, *Contents: read and write*.
4. **Key:** `python login.py --genkey` -> keep the output.
5. **Secrets** (public repo -> Settings -> Secrets and variables -> Actions):
   `STATE_KEY`, `C4_DATA_REPO` (`you/c4-data`), `C4_DATA_TOKEN`, `C4_PLAYER_ID` (your account id),
   `DISCORD_WEBHOOK_URL`.
6. **Login state:** log in to donutflip.net in Chrome started with `--remote-debugging-port=9222`, then
   `STATE_KEY=... C4_DATA_REPO=you/c4-data C4_DATA_TOKEN=... python login.py --encrypt`
   (uploads `state.enc` to the private repo). Alternative: put `storage_state_main.json` in secret `STATE_JSON`.
7. Actions tab -> *connect4-bot* -> Run workflow. The first run creates `config.json` in the private repo.

## Controlling it
Edit `config.json` **in the private data repo** (github.com or the mobile app). Re-read before every game.
| key | meaning |
|---|---|
| enabled | `false` = finish the current game, then stop (hourly runs also exit at once) |
| stake | `1000`, `"5k"`, `"2.5m"` |
| seat | `auto` (API `viewer` field), or force `creator` / `opponent` |
| max_loss / max_games / min_balance | automatic stop conditions (0 = off) |
| notify_every | webhook message every N games (0 = errors only) |
| reset_pnl_token | change to any new text to reset the P/L counters |

Player id: secret `C4_PLAYER_ID` (fallback when the API has no `viewer`).
Hard stop: cancel the running workflow. Restart: Run workflow.

## What is public / what is not
Public: code, books, workflow file, and run status (success/failure, duration).
Not public: config, stake, player id, session, P/L, balance (workflow logs print no results;
details go only to your Discord webhook).

## Logged out?
The webhook says so and the bot stays quiet until the state changes. Re-run `login.py --encrypt`.

## Notes
- Never use self-hosted runners with a public repo (forks could run code on your machine).
- Cloudflare may challenge GitHub's datacenter IPs; repeated challenge errors mean this setup won't work reliably.
- Runs last ~5.6 h; the queued hourly run continues afterwards.
- Automated play against other users likely breaks the site's rules; account ban is possible.

## If Cloudflare blocks the page
The bot waits and retries with a fresh browser each time: 3, 5, 10, 20, 30, then every 60 min
(`cf_retry_minutes` in the private config.json, ±jitter). You get a webhook message on the 1st and every 3rd failed
attempt, and one when it clears. After `cf_give_up_min` (default 300) minutes blocked in a row it stops that run and the
next hourly run tries again. Blocks never count toward the "5 consecutive errors" stop.

## Unjoined listings are cancelled
If the bot stops while your game is still "Waiting for opponent" (opponent wait timeout, `enabled: false`, the 335-minute
limit, an error, or you cancel the workflow), it presses **Cancel** first: in the game window if it is open, otherwise on
the listing card in the lobby (matched by game id). It never presses "Call bot". You get a Discord message saying whether
the cancel worked. A workflow cancelled from GitHub only gives the bot a few seconds, so that path is best-effort.
