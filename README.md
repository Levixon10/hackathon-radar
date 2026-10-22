# Hackathon Radar

Pulls every open hackathon from Unstop, Devfolio, Devpost, HackerEarth and MLH,
scores them, and keeps the ones worth entering: big-company organisers, real prize
money, internship/PPO offers, AI/ML topics, online. College fests, registration
fees, quiz-style "hackathons" and organisers churning out events get marked down.

The page is rebuilt every morning at 08:00 IST by a GitHub Action and served on GitHub Pages.

- Tune what counts as good: `config.toml`
- Run it locally: `python3 radar.py` (add `--html` to rebuild `index.html`, `--why NAME` to see a score breakdown)
- Refresh now: Actions → Refresh → Run workflow

No dependencies beyond Python 3.11+.
