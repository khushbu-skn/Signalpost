# Signalpost Agent — Norwegian company profiles you can trust

Give it Norwegian organisation numbers; it returns, for each, dated and sourced company facts.
Built to *win by not failing*: the disqualifiers (wrong-company publication, fabricated financials) are
made structurally impossible, then coverage, evidence and update-correctness are maximised.

## Run (one command)
```
pip install -r requirements.txt
python -m signalpost run --input orgnrs.txt --out out          # daily test: 100 companies
python -m signalpost all --n 1200 --out out                    # builds the 1,000+ profile submission set
```
Add `--llm` (needs `ANTHROPIC_API_KEY`) for optional quote-verified summaries. Input: one org number per line or a JSON list.
Output: `out/profiles.jsonl`, `out/profiles/<orgnr>.json`, `out/run_report.json`.

## Sources (all public, permitted, machine-readable)
| Source | Used for |
|---|---|
| Enhetsregisteret API `data.brreg.no/enhetsregisteret/api/enheter/{orgnr}` | name, form, addresses, industry, employees, VAT, founding/registration dates, status flags (bankrupt/dissolution), activity text, parent unit, website |
| `.../enheter/{orgnr}/roller` | current CEO and chair (public roles only; no birth dates) |
| Regnskapsregisteret API `data.brreg.no/regnskapsregisteret/regnskap/{orgnr}` | revenue, operating result, net result, assets, equity for the latest period |
| Company website (from the registry's own `hjemmeside`), robots.txt honoured | title, description, same-domain email, `tel:` phone |

## How each scoring dimension is handled
- **Right company / evidence (30):** every response must echo the requested org number or it is discarded; org numbers are mod-11 validated first;
  each fact stores `value, source_url, source_date, evidence` (raw field path and value). Website facts are published **only** after an identity
  proof (page shows the org number or the full legal name, or domain+name match for non-generic names). Emails must be on the site's own domain. If
  identity can't be proven, nothing from that site is published.
- **No fabrication:** financial values are copied from Regnskapsregisteret; missing fields are omitted, never estimated. LLM output (optional) must
  include a verbatim quote found in the page text, otherwise it is dropped.
- **Coverage (35):** ~20-30 facts per typical company across four independent official/verified sections, plus deleted-unit and not-found handling.
- **Updates (20):** re-running against an existing `out/` diffs new vs old: `first_seen` preserved, changes appended to `history` with old/new/date,
  removed facts logged, and facts from a *temporarily failing* section kept but flagged `stale` (a network blip never erases data).
- **Explanations (10):** every profile has a plain-language `explanation` (what was found, from where, what was withheld and why).
- **Ease of use (5):** one command, one input file, deterministic output layout, `run_report.json`.

## Limits and cost
About 5-7 requests per company, so ~700 for a 100-company day (limit 2,000; hard-stops at 1,900). Runtime is a few minutes with 6 concurrent workers
(hard stop at 40 min). Without `--llm`: **$0** in external API cost. With `--llm` (Claude Haiku 4.5, ~$1/$5 per M tokens): about $0.10 per 100
companies; hard cap $2 per run. Development cost to me: $0 beyond the model usage above.

## Model / API details
No model is required for the core agent. Optional: `claude-haiku-4-5-20251001` via `api.anthropic.com/v1/messages`, temperature 0.

## Tests
`python -m pytest` — checksum, wrong-company refusal, financial copy-not-invent, identity check, update/diff logic, and an end-to-end mocked run.
