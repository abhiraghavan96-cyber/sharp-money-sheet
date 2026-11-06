# Sharp Money Sheet

A daily NFL and NBA betting sheet: DraftKings lines, Kalshi prices, public share of bets, rest, injuries and ESPN's model, scored into one best bet a day, with a paper-trading record graded against the closing line.

It runs on its own. GitHub Actions pulls fresh data three times a day, scores it, grades finished games and saves `data.json`. GitHub Pages serves the site. No Claude account, no sign-in and no computer left on.

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
| 9:13 AM | Refresh: lines, Kalshi prices, bets, injuries, rest, ESPN model, scores, reads |
| 3:13 PM | Refresh again, closer to game time |
| 11:47 PM | Grade finished games: result, closing price, CLV |

Times shift an hour during daylight saving time, because GitHub schedules run on UTC. GitHub can start scheduled jobs a few minutes late on busy days.

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
| `pipeline/run.py` | Runs one refresh or grade from start to finish |
| `pipeline/fetch.py` | Pulls ESPN, Kalshi and Action Network |
| `pipeline/merge.py`, `score.py`, `reads.py`, `grade.py`, `pack.py` | Combine, score, write reads, grade, save |
| `.github/workflows/update.yml` | The schedule |
