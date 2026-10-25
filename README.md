# Live Indian IPO GMP Tracker (free, no server)

A website that refreshes itself. GitHub runs the scraper every 30 minutes and
saves the result to `data.json`; the web page reads that file and shows every
open, closed-awaiting-listing and upcoming IPO with its expected return.

## Setup (about 10 minutes, all in the browser)

1. Create a free account at github.com.
2. Create a **new public repository** (e.g. `ipo-gmp-live`).
3. Upload everything from this folder, including the hidden `.github` folder
   (drag the whole contents into "Add file → Upload files").
4. **Settings → Pages**: Source = "Deploy from a branch", Branch = `main`, folder = `/ (root)`. Save.
5. **Settings → Actions → General → Workflow permissions**: choose "Read and write permissions". Save.
6. **Actions tab → "Update IPO GMP data" → Run workflow** once to test.
   A green tick means it works. After that it runs on its own every 30 minutes.
7. Your site: `https://YOUR-USERNAME.github.io/ipo-gmp-live/`

## If the update fails
The scraper reads InvestorGain's table. If the site changes its layout, the run
fails (red cross in the Actions tab) and the website keeps showing the last good
data, with a warning once it is more than 3 hours old. Open the failed run to
see the error, and the fix is usually a small edit in `scraper.py`.

## Notes
- GMP is unofficial. Check the source site's terms before using the data publicly.
- Schedule is set in `.github/workflows/update.yml` (`cron`).
