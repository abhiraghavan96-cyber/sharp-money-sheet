# Sharp Money Sheet

A daily NFL and NBA betting sheet. It prices every spread and total from the sportsbooks' consensus, adds the line move it expects (learned nightly from this season's games) and bets only where the best available price beats that fair price. Paper trading, graded against the closing line.

It runs on its own. GitHub Actions pulls fresh data every hour through the day, scores it, grades finished games and saves `data.json`. GitHub Pages serves the site. No Claude account, no sign-in and no computer left on.

## Set it up (about 10 minutes)

1. **Create a free GitHub account** at github.com/signup, or sign in to yours.
2. **Create the repository.** Click **+** (top right), then **New repository**. Name it `sharp-money-sheet`, choose **Public**, and click **Create repository**. (GitHub Pages is free only for public repositories. Nothing in here is private.)
3. **Upload the files.** On the new repository's page, click **uploading an existing file**. Unzip `sharp-money-sheet.zip` on your computer, open the folder, select everything inside it (including the `.github` folder), and drag it all onto the upload area. Click **Commit changes**.
   - Can't see `.github`? On Windows, turn on File Explorer, View, Show, Hidden items. On a Mac, press Cmd+Shift+Period in Finder.
4. **Let the job save data.** Go to **Settings**, then **Actions**, then **General**. Under **Workflow permissions**, choose **Read and write permissions** and click **Save**.
5. **Turn on the site.** Go to **Settings**, then **Pages**. Under **Build and deployment**, set **Source** to **Deploy from a branch**, branch **main**, folder **/ (root)**, and click **Save**. After a minute or two the page shows your link: `https://YOUR-USERNAME.github.io/sharp-money-sheet/`.
6. **Run it once now.** Go to the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**. Click **Update sheet**, then **Run workflow**, then the green **Run workflow** button. When it shows a green check (about a minute), refresh your site.

Share the link from step 5 with your friends. That's it.

## What runs when

| Time (Pacific) | Job |
| --- | --- |
| :13 past every hour, 7 AM to 8 PM | Refresh: lines, Kalshi prices, bets, injuries, rest, ESPN model, scores, reads, limit-order fills |
| also :43, 2 PM to 8 PM daily | Extra refreshes before NBA tip-offs |
| also :43, 8 AM to 1 PM Sundays | Extra refreshes before the NFL afternoon games |
| 11:47 PM | Grade finished games: result, closing price, CLV, limit orders, accuracy rows; then refit the model (`model.json`) |

Times shift an hour during daylight saving time, because GitHub schedules run on UTC. GitHub often starts scheduled jobs 10 to 40 minutes late. The last refresh before a game is its closing snapshot, so more refreshes near game time mean a truer closing price. Public repositories get unlimited Actions minutes.

**Once, after setting up (or at the start of a season):** Actions, Update sheet, Run workflow, choose **backfill**. It loads every finished game this season (DraftKings opening and closing lines, final scores, ESPN's pregame chance) into `hist.json` and fits the model. It takes a few minutes.

## Good to know

- **Reads.** Each game gets an automatic read built from its numbers: line moves, bets, Kalshi vs DraftKings, ESPN's model, injuries and rest. Automatic reads never lean, so they never change a score.
- **If a source fails** (a site changes its format or blocks the request), that run skips it and the page shows the data as missing. Check the **Actions** tab for a red X and its log.
- **Inactivity.** GitHub pauses scheduled jobs in a repository with no activity for 60 days. The job's own daily saves normally count as activity; if it ever pauses, the Actions tab shows a button to turn it back on.
- **Money percentages** behind paywalls are never used. Only free share-of-bets figures.
- This is paper trading for research and fun among friends, not betting advice. If gambling stops being fun, call 1-800-GAMBLER.

## Files

| Path | What it is |
| --- | --- |
| `index.html` | The site |
| `data.json` | Everything the site shows: slates, picks, record |
| `props.json` | Players, Kalshi prop ladders, team ratings for Test a bet, and closing prop prices for CLV |
| `results.json` | Final scores and box scores from the last 14 days, for grading My trades |
| `calib.json` | One row per finished game and market: each source's pre-game chance and the result (the accuracy test) |
| `hist.json` | Every finished game this season: DraftKings open and close, score, ESPN's pregame chance (training data) |
| `snaps.json`, `moves.json` | The sheet's own pre-game snapshots and where each line then closed (training data for the move model) |
| `model.json` | Market team ratings and the learned weights behind the fair price, refit nightly by `pipeline/learn.py` |
| `pipeline/run.py` | Runs one refresh or grade from start to finish |
| `pipeline/fetch.py` | Pulls ESPN, Kalshi and Action Network |
| `pipeline/merge.py`, `score.py`, `reads.py`, `grade.py`, `pack.py` | Combine, price and score, write reads, grade, save |
| `pipeline/learn.py` | Fits market ratings and the fair-price weights from the season so far |
| `.github/workflows/update.yml` | The schedule |
