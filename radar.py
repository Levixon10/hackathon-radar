#!/usr/bin/env python3
"""Hackathon Radar: pull open hackathons from Unstop, Devfolio, Devpost,
HackerEarth and MLH, score them against config.toml, print the worthwhile ones.

    ./radar.py              ranked list of events above min_score
    ./radar.py --new        only events you haven't been shown before
    ./radar.py --all        everything, including what got filtered and why
    ./radar.py --why NAME   score breakdown for events whose title contains NAME
    ./radar.py --html       also rebuild index.html (the browsable page)
    ./radar.py --notify     desktop notification about events new today

On GitHub, .github/workflows/refresh.yml runs `--html` every morning and
commits index.html and seen.json; GitHub Pages serves index.html.
"""
import argparse
import html
import json
import math
import subprocess
import re
import sys
import tomllib
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEEN_FILE = HERE / "seen.json"
TEMPLATE = HERE / "template.html"
PAGE = HERE / "index.html"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/130 Safari/537.36"}
NOW = datetime.now(timezone.utc)


@dataclass
class Event:
    source: str
    id: str
    title: str
    url: str
    organiser: str = ""
    text: str = ""            # description / themes, for keyword matching
    prize_inr: float = 0
    registrations: int = 0
    online: bool = True
    city: str = ""
    country: str = "india"
    deadline: datetime | None = None
    paid: bool = False
    women_only: bool = False
    school_only: bool = False
    career_perks: bool = False
    subtype: str = ""
    score: float = 0
    reasons: list = field(default_factory=list)
    rejected: str = ""


def get_json(url, data=None):
    req = urllib.request.Request(url, headers=UA | ({"Content-Type": "application/json"} if data else {}),
                                 data=json.dumps(data).encode() if data else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def parse_dt(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or ""))


# ---------------------------------------------------------------- sources

def fetch_unstop(cfg):
    events, page = [], 1
    while True:
        d = get_json("https://unstop.com/api/public/opportunity/search-result"
                     f"?opportunity=hackathons&page={page}&per_page=50&oppstatus=open")["data"]
        for e in d["data"]:
            prizes = e.get("prizes") or []
            usd = any(p.get("currency") == "fa-dollar" for p in prizes)
            cash = sum(p.get("cash") or 0 for p in prizes) * (cfg["weights"]["usd_to_inr"] if usd else 1)
            req = e.get("regnRequirements") or {}
            elig = {f["name"] for f in e.get("filters") or [] if f.get("type") == "eligible"}
            addr = e.get("address_with_country_logo") or {}
            details = strip_html(e.get("details"))
            events.append(Event(
                source="unstop", id=str(e["id"]), title=e["title"], url=e["seo_url"],
                organiser=(e.get("organisation") or {}).get("name", ""),
                text=details + " " + " ".join(w["name"] for w in e.get("workfunction") or []),
                prize_inr=cash, registrations=e.get("registerCount") or 0,
                online=e.get("region") == "online", city=(addr.get("city") or "").lower(),
                deadline=parse_dt(req.get("end_regn_dt") or e.get("end_date")),
                paid=bool(e.get("isPaid")),
                women_only="women" in json.dumps(req.get("eligibility") or "").lower() and "female" in json.dumps(req).lower(),
                school_only=elig == {"School Students"},
                career_perks=any(p.get("pre_placement_internship") or p.get("pre_placement_opportunity") for p in prizes),
                subtype=e.get("subtype") or "",
            ))
        if page >= d["last_page"]:
            return events
        page += 1


def fetch_devfolio(cfg):
    events, start = [], 0
    while True:
        d = get_json("https://api.devfolio.co/api/search/hackathons",
                     {"type": "application_open", "from": start, "size": 50})["hits"]
        for h in d["hits"]:
            s, st = h["_source"], h["_source"].get("hackathon_setting") or {}
            faqs = " ".join(f.get("question", "") + " " + f.get("answer", "") for f in s.get("hackathon_faqs") or [])
            events.append(Event(
                source="devfolio", id=s["uuid"], title=s["name"].strip(),
                url=f"https://{st.get('subdomain') or s['slug']}.devfolio.co/",
                organiser=s.get("hosted_by") or "",
                text=(s.get("tagline") or "") + " " + (s.get("desc") or "") + " " + faqs
                     + " " + " ".join(t.get("name", "") if isinstance(t, dict) else str(t) for t in s.get("themes") or []),
                registrations=s.get("participants_count") or 0,
                online=bool(s.get("is_online")), city=(s.get("city") or "").lower(),
                country=(s.get("country") or "india").lower(),
                deadline=parse_dt(st.get("reg_ends_at")),
                women_only=bool(st.get("women_only")),
            ))
        start += 50
        if start >= d["total"]["value"]:
            return events


def devpost_end(dates):
    # "Aug 31 - Oct 23, 2026", "Oct 01 - 30, 2026" or "Oct 05, 2026"
    m = re.match(r"(\w{3}) \d+(?:, \d{4})? - (?:(\w{3}) )?(\d+), (\d{4})", dates) or \
        re.match(r"(\w{3}) (\d+), (\d{4})", dates)
    if not m:
        return None
    g = m.groups()
    month, day, year = (g[1] or g[0], g[2], g[3]) if len(g) == 4 else g
    try:
        return datetime.strptime(f"{month} {day} {year}", "%b %d %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fetch_devpost(cfg):
    events = []
    for page in range(1, 21):
        hs = get_json(f"https://devpost.com/api/hackathons?status[]=upcoming&status[]=open&page={page}")["hackathons"]
        if not hs:
            break
        for h in hs:
            raw = strip_html(h.get("prize_amount"))
            amount = float(re.sub(r"[^\d.]", "", raw) or 0)
            rate = 1 if "₹" in raw else cfg["weights"]["usd_to_inr"]
            loc = (h.get("displayed_location") or {}).get("location", "")
            deadline = devpost_end(h.get("submission_period_dates") or "")
            events.append(Event(
                source="devpost", id=str(h["id"]), title=h["title"], url=h["url"],
                organiser=h.get("organization_name") or "",
                text=" ".join(t["name"] for t in h.get("themes") or []),
                prize_inr=amount * rate, registrations=h.get("registrations_count") or 0,
                online=loc.lower() == "online", city=loc.lower(),
                country="india" if "india" in loc.lower() or loc.lower() == "online" else loc.lower(),
                deadline=deadline,
            ))
    return events


def fetch_hackerearth(cfg):
    events = []
    for h in get_json("https://www.hackerearth.com/chrome-extension/events/")["response"]:
        if h.get("challenge_type") not in (None, "hackathon") and "hackathon" not in h["url"]:
            continue
        events.append(Event(
            source="hackerearth", id=h["url"], title=h["title"], url=h["url"],
            text=strip_html(h.get("description")), deadline=parse_dt(h.get("end_utc_tz")),
            subtype="college" if h.get("college") else "",
        ))
    return events


def fetch_mlh(cfg):
    year = NOW.year + (1 if NOW.month >= 7 else 0)
    req = urllib.request.Request(f"https://www.mlh.com/seasons/{year}/events", headers=UA)
    page = urllib.request.urlopen(req, timeout=30).read().decode()
    m = re.search(r'<script data-page="app" type="application/json">(.*?)</script>', page, re.S)
    events = []
    for e in json.loads(m.group(1))["props"]["upcomingEvents"]:
        venue = e.get("venueAddress") or {}
        events.append(Event(
            source="mlh", id=e["id"], title=e["name"].strip(), url=e.get("websiteUrl") or "https://www.mlh.com" + e["url"],
            text=" ".join((e.get("customFields") or {}).get("hackathon_focus") or []),
            online=e.get("formatType") == "digital", city=(venue.get("city") or "").lower(),
            country="india" if venue.get("country") in ("IN", "India") or e.get("formatType") == "digital" else (venue.get("country") or "").lower(),
            deadline=parse_dt(e.get("startsAt")),
        ))
    return events


SOURCES = {"unstop": fetch_unstop, "devfolio": fetch_devfolio, "devpost": fetch_devpost,
           "hackerearth": fetch_hackerearth, "mlh": fetch_mlh}


# ---------------------------------------------------------------- scoring

def words_re(words):
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I) if words else None


COLLEGE = re.compile(r"\b(university|college|institute|vidyapeeth|vidyapith|school of|polytechnic|"
                     r"acm|ieee|gdg|student chapter|club)\b", re.I)
SPONSOR = re.compile(r"(powered by|presented by|sponsored by|hosted by|organi[sz]ed by|"
                     r"in (?:collaboration|partnership|association) with)\s*:?\s*(.{0,60})", re.I)
PERKS = re.compile(r"\b(ppi|ppo|pre[- ]placement|internship offer\w*|job offer\w*|"
                   r"interview opportunit\w*|fast[- ]track\w* interview\w*)\b", re.I)
# Student communities named after a company, which aren't that company.
NOT_THE_BRAND = re.compile(r"google developer (student )?(groups?|clubs?)|\bgdgs?\b|"
                           r"aws (cloud )?(clubs?|community|user group)|microsoft learn student\w*", re.I)
NOT_HACK = re.compile(r"\b(quiz|treasure hunt|paper presentation|poster|essay|case study|"
                      r"debate|bgmi|valorant|free fire|esports?)\b", re.I)


def score(e, cfg, mills):
    w = cfg["weights"]
    head = NOT_THE_BRAND.sub(" ", re.sub(r"\S+\.(app|com|io|dev|in)\b", " ", f"{e.title} {e.organiser}"))
    add = lambda pts, why: (e.reasons.append(f"{pts:+.0f} {why}"), setattr(e, "score", e.score + pts))

    if e.deadline and e.deadline < NOW:
        e.rejected = "registration closed"
    elif e.school_only:
        e.rejected = "school students only"
    elif e.women_only and cfg["skip_women_only"]:
        e.rejected = "women only"
    elif not e.online and e.country not in ("india", ""):
        e.rejected = f"in-person abroad ({e.city or e.country})"

    top, good, colleges = words_re(cfg["top_brands"]), words_re(cfg["good_brands"]), words_re(cfg["top_colleges"])
    sponsor_text = NOT_THE_BRAND.sub(" ", " ".join(m.group(2) for m in SPONSOR.finditer(e.text)))
    branded = bool(top.search(head) or good.search(head))
    if m := top.search(head):
        add(w["top_brand"], f"top organiser ({m.group(0)})")
    elif m := good.search(head):
        add(w["good_brand"], f"strong organiser ({m.group(0)})")
    elif m := top.search(sponsor_text) or good.search(sponsor_text):
        base = w["top_brand"] if top.search(m.group(0)) else w["good_brand"]
        add(base * w["sponsor_mention"], f"backed by {m.group(0)}")
    if m := colleges.search(head):
        add(w["top_college"], f"top college ({m.group(0)})")
    elif COLLEGE.search(e.organiser) or e.subtype == "college":
        add(w["generic_college"], "generic college event")

    if e.prize_inr > 0:
        pts = min(w["prize_max"], max(0, 12 * math.log10(e.prize_inr / 20000)))
        # Big "prize pools" at small unknown events are usually sponsor credits and swag.
        unproven = not branded and e.registrations < 1000
        add(pts / 2 if unproven else pts, f"prize ₹{e.prize_inr / 1e5:.1f}L" + (" (unproven, halved)" if unproven else ""))
    if e.career_perks or PERKS.search(e.text):
        add(w["career_perks"], "career perks (PPI/PPO/hiring)")
    if e.registrations > 100:
        add(min(w["popularity_max"], 4 * math.log10(e.registrations / 100)), f"{e.registrations:,} registered")
    if m := words_re(cfg["interests"]).search(f"{head} {e.text}"):
        add(w["interest_match"], f"matches interest ({m.group(0).lower()})")

    if e.online:
        add(w["online"], "online")
    elif cfg["home_city"] and cfg["home_city"] in e.city:
        add(w["offline_home_city"], f"in {e.city}")
    else:
        add(w["offline_elsewhere"], f"travel to {e.city or 'venue'}")
    if not branded and (e.source, e.organiser) in mills:
        add(w["event_mill"], f"{e.organiser.strip()} runs {mills[(e.source, e.organiser)]} events at once")
    if e.paid:
        add(w["registration_fee"], "registration fee")
    if NOT_HACK.search(e.title) or "quiz" in e.subtype:
        add(w["not_really_a_hackathon"], "not really a hackathon")
    if e.women_only:
        e.reasons.append("women only")
    if not e.rejected and e.score < cfg["min_score"]:
        e.rejected = f"score {e.score:.0f} < {cfg['min_score']}"


# ---------------------------------------------------------------- output

def dedupe(events):
    best = {}
    for e in events:
        key = re.sub(r"[^a-z0-9]", "", e.title.lower())[:40]
        if key not in best or e.score > best[key].score:
            best[key] = e
    return list(best.values())


def show(e, new):
    days = f"{(e.deadline - NOW).days}d left" if e.deadline else "no deadline"
    tag = " [NEW]" if new else ""
    print(f"\n{e.score:5.0f}  {e.title}{tag}")
    print(f"       {e.organiser or e.source} · {e.source} · {days} · {e.url}")
    print("       " + ", ".join(e.reasons))
    if e.rejected:
        print(f"       ✗ {e.rejected}")


def load_seen():
    """{source:id -> date first shown}; older versions stored a plain list."""
    if not SEEN_FILE.exists():
        return {}
    seen = json.loads(SEEN_FILE.read_text())
    return seen if isinstance(seen, dict) else dict.fromkeys(seen, "2000-01-01")


def write_page(events, keys, seen, sources):
    today = NOW.astimezone().date().isoformat()
    data = {
        "updated": NOW.astimezone().isoformat(timespec="minutes"),
        "sources": sources,
        "events": [{
            "title": e.title, "url": e.url, "organiser": e.organiser.strip(), "source": e.source,
            "score": round(e.score), "reasons": e.reasons, "rejected": e.rejected,
            "deadline": e.deadline.isoformat() if e.deadline else None,
            "online": e.online, "city": e.city.title(), "prize": round(e.prize_inr),
            "registrations": e.registrations, "new": seen.get(keys[id(e)]) == today,
        } for e in events],
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    PAGE.write_text(TEMPLATE.read_text().replace("/*__DATA__*/null", blob))


def notify(new_good):
    if not new_good:
        return
    lines = "\n".join(f"{e.score:.0f} · {e.title}" for e in new_good[:5])
    more = f"\n…and {len(new_good) - 5} more" if len(new_good) > 5 else ""
    subprocess.run(["notify-send", "-a", "Hackathon Radar", "-i", "applications-development",
                    f"{len(new_good)} new hackathon{'s' * (len(new_good) > 1)} worth a look",
                    lines + more], check=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--new", action="store_true", help="only events not shown in an earlier run")
    ap.add_argument("--all", action="store_true", help="also list filtered-out events with the reason")
    ap.add_argument("--why", metavar="NAME", help="show the breakdown for matching titles, filtered or not")
    ap.add_argument("--top", type=int, default=40, help="max events to list (default 40)")
    ap.add_argument("--html", action="store_true", help=f"rebuild {PAGE.name}")
    ap.add_argument("--notify", action="store_true", help="desktop notification for events new today")
    ap.add_argument("--config", default=HERE / "config.toml")
    args = ap.parse_args()
    cfg = tomllib.loads(Path(args.config).read_text())

    events, sources = [], {}
    with ThreadPoolExecutor() as pool:
        futures = {name: pool.submit(fn, cfg) for name, fn in SOURCES.items()}
        for name, fut in futures.items():
            try:
                got = fut.result()
                events += got
                sources[name] = len(got)
                print(f"{name}: {len(got)} open", file=sys.stderr)
            except Exception as ex:
                sources[name] = None
                print(f"{name}: failed ({ex})", file=sys.stderr)
    if not events:
        sys.exit("every source failed; is the network up?")

    # Organisers with several open events at once tend to churn out low-effort ones.
    counts = Counter((e.source, e.organiser) for e in events if e.organiser)
    mills = {k: n for k, n in counts.items() if n >= 3}
    for e in events:
        score(e, cfg, mills)
    events = sorted(dedupe(events), key=lambda e: -e.score)

    keys = {id(e): f"{e.source}:{e.id}" for e in events}
    seen = load_seen()
    shown_before = set(seen)
    good = [e for e in events if not e.rejected]
    if not (args.all or args.why):
        today = NOW.astimezone().date().isoformat()
        for e in good:
            seen.setdefault(keys[id(e)], today)
        SEEN_FILE.write_text(json.dumps(seen, indent=0, sort_keys=True))
    if args.html:
        write_page(events, keys, seen, sources)
    if args.notify:
        notify([e for e in good if seen.get(keys[id(e)]) == NOW.astimezone().date().isoformat()
                and keys[id(e)] not in shown_before])

    if args.why:
        picked = [e for e in events if args.why.lower() in e.title.lower()]
    else:
        picked = [e for e in events if args.all or not e.rejected]
        if args.new:
            picked = [e for e in picked if keys[id(e)] not in shown_before]
        picked = picked[:args.top] if not args.all else picked

    print(f"\n{len(good)} worth a look out of {len(events)} open events", file=sys.stderr)
    for e in picked:
        show(e, keys[id(e)] not in shown_before)
    if not picked and args.top:
        print("\nNothing new." if args.new else "\nNothing matched.")


if __name__ == "__main__":
    main()
