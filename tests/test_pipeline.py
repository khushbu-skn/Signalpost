import asyncio, json, httpx
from pathlib import Path
from signalpost import agent

ORG = "923609016"
def handler(req: httpx.Request):
    u = str(req.url)
    if u.endswith(f"/enheter/{ORG}"):
        return httpx.Response(200, json={"organisasjonsnummer": ORG, "navn": "FJORD BYGG AS", "antallAnsatte": 7,
            "hjemmeside": "fjordbygg.no", "organisasjonsform": {"beskrivelse": "Aksjeselskap"},
            "forretningsadresse": {"adresse": ["Gata 1"], "postnummer": "0150", "poststed": "OSLO", "kommune": "OSLO", "land": "Norge"}})
    if u.endswith("/roller"):
        return httpx.Response(200, json={"rollegrupper": [{"roller": [{"type": {"kode": "DAGL"}, "person": {"navn": {"fornavn": "Kari", "etternavn": "Nordmann"}}}]}]})
    if "regnskapsregisteret" in u:
        return httpx.Response(200, json=[{"virksomhet": {"organisasjonsnummer": ORG}, "regnskapsperiode": {"tilDato": "2024-12-31"},
            "eiendeler": {"sumEiendeler": 1234}}])
    if u.endswith("robots.txt"): return httpx.Response(404)
    if "fjordbygg.no" in u:
        return httpx.Response(200, text="<title>Fjord Bygg</title><meta name='description' content='Bygg i Oslo'>"
            "<a href='mailto:post@fjordbygg.no'>m</a><a href='mailto:x@gmail.com'>x</a><a href='tel:+4722334455'>t</a> Fjord Bygg AS")
    return httpx.Response(404)

def test_end_to_end(tmp_path, monkeypatch):
    orig = httpx.AsyncClient
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kw: orig(transport=httpx.MockTransport(handler), **kw))
    asyncio.run(agent.run([ORG, "123456789"], tmp_path))
    ps = {json.loads(l)["orgnr"]: json.loads(l) for l in (tmp_path / "profiles.jsonl").read_text().splitlines()}
    f = ps[ORG]["facts"]
    assert f["fin_total_assets"]["value"]["amount"] == 1234 and f["role_ceo"]["value"] == "Kari Nordmann"
    assert f["web_email"]["value"] == "post@fjordbygg.no" and f["web_phone"]["value"] == "+4722334455"
    assert all(x["source_url"] and x["source_date"] and x["evidence"] for x in f.values())
    assert ps["123456789"]["status"] == "invalid_orgnr"
