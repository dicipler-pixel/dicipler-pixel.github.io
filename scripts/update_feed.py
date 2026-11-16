#!/usr/bin/env python3
"""Build the site's generated feed files from public sources. Standard library only.

Reads
  * GitHub: public, non-fork repositories of OWNER (REST API; uses GITHUB_TOKEN if set, works anonymously)
  * Zenodo: public records whose creator has ORCID (Zenodo search API, anonymous)
Writes (next to index.html)
  latest.json        newest LATEST_N included items, for the front-page treasure box
  balls/<id>.json    included items per ball (one file per ball in atlas.json ORDER, plus balls/unsorted.json)
  search-index.json  compact list of every included item
  feed-report.json   counts, held-back items with reasons, unknown ball topics
It never edits atlas.json or index.html (except with the explicit --embed-into flag, for a manual refresh of the
page's built-in fallback copy of latest.json).

LICENSE RULE: an item is included only if its license is MIT or a Creative Commons licence (CC-*, including CC0).
GitHub repos whose licence is missing, "other" or NOASSERTION are held back unless their name is in
license-allowlist.json (a JSON list of repo names; approved by the owner by hand).

Ball mapping: a repo topic "ball-<id>" (id = a key in atlas.json ORDER) puts the repo on that ball. A Zenodo record
goes on the balls of any OWNER repo it links to; anything without a ball goes to "unsorted".
"""
import json, os, re, sys, time, html, argparse, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone

OWNER = "dicipler-pixel"
ORCID = "0009-0006-4645-5643"
LATEST_N = 20
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = "atlas-memoriae-feed (+https://dicipler-pixel.github.io/)"

def get_json(url, headers=None, tries=4):
    h = {"User-Agent": UA, "Accept": "application/json"}; h.update(headers or {})
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
                return json.load(r), dict(r.headers)
        except urllib.error.HTTPError as e:
            if e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0":
                raise SystemExit(f"GitHub rate limit reached (resets at {e.headers.get('X-RateLimit-Reset')}); nothing written")
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(int(e.headers.get("Retry-After") or 0) or 5 * (i + 1)); continue
            raise
        except urllib.error.URLError:
            if i < tries - 1: time.sleep(5 * (i + 1)); continue
            raise

def license_ok(lic):
    if not lic: return False
    u = lic.upper()
    return u == "MIT" or u.startswith("CC-") or u.startswith("CC0")

def strip_html(s, n=300):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or "")); s = re.sub(r"\s+", " ", s).strip()
    return s[:n]

def github_repos():
    hdr = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if os.environ.get("GITHUB_TOKEN"): hdr["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    out, page = [], 1
    while True:
        data, _ = get_json(f"https://api.github.com/users/{OWNER}/repos?type=owner&sort=created&per_page=100&page={page}", hdr)
        out += data
        if len(data) < 100: return out
        page += 1

def zenodo_records():
    out, page, total = [], 1, None
    q = f'metadata.creators.person_or_org.identifiers.identifier:"{ORCID}"'
    while True:  # anonymous page size is capped at 25
        url = "https://zenodo.org/api/records?" + urllib.parse.urlencode({"q": q, "size": 25, "page": page, "sort": "newest"})
        data, _ = get_json(url)
        hits = data["hits"]["hits"]; total = data["hits"]["total"]; out += hits
        if not hits or len(out) >= total: return out, total
        page += 1; time.sleep(1.5)  # stay well inside Zenodo's anonymous rate limit

def write_if_changed(path, obj):
    """Write JSON only when the content (ignoring the 'generated' timestamp) changed, so weekly runs with no news commit nothing."""
    strip = lambda o: {k: v for k, v in o.items() if k != "generated"} if isinstance(o, dict) else o
    try:
        with open(path, encoding="utf-8") as f:
            if strip(json.load(f)) == strip(obj): return False
    except (FileNotFoundError, ValueError): pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False, indent=1); f.write("\n")
    return True

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--embed-into", help="also refresh the latest-data fallback block inside this HTML file")
    args = ap.parse_args()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(os.path.join(ROOT, "atlas.json"), encoding="utf-8") as f: balls = json.load(f)["ORDER"]
    try:
        with open(os.path.join(ROOT, "license-allowlist.json"), encoding="utf-8") as f: allow = set(json.load(f))
    except FileNotFoundError: allow = set()

    repos = [r for r in github_repos() if not r.get("fork") and not r.get("private")]
    zen, zen_total = zenodo_records()
    items, held, unknown_topics = [], [], {}

    repo_balls = {}
    for r in repos:
        bs = []
        for t in r.get("topics") or []:
            if t.startswith("ball-"):
                b = t[5:]
                if b in balls: bs.append(b)
                else: unknown_topics.setdefault(t, []).append(r["name"])
        repo_balls[r["name"].lower()] = bs

    # Zenodo records, and which repos they archive
    archived_repo = {}
    for z in zen:
        m = z["metadata"]; lic = (m.get("license") or {}).get("id")
        links = [ri.get("identifier", "") for ri in m.get("related_identifiers") or []] + re.findall(rf"https?://github\.com/{OWNER}/[A-Za-z0-9._-]+", m.get("description") or "", re.I)
        rnames = sorted({mm.group(1).lower() for l in links for mm in [re.match(rf"https?://github\.com/{OWNER}/([^/#?]+)", l, re.I)] if mm})
        for rn in rnames: archived_repo.setdefault(rn, z["doi"])
        it = {"id": "zenodo-" + str(z["id"]), "kind": "zenodo", "title": m.get("title", "").strip(), "date": m.get("publication_date") or z.get("created", "")[:10],
              "url": "https://doi.org/" + z["doi"], "doi": z["doi"], "concept_doi": z.get("conceptdoi"), "license": lic, "badge": "archived",
              "repos": rnames, "balls": sorted({b for rn in rnames for b in repo_balls.get(rn, [])}) or ["unsorted"], "text": strip_html(m.get("description"))}
        if license_ok(lic): items.append(it)
        else: held.append({"name": it["title"], "kind": "zenodo", "url": it["url"], "license": lic, "reason": f"Zenodo licence '{lic}' is not MIT or CC"})

    for r in repos:
        lic = (r.get("license") or {}).get("spdx_id"); name = r["name"]
        doi = archived_repo.get(name.lower())
        it = {"id": "repo-" + name, "kind": "repo", "title": name, "date": (r.get("created_at") or "")[:10], "updated": (r.get("pushed_at") or "")[:10],
              "url": r["html_url"], "license": lic, "badge": "archived" if doi else "GitHub", "doi": doi,
              "balls": repo_balls[name.lower()] or ["unsorted"], "topics": r.get("topics") or [], "text": (r.get("description") or "")[:300]}
        if license_ok(lic): items.append(it)
        elif name in allow: it["license_note"] = "allowlisted by owner"; items.append(it)
        else: held.append({"name": name, "kind": "repo", "url": r["html_url"], "license": lic,
                           "reason": "no licence detected" if not lic else ("licence not recognised by GitHub (NOASSERTION)" if lic == "NOASSERTION" else f"licence '{lic}' is not MIT or CC")})

    items.sort(key=lambda x: (x["date"], x["id"]), reverse=True)
    card = lambda x: {k: x[k] for k in ("id", "kind", "title", "date", "url", "license", "badge", "doi", "balls") if x.get(k) is not None}
    changed = []
    latest = {"generated": now, "count": min(LATEST_N, len(items)), "items": [card(x) for x in items[:LATEST_N]]}
    if write_if_changed(os.path.join(ROOT, "latest.json"), latest): changed.append("latest.json")
    for b in balls + ["unsorted"]:
        bi = [card(x) for x in items if b in x["balls"]]
        if write_if_changed(os.path.join(ROOT, "balls", b + ".json"), {"generated": now, "ball": b, "items": bi}): changed.append(f"balls/{b}.json")
    idx = {"generated": now, "items": [{"id": x["id"], "t": x["title"], "u": x["url"], "d": x["date"], "k": x["kind"], "b": x["balls"],
            "s": " ".join([x["title"], x.get("text", ""), " ".join(x.get("topics", []))]).lower()} for x in items]}
    if write_if_changed(os.path.join(ROOT, "search-index.json"), idx): changed.append("search-index.json")
    report = {"generated": now, "sources": {"github_repos_public_nonfork": len(repos), "zenodo_records_found": len(zen), "zenodo_total_reported": zen_total},
              "included": len(items), "included_by_kind": {k: sum(1 for x in items if x["kind"] == k) for k in ("repo", "zenodo")},
              "held_back": sorted(held, key=lambda h: (h["kind"], h["name"].lower())), "unknown_ball_topics": unknown_topics,
              "allowlist": sorted(allow), "license_rule": "include only MIT or Creative Commons (CC-*, CC0); others held back unless repo name is in license-allowlist.json"}
    if write_if_changed(os.path.join(ROOT, "feed-report.json"), report): changed.append("feed-report.json")

    if args.embed_into:
        p = os.path.join(ROOT, args.embed_into); s = open(p, encoding="utf-8").read()
        block = json.dumps(latest, ensure_ascii=False).replace("</", "<\\/")
        s2, n = re.subn(r'(<script type="application/json" id="latest-data">)(.*?)(</script>)', lambda m: m.group(1) + block + m.group(3), s, count=1, flags=re.S)
        if n != 1: raise SystemExit("latest-data block not found in " + p)
        if s2 != s: open(p, "w", encoding="utf-8").write(s2); changed.append(args.embed_into + " (latest-data block)")
    print(json.dumps({"included": len(items), "held_back": len(held), "zenodo_records": len(zen), "repos": len(repos), "changed": changed}))

if __name__ == "__main__":
    main()
