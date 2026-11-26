# Animal Welfare Science – Job Board

Part of the **Connect** pillar of the AWS Toolbox on sebastiengoumon.com.

A GitHub Action runs every morning. It collects vacancies from the sources in `sources.yaml`, keeps only open ones, and writes `docs/jobs.json`. GitHub Pages serves `docs/index.html`, which is embedded in Google Sites.

## Set-up (one time, about 15 minutes)

1. **Create the repo.** On GitHub, create a new public repository `sgoumon/welfare-jobs` and upload every file from this folder, keeping the folder structure (`.github/workflows/`, `docs/`, `scripts/`).
   *Hidden folder tip:* `.github` starts with a dot, so your computer may hide it. If the upload misses it, use **Add file → Create new file** on GitHub, type `.github/workflows/update-jobs.yml` as the name, and paste the file's contents.
2. **Allow the Action to save results.** Go to Settings → Actions → General → Workflow permissions, choose **Read and write permissions**, and click Save.
3. **Turn on the website.** Go to Settings → Pages. Under Source, choose *Deploy from a branch*, then branch `main` and folder `/docs`, and click Save. After a minute your page is at `https://sgoumon.github.io/welfare-jobs/`.
4. **First run.** Open the Actions tab, choose **Update job listings**, then **Run workflow**. After 1–3 minutes, refresh the page.
5. **Embed in Google Sites.** Open the toolbox page and choose Insert → Embed → *By URL*. Paste `https://sgoumon.github.io/welfare-jobs/`, choose *Whole page*, then resize the box.

## Sources and the review step

Sources whose structure I had checked go straight to the public list: ISAE, AWRN jobs, AWRN PhDs, Animal Advocacy Careers (RSS), and MPI Animal Behavior (RSS).

Every other source is marked `trial: true`. Its results go to a separate review list that isn't linked from anywhere: **`https://sgoumon.github.io/welfare-jobs/?data=trial.json`**.

After the first run:
1. Open the review list. For each trial source, ask whether the listings are real, open positions.
2. **Good:** delete the `trial: true` line for that source in `sources.yaml`. It joins the public list on the next run.
3. **Junk** (menu links, news items): add a `link_pattern:` (a pattern the advert URLs share, e.g. `/job/`) or an `exclude_pattern:`, or set `enabled: false`.
4. **Zero results:** the page probably loads its jobs with JavaScript. Check whether the site has an RSS feed, or switch the source off.

The run log (Actions tab → latest run → "Run python scripts/fetch_jobs.py") lists each source with "N found, M kept" and the first five titles.

## Adding sources

- **jobs.ac.uk:** open their subject feed pages (Agriculture, Food & Veterinary; Biological Sciences), right-click an "RSS Feed" link, choose *Copy link*, paste it into `sources.yaml`, and set `enabled: true`. Any other RSS feed works the same way (`kind: rss`).
- **Google Form for submissions:** create a Form with the questions *Job title, Employer, Country, Link to advert, Application deadline (date), Position type*. In the response Sheet, go to File → Share → Publish to web, pick the responses tab, choose CSV, and publish. Paste that link into the `Submitted` source and enable it. Then paste the Form's own link into `SUBMIT_FORM_URL` at the bottom of `docs/index.html`. Spam goes in automatically, so check new submissions now and then; you can delete rows in the Sheet.
- `filter: true` keeps only posts that match `keywords`. Use it for broad feeds. Curated welfare lists (ISAE, AWRN) use `filter: false`.

## Update schedule

The Action runs once a day at 05:17 UTC, which is 06:17 CET in winter and 07:17 CEST in summer. You can change this in `.github/workflows/update-jobs.yml`. GitHub can delay scheduled runs, or occasionally skip one, when its servers are busy.

GitHub also switches off scheduled runs in a public repo after 60 days without activity. The bot commits every day, which should prevent this, but I'm not certain bot commits count. If the page shows the yellow "not refreshed" warning, go to Actions → Update job listings. If you see a "disabled" banner, click **Enable workflow**, then **Run workflow**.

## When something breaks

- If one source fails, its listings from the previous day are kept, and the footer shows it as "temporarily unavailable". The other sources carry on.
- ISAE and AWRN are read from their web pages, so a redesign of either site can break that source. The run log (Actions tab → latest run) shows which source failed.
- Deadlines are found only when the advert says something like *closing date / deadline / apply by* followed by a date. Otherwise the listing shows "Deadline not stated", and it is removed 90 days after it first appears.

## Courtesy

The script identifies itself and pauses between requests. ISAE and AWRN curate their lists by hand, so ask them before going live. Every listing credits its source and links to the original advert.
