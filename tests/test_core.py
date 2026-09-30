import asyncio
from signalpost.agent import *

def test_checksum():
    assert valid_orgnr("923609016")      # Equinor ASA
    assert valid_orgnr("974760673")      # Brønnøysundregistrene
    assert not valid_orgnr("923609017")
    assert not valid_orgnr("12345")

def test_financial_wrong_company_refused():
    data = [{"virksomhet": {"organisasjonsnummer": "111111111"}, "regnskapsperiode": {"tilDato": "2024-12-31"},
             "resultatregnskapResultat": {"aarsresultat": 5}}]
    assert financial_facts("923609016", data, "u") == {}

def test_financial_values_copied():
    data = [{"virksomhet": {"organisasjonsnummer": "923609016"}, "regnskapsperiode": {"tilDato": "2024-12-31"},
             "valuta": "NOK", "resultatregnskapResultat": {"aarsresultat": 5, "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 100}}},
             "eiendeler": {"sumEiendeler": 9}}]
    f = financial_facts("923609016", data, "u")
    assert f["fin_revenue"]["value"]["amount"] == 100 and f["fin_total_assets"]["value"]["amount"] == 9
    assert "fin_equity" not in f   # missing -> not invented

def test_identity_check():
    assert identity_check("Fjord Bygg AS", "923609016", "fjordbygg.no", "Fjord Bygg", "Kontakt Fjord Bygg AS Oslo")
    assert identity_check("Fjord Bygg AS", "923609016", "x.no", "t", "Org.nr 923 609 016")
    assert not identity_check("Norge AS", "923609016", "norge.no", "Norge", "Velkommen til Norge")  # generic -> refuse
    assert not identity_check("Fjord Bygg AS", "923609016", "other.no", "Other", "Something else")

def test_merge_records_changes_and_keeps_stale():
    prev = {"facts": {"employees": {"value": 5, "first_seen": "2026-01-01"}, "fin_revenue": {"value": 1}}, "history": []}
    new = {"employees": fact(7, "u", "2026-09-30", "e")}
    facts, hist = merge(prev, new, "2026-09-30", {"fin"})
    assert facts["employees"]["last_changed"] == "2026-09-30" and hist[0]["old"] == 5
    assert facts["fin_revenue"]["stale"] is True
    facts2, hist2 = merge(prev, new, "2026-09-30", set())
    assert "fin_revenue" not in facts2 and hist2[-1]["new"] is None

def test_llm_quote_must_be_verbatim():
    class B: requests=0; llm_usd=0
    assert norm("we build boats") in norm("Hello. We build boats! Since 1990")
