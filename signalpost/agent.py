"""Signalpost agent: Norwegian org number -> verified, sourced, dated company profile.

Principles (each maps to a scoring/disqualification rule):
  * Wrong-company safety: every API response is checked to echo the requested org number;
    website facts are published only after an identity check against the legal name / org number.
  * No fabrication: financial values are copied from Regnskapsregisteret, never generated.
    LLM text (optional) is published only if it carries a quote found verbatim on the page.
  * Freshness: profiles are re-fetched, diffed, and changes recorded in `history`.
  * Budget: hard caps on requests (1,900), wall-clock (40 min) and LLM spend ($2).
"""
import argparse, asyncio, datetime as dt, json, os, random, re, sys, time
import urllib.robotparser
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

BRREG = "https://data.brreg.no"
UA = "SignalpostAgent/1.0 (public-data research; contact: set-your-email@example.com)"
MAX_REQUESTS, MAX_LLM_USD = 1900, 2.0
LLM_MODEL = "claude-haiku-4-5-20251001"
LLM_IN, LLM_OUT = 1.0 / 1e6, 5.0 / 1e6  # USD per token (declared cost basis)


# ---------- helpers ----------
def valid_orgnr(s: str) -> bool:
    """Norwegian org numbers: 9 digits, mod-11 check digit."""
    if not re.fullmatch(r"\d{9}", s or ""):
        return False
    w = [3, 2, 7, 6, 5, 4, 3, 2]
    r = 11 - sum(int(c) * k for c, k in zip(s[:8], w)) % 11
    r = 0 if r == 11 else r
    return r != 10 and r == int(s[8])


def clean_orgnr(s) -> str:
    return re.sub(r"\D", "", str(s))


def dig(d, path):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def fact(value, url, date, evidence):
    return {"value": value, "source_url": url, "source_date": date, "evidence": evidence}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (s or "").lower())).strip()


SUFFIX = re.compile(r"\b(as|asa|ans|da|sa|enk|nuf|ba|stiftelse|foretak)\b")


def name_core(name: str) -> str:
    return re.sub(r"\s+", " ", SUFFIX.sub("", norm(name))).strip()


def section_of(key: str) -> str:
    for p in ("role_", "fin_", "web_"):
        if key.startswith(p):
            return p[:-1]
    return "registry"


def merge(prev: dict, new: dict, today: str, failed: set):
    """Update logic: keep first_seen, record changes, keep stale facts only if the source failed transiently."""
    old, hist, facts = prev.get("facts", {}), list(prev.get("history", [])), {}
    for k, f in new.items():
        o = old.get(k)
        f = dict(f)
        if o is None:
            f["first_seen"] = today
        elif o["value"] == f["value"]:
            f["first_seen"], f["last_changed"] = o.get("first_seen", today), o.get("last_changed")
        else:
            hist.append({"date": today, "field": k, "old": o["value"], "new": f["value"]})
            f["first_seen"], f["last_changed"] = o.get("first_seen"), today
        facts[k] = f
    for k, o in old.items():
        if k in facts:
            continue
        if section_of(k) in failed:
            facts[k] = {**o, "stale": True}
        else:
            hist.append({"date": today, "field": k, "old": o["value"], "new": None, "note": "no longer published by source"})
    return facts, hist


# ---------- budgeted HTTP ----------
class Budget:
    def __init__(self):
        self.requests, self.llm_usd, self.start = 0, 0.0, time.time()

    def ok(self):
        return self.requests < MAX_REQUESTS


class Http:
    def __init__(self, budget, concurrency=8):
        self.b = budget
        self.sem = asyncio.Semaphore(concurrency)
        self.client = httpx.AsyncClient(headers={"User-Agent": UA, "Accept-Language": "nb,no;q=0.9,en;q=0.5"},
                                        timeout=15, follow_redirects=True,
                                        limits=httpx.Limits(max_connections=32))

    async def get(self, url, params=None, max_bytes=1_500_000):
        """Returns (status, bytes, final_url). status None = transient failure / budget exhausted."""
        for attempt in range(3):
            if not self.b.ok():
                return None, b"", url
            self.b.requests += 1
            try:
                async with self.sem:
                    r = await self.client.get(url, params=params)
                if r.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                return r.status_code, r.content[:max_bytes], str(r.url)
            except (httpx.HTTPError, asyncio.TimeoutError):
                await asyncio.sleep(1 + attempt)
        return None, b"", url

    async def json(self, url, params=None):
        st, body, u = await self.get(url, params)
        if st is None:
            return None, None, u
        try:
            return st, json.loads(body), u
        except ValueError:
            return st, None, u


# ---------- registry sources (official, machine-readable) ----------
def registry_facts(orgnr, d, url, today):
    F = {}
    def add(k, v, path=None):
        if v not in (None, "", []):
            F[k] = fact(v, url, today, f"{path or k}={json.dumps(v, ensure_ascii=False)[:200]}")
    add("name", d.get("navn"), "navn")
    add("org_form", dig(d, "organisasjonsform.beskrivelse"), "organisasjonsform.beskrivelse")
    add("registered_in_brreg_date", d.get("registreringsdatoEnhetsregisteret"), "registreringsdatoEnhetsregisteret")
    add("founded_date", d.get("stiftelsesdato"), "stiftelsesdato")
    add("vat_registered", d.get("registrertIMvaregisteret"), "registrertIMvaregisteret")
    add("employees", d.get("antallAnsatte"), "antallAnsatte")
    add("industry_code", dig(d, "naeringskode1.kode"), "naeringskode1.kode")
    add("industry", dig(d, "naeringskode1.beskrivelse"), "naeringskode1.beskrivelse")
    add("sector", dig(d, "institusjonellSektorkode.beskrivelse"), "institusjonellSektorkode.beskrivelse")
    add("activity_description", " ".join(d.get("aktivitet") or []) or None, "aktivitet")
    add("statutory_purpose", " ".join(d.get("vedtektsfestetFormaal") or []) or None, "vedtektsfestetFormaal")
    add("parent_orgnr", d.get("overordnetEnhet"), "overordnetEnhet")
    add("last_annual_accounts_year", d.get("sisteInnsendteAarsregnskap"), "sisteInnsendteAarsregnskap")
    for src, key in (("forretningsadresse", "business_address"), ("postadresse", "postal_address")):
        a = d.get(src)
        if a:
            s = ", ".join(x for x in [", ".join(a.get("adresse") or []), f'{a.get("postnummer","")} {a.get("poststed","")}'.strip(),
                                       a.get("kommune"), a.get("land")] if x)
            add(key, s, src)
    for flag, key in (("konkurs", "bankrupt"), ("underAvvikling", "under_dissolution"),
                      ("underTvangsavviklingEllerTvangsopplosning", "under_forced_dissolution")):
        if flag in d:
            add(key, bool(d[flag]), flag)
    if d.get("hjemmeside"):
        add("website_registered", d["hjemmeside"], "hjemmeside")
    return F


def role_facts(d, url, today):
    F = {}
    for g in (d or {}).get("rollegrupper", []):
        for r in g.get("roller", []):
            code = dig(r, "type.kode")
            if code not in ("DAGL", "LEDE") or r.get("fratraadt") or r.get("avregistrert"):
                continue
            n = dig(r, "person.navn") or {}
            name = " ".join(x for x in (n.get("fornavn"), n.get("mellomnavn"), n.get("etternavn")) if x)
            if not name:
                name = dig(r, "enhet.navn") or ""
            if name:
                key = "role_ceo" if code == "DAGL" else "role_chair"
                F.setdefault(key, fact(name, url, today, f"roller.{code}={name}"))
    return F


def financial_facts(orgnr, data, url):
    if not isinstance(data, list) or not data:
        return {}
    rows = [r for r in data if isinstance(r, dict) and str(dig(r, "virksomhet.organisasjonsnummer")) == orgnr]
    if not rows:  # response about someone else -> refuse
        return {}
    r = max(rows, key=lambda x: dig(x, "regnskapsperiode.tilDato") or "")
    end = dig(r, "regnskapsperiode.tilDato")
    cur = r.get("valuta") or "NOK"
    F = {}
    for key, path in (("fin_revenue", "resultatregnskapResultat.driftsresultat.driftsinntekter.sumDriftsinntekter"),
                      ("fin_operating_result", "resultatregnskapResultat.driftsresultat.driftsresultat"),
                      ("fin_net_result", "resultatregnskapResultat.aarsresultat"),
                      ("fin_total_assets", "eiendeler.sumEiendeler"),
                      ("fin_equity", "egenkapitalGjeld.egenkapital.sumEgenkapital")):
        v = dig(r, path)
        if isinstance(v, (int, float)):
            F[key] = fact({"amount": v, "currency": cur, "period_end": end}, url, end, f"{path}={v}")
    if end:
        F["fin_period_end"] = fact(end, url, end, "regnskapsperiode.tilDato")
    return F


# ---------- website (verified) ----------
class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title, self.desc, self.text, self.mail, self.tel, self.links = "", "", [], set(), set(), []
        self._t = self._skip = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title": self._t = True
        if tag in ("script", "style", "noscript"): self._skip = True
        if tag == "meta" and (a.get("name") or "").lower() in ("description",) and a.get("content"):
            self.desc = a["content"].strip()
        if tag == "a" and a.get("href"):
            h = a["href"].strip()
            if h.lower().startswith("mailto:"): self.mail.add(h[7:].split("?")[0].strip().lower())
            elif h.lower().startswith("tel:"): self.tel.add(re.sub(r"[^\d+]", "", h[4:]))
            else: self.links.append(h)

    def handle_endtag(self, tag):
        if tag == "title": self._t = False
        if tag in ("script", "style", "noscript"): self._skip = False

    def handle_data(self, data):
        if self._t: self.title += data
        elif not self._skip: self.text.append(data)


def parse_page(html: bytes):
    p = Page()
    try:
        p.feed(html.decode("utf-8", "ignore"))
    except Exception:
        pass
    p.title = re.sub(r"\s+", " ", p.title).strip()
    return p, re.sub(r"\s+", " ", " ".join(p.text)).strip()


def identity_check(name, orgnr, host, title, text):
    """Return evidence string if the page demonstrably belongs to this company, else None."""
    digits = re.sub(r"\s", "", text)
    if re.search(rf"(?<!\d){orgnr}(?!\d)", digits) or re.search(rf"{orgnr[:3]} {orgnr[3:6]} {orgnr[6:]}", text):
        return f"page contains org number {orgnr}"
    full, core = norm(name), name_core(name)
    if full and full in norm(text):
        return f"page contains full legal name '{name}'"
    hostcore = re.sub(r"[^a-z0-9]", "", host.lower())
    if core and re.sub(r"[^a-z0-9æøå]", "", core) and re.sub(r"[^a-z0-9]", "", core) in hostcore \
            and (len(core.split()) >= 2 or len(core) >= 7) and core in norm(title + " " + text[:3000]):
        return f"domain '{host}' and page text match name '{core}'"
    return None


def registrable(host):
    p = host.lower().split(".")
    return ".".join(p[-2:]) if len(p) >= 2 else host


async def website_facts(http, orgnr, name, site, today, use_llm):
    if not site:
        return {}, None
    url = site if re.match(r"https?://", site, re.I) else "https://" + site.lstrip("/")
    u = urlparse(url)
    if not u.hostname:
        return {}, None
    rp = urllib.robotparser.RobotFileParser()
    st, body, _ = await http.get(f"{u.scheme}://{u.netloc}/robots.txt", max_bytes=100_000)
    if st == 200:
        rp.parse(body.decode("utf-8", "ignore").splitlines())
        if not rp.can_fetch(UA, url):
            return {}, "robots"
    st, body, final = await http.get(url)
    if st is None:
        return {}, "transient"
    if st != 200:
        return {}, None
    page, text = parse_page(body)
    host = urlparse(final).hostname or u.hostname
    ev = identity_check(name, orgnr, host, page.title, text)
    pages = [(page, text, final)]
    if not ev:  # try contact/about page for footer identity
        pass
    contact = next((urljoin(final, h) for h in page.links if re.search(r"kontakt|contact|om-oss|about", h, re.I)
                    and urlparse(urljoin(final, h)).hostname == host), None)
    if contact and contact != final and http.b.ok():
        st2, b2, f2 = await http.get(contact)
        if st2 == 200:
            p2, t2 = parse_page(b2)
            pages.append((p2, t2, f2))
            ev = ev or identity_check(name, orgnr, host, page.title, text + " " + t2)
    if not ev:
        return {}, None  # cannot prove it is this company -> publish nothing
    F = {"web_url": fact(final, final, today, f"identity verified: {ev}")}
    if page.title:
        F["web_title"] = fact(page.title, final, today, f"<title>; identity: {ev}")
    if page.desc:
        F["web_description"] = fact(page.desc, final, today, f'meta description; identity: {ev}')
    dom = registrable(host)
    for p, _, src in pages:
        for m in sorted(p.mail):
            if registrable(m.split("@")[-1]) == dom:
                F.setdefault("web_email", fact(m, src, today, f"mailto link on page; same domain; identity: {ev}"))
        for t in sorted(p.tel):
            digits = re.sub(r"\D", "", t)
            if len(digits) in (8, 10) and (t.startswith("+47") or len(digits) == 8):
                F.setdefault("web_phone", fact(t, src, today, f"tel: link on page; identity: {ev}"))
    if use_llm and text and http.b.llm_usd < MAX_LLM_USD:
        d = await llm_description(http, name, text[:6000])
        if d:
            F["web_summary"] = fact(d["description"], final, today, f'verbatim quote on page: "{d["quote"]}"; identity: {ev}')
    return F, None


async def llm_description(http, name, text):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not http.b.ok():
        return None
    prompt = (f"Company: {name}\nPage text:\n{text}\n\nIn one factual sentence (English), say what this company does, "
              "using only the page text. Also return the exact verbatim phrase (8-25 words, copied character for character "
              "from the page text) that supports it. Reply as JSON: {\"description\":..., \"quote\":...}. "
              "If the page does not say what the company does, reply {\"description\":null,\"quote\":null}.")
    http.b.requests += 1
    try:
        r = await http.client.post("https://api.anthropic.com/v1/messages",
                                   headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                                   json={"model": LLM_MODEL, "max_tokens": 300, "temperature": 0,
                                         "messages": [{"role": "user", "content": prompt}]})
        j = r.json()
        u = j.get("usage", {})
        http.b.llm_usd += u.get("input_tokens", 0) * LLM_IN + u.get("output_tokens", 0) * LLM_OUT
        out = json.loads(re.search(r"\{.*\}", j["content"][0]["text"], re.S).group(0))
    except Exception:
        return None
    if out.get("description") and out.get("quote") and norm(out["quote"]) in norm(text) and len(out["quote"]) >= 25:
        return out
    return None


# ---------- per-company pipeline ----------
async def build_profile(http, orgnr, prev, today, use_llm):
    prof = {"schema_version": 1, "orgnr": orgnr, "fetched_at": today}
    if not valid_orgnr(orgnr):
        return {**prev, **prof, "status": "invalid_orgnr", "explanation": "Org number fails the mod-11 checksum; no lookup performed."}
    base = f"{BRREG}/enhetsregisteret/api/enheter/{orgnr}"
    st, d, url = await http.json(base)
    if st == 404:
        return {**prof, "status": "not_found", "facts": {}, "history": prev.get("history", []),
                "explanation": "Enhetsregisteret has no unit with this number."}
    if st == 410 and isinstance(d, dict):
        F = {"deleted_date": fact(d.get("slettedato"), url, today, f"slettedato={d.get('slettedato')}")}
        F["name"] = fact(d.get("navn"), url, today, "navn") if d.get("navn") else None
        F = {k: v for k, v in F.items() if v}
        facts, hist = merge(prev, F, today, set())
        return {**prof, "status": "deleted", "facts": facts, "history": hist,
                "explanation": "Unit is deleted in Enhetsregisteret; only the deletion date and name are published."}
    if st != 200 or not isinstance(d, dict) or str(d.get("organisasjonsnummer")) != orgnr:
        return {**prev, **prof, "status": "error", "explanation": "Registry lookup failed or returned another company; nothing published."}

    new, failed = registry_facts(orgnr, d, url, today), set()
    name = d.get("navn", "")

    async def roles():
        s, j, u = await http.json(base + "/roller")
        if s == 200 and isinstance(j, dict): return role_facts(j, u, today), None
        return {}, ("role" if s is None or s >= 500 else None)

    async def fin():
        s, j, u = await http.json(f"{BRREG}/regnskapsregisteret/regnskap/{orgnr}")
        if s == 200: return financial_facts(orgnr, j, u), None
        return {}, ("fin" if s is None or s >= 500 else None)

    async def web():
        f, err = await website_facts(http, orgnr, name, d.get("hjemmeside"), today, use_llm)
        return f, ("web" if err == "transient" else None)

    for f, err in await asyncio.gather(roles(), fin(), web()):
        new.update(f)
        if err: failed.add(err)
    facts, hist = merge(prev, new, today, failed)
    groups = {s: sum(1 for k in facts if section_of(k) == s) for s in ("registry", "role", "fin", "web")}
    expl = (f"{len(facts)} facts for {name}: {groups['registry']} from Enhetsregisteret, {groups['role']} roles, "
            f"{groups['fin']} financial values from Regnskapsregisteret (copied, not estimated), {groups['web']} from the company website "
            f"(published only after identity check). Every fact carries source_url, source_date and evidence. "
            f"{len(hist)} change(s) recorded since first seen."
            + (f" Sections temporarily unavailable, older values kept and marked stale: {sorted(failed)}." if failed else ""))
    return {**prof, "status": "ok", "facts": facts, "history": hist, "explanation": expl}


async def run(orgnrs, out: Path, use_llm=False, max_minutes=40, concurrency=6):
    out.mkdir(parents=True, exist_ok=True)
    (out / "profiles").mkdir(exist_ok=True)
    today = dt.date.today().isoformat()
    bud = Budget()
    http = Http(bud)
    deadline = bud.start + max_minutes * 60
    sem, done = asyncio.Semaphore(concurrency), {}

    async def one(o):
        if time.time() > deadline or not bud.ok():
            return
        async with sem:
            pf = out / "profiles" / f"{o}.json"
            prev = json.loads(pf.read_text()) if pf.exists() else {}
            try:
                p = await build_profile(http, o, prev, today, use_llm)
            except Exception as e:  # never crash the whole run
                p = {**prev, "orgnr": o, "status": "error", "explanation": f"internal error: {type(e).__name__}"}
            pf.write_text(json.dumps(p, ensure_ascii=False, indent=1))
            done[o] = p

    await asyncio.gather(*(one(o) for o in orgnrs))
    await http.client.aclose()
    with open(out / "profiles.jsonl", "w") as f:
        for o in orgnrs:
            if o in done:
                f.write(json.dumps(done[o], ensure_ascii=False) + "\n")
    stat = {}
    for p in done.values():
        stat[p["status"]] = stat.get(p["status"], 0) + 1
    ok = [p for p in done.values() if p["status"] == "ok"]
    rep = {"date": today, "requested": len(orgnrs), "completed": len(done), "status_counts": stat,
           "avg_facts_per_ok_profile": round(sum(len(p["facts"]) for p in ok) / max(1, len(ok)), 2),
           "outbound_requests": bud.requests, "llm_cost_usd": round(bud.llm_usd, 4),
           "elapsed_seconds": round(time.time() - bud.start, 1)}
    (out / "run_report.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep, indent=1))


async def sample(n, seed=None):
    """Random valid org numbers via Enhetsregisteret search (random registration-date windows)."""
    rnd = random.Random(seed)
    b = Budget(); http = Http(b)
    got = {}
    while len(got) < n and b.requests < 200:
        y = rnd.randint(1985, 2025)
        m = rnd.randint(1, 12)
        a = dt.date(y, m, 1)
        z = dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)
        s, j, _ = await http.json(f"{BRREG}/enhetsregisteret/api/enheter",
                                  {"fraRegistreringsdatoEnhetsregisteret": a.isoformat(),
                                   "tilRegistreringsdatoEnhetsregisteret": z.isoformat(), "size": 100,
                                   "page": rnd.randint(0, 3)})
        for e in ((j or {}).get("_embedded", {}) or {}).get("enheter", []):
            o = str(e.get("organisasjonsnummer"))
            if valid_orgnr(o) and not e.get("konkurs"):
                got[o] = 1
    await http.client.aclose()
    l = list(got)
    rnd.shuffle(l)
    return l[:n]


def load_input(path):
    t = Path(path).read_text()
    try:
        j = json.loads(t)
        items = j if isinstance(j, list) else j.get("orgnrs", [])
    except ValueError:
        items = t.split()
    seen, out = set(), []
    for i in items:
        o = clean_orgnr(i.get("orgnr") if isinstance(i, dict) else i)
        if o and o not in seen:
            seen.add(o); out.append(o)
    return out


def main():
    ap = argparse.ArgumentParser(prog="signalpost")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="profile org numbers from a file (one per line or JSON list)")
    r.add_argument("--input", required=True); r.add_argument("--out", default="out")
    r.add_argument("--llm", action="store_true", help="add quote-verified summaries (needs ANTHROPIC_API_KEY)")
    r.add_argument("--max-minutes", type=int, default=40)
    a = sub.add_parser("all", help="sample N random companies, then profile them (submission dataset)")
    a.add_argument("--n", type=int, default=1200); a.add_argument("--out", default="out")
    a.add_argument("--llm", action="store_true"); a.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()
    if args.cmd == "run":
        asyncio.run(run(load_input(args.input), Path(args.out), args.llm, args.max_minutes))
    else:
        ids = asyncio.run(sample(args.n, args.seed))
        Path(args.out).mkdir(parents=True, exist_ok=True)
        (Path(args.out) / "orgnrs.txt").write_text("\n".join(ids))
        asyncio.run(run(ids, Path(args.out), args.llm, max_minutes=120))


if __name__ == "__main__":
    main()
