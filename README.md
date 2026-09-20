# US Day-Trade Desk – auto-updating

Every weekday at **09:00 Dubai time** a GitHub Action:
1. asks Claude (with web search) for the day's news, earnings, IPOs, analyst calls, macro events and sentiment, and for candidate stocks;
2. checks every candidate against real prices (yfinance): price, volume, volatility;
3. computes entry / stop / exit from those prices with fixed ATR rules (the model never sets price levels);
4. writes `data.json`, redeploys the web app, and (optional) emails the brief.

If validation fails (too few valid picks, no data), the old brief stays live and the run shows as failed.

## One-time setup (about 15 minutes, on a computer)

1. **Create a public repo** on GitHub (free Pages needs public). Unzip this package and upload **everything**, including the `.github` folder (hidden on some systems; if needed use *Add file > Create new file* and type `.github/workflows/daily.yml`, then paste the file).
2. **Settings > Pages > Source: GitHub Actions.**
3. **Anthropic API key:** create one at console.anthropic.com (billing required; web search must be enabled for your organisation; set a monthly spend limit). Then in the repo: *Settings > Secrets and variables > Actions > New repository secret* → `ANTHROPIC_API_KEY`.
4. **Optional email** (secrets): `SMTP_HOST` (e.g. smtp.gmail.com), `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASS` (an app password, not your normal password), `MAIL_TO` (kamran@shamz.com), `MAIL_FROM`. Microsoft 365 accounts often block SMTP sign-in; a Gmail app password or a transactional service is easier.
5. **First run:** *Actions > Daily brief > Run workflow* (leave force = true). When it finishes, the app URL is shown on the *deploy* job (`https://<user>.github.io/<repo>/`).
6. **Install on phone:** iPhone Safari: Share > Add to Home Screen. Android Chrome: Install app.

Optional repository variable `ANTHROPIC_MODEL` to choose the model (default `claude-sonnet-5`).

## How levels are set (all from real prices)
- Entry: a dip 0.15–0.5 ATR below the last close, or a break above the 5-day high.
- Stop: 0.6 ATR below the entry midpoint. Exits: 1.0 ATR and 1.6 ATR above it (about 1.7R and 2.7R).
- Filters: price at least $1, average volume 1M+ (2M+ for low-priced), daily range 0.5–15% of price.
- Low-priced picks are stocks under $10; the app halves the position size for them.

## Limits you should know
- **The stock selection is an AI's reading of news, not vetted research.** Read the sources linked on each card before trading. It can be wrong.
- Levels are mechanical, not a view on each chart; breakout entries have worse reward-to-risk than pullback entries.
- yfinance is an unofficial data source and can break or lag. Consider a paid data API for anything serious.
- GitHub scheduled runs can start late (5–30+ minutes), and GitHub may pause schedules after 60 days without repository activity (the daily data commits normally count).
- The site is public. It shows specific stock levels, so check your employer's outside-activity rules and local securities rules before sharing widely. Cloudflare Pages + Access can add a login.
- Half-day and holiday sessions are handled only by the NYSE holiday list.
- Cost: one Claude call with up to 15 web searches per run. Check current API pricing; expect cents to about a dollar per run.

Not investment advice. Day trading and penny stocks carry a high risk of loss.
