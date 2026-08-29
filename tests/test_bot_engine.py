from datetime import date

from bot_engine import (
    EventRecord,
    SourceRow,
    edgar_index_url,
    is_blocked_url,
    is_business_name,
    nyc_occupancy_class,
    parse_rss_items,
    redact_ssn,
    rss_event_from_item,
    strip_html,
)


SAMPLE_RSS = """<?xml version="1.0" encoding="ISO-8859-1"?>
<rss version="2.0">
<channel>
<title>District of Delaware - Recent Entries</title>
<item>
<title>26-10911- Nerra Inc.</title>
<link>https://ecf.deb.uscourts.gov/cgi-bin/DktRpt.pl?201578</link>
<description>Type: bk Office: 1 Chapter: 7 Trustee: Nimeroff, Jami  [Voluntary Petition (Chapter 7)] (&amp;#x3C;a href=&amp;#x27;https://ecf.deb.uscourts.gov/doc1/042023164847?caseid=201578&amp;#x27;&amp;#x3E;1&amp;#x3C;/a&amp;#x3E;)</description>
<guid isPermaLink="true">https://ecf.deb.uscourts.gov/cgi-bin/DktRpt.pl?201578-2</guid>
<pubDate>Sat, 29 Aug 2026 05:10:08 GMT</pubDate>
</item>
<item>
<title>26-10056- Debra L. Milnes</title>
<link>https://ecf.deb.uscourts.gov/cgi-bin/DktRpt.pl?200219</link>
<description>Type: bk Office: 1 Chapter: 13 Trustee: Jaworski, William F [Voluntary Petition (Chapter 13)]</description>
<guid>https://ecf.deb.uscourts.gov/cgi-bin/DktRpt.pl?200219-1</guid>
<pubDate>Sat, 29 Aug 2026 05:25:05 GMT</pubDate>
</item>
<item>
<title>26-10971- Adria M. Bondanza</title>
<link>https://ecf.deb.uscourts.gov/cgi-bin/DktRpt.pl?201691</link>
<description>Type: bk Office: 1 Chapter: 7 [Financial Management Certificate]</description>
<guid>g-3</guid>
<pubDate>Sat, 29 Aug 2026 05:13:11 GMT</pubDate>
</item>
<item>
<title>26-01047- Aristone Capital Asset Management LLC v. Kaja Holdings LLC</title>
<link>https://ecf.deb.uscourts.gov/cgi-bin/DktRpt.pl?1</link>
<description>Type: ap Office: 1 Chapter:   [Written Opinion]</description>
<guid>g-4</guid>
<pubDate>Sat, 29 Aug 2026 05:00:00 GMT</pubDate>
</item>
</channel>
</rss>
"""

DE_SOURCE = SourceRow(
    id="6231bfa5-389c-4b03-a471-1c44f2d6c309",
    slug="ecf-rss-deb",
    name="CM/ECF public RSS DE",
    source_kind="court_rss",
    access_model="public_rss",
    license_status="none",
    collection_status="approved",
    listing_url="https://ecf.deb.uscourts.gov/cgi-bin/rss_outside.pl",
    homepage_url="https://www.deb.uscourts.gov/",
    jurisdiction_state="DE",
    jurisdiction_local="District of Delaware",
    operator="US Bankruptcy Court District of Delaware",
    robots_notes="Feed XML only; never follow PACER document links",
)


def test_business_name_heuristics() -> None:
    assert is_business_name("Nerra Inc.")
    assert is_business_name("Livstreet Realty Corp.")
    assert is_business_name("20501 Linden Owner LLC")
    assert not is_business_name("Debra L. Milnes")
    assert not is_business_name("Robert J. Frost and Rashan A. Frost")


def test_ssn_redaction() -> None:
    assert redact_ssn("Jane 123-45-6789 Doe") == "Jane [REDACTED] Doe"


def test_strip_html_drops_tags_keeps_facts() -> None:
    raw = "Type: bk [Voluntary Petition] (&lt;a href='https://ecf.deb.uscourts.gov/doc1/0420'&gt;1&lt;/a&gt;)"
    text = strip_html(raw)
    assert "Voluntary Petition" in text
    assert "<a" not in text
    assert "href" not in text


def test_pacer_urls_are_blocked() -> None:
    assert is_blocked_url("https://ecf.deb.uscourts.gov/cgi-bin/DktRpt.pl?201578")
    assert is_blocked_url("https://ecf.deb.uscourts.gov/doc1/042023164847")
    assert is_blocked_url("https://ecf.nysb.uscourts.gov/doc1/1260.pdf")
    assert not is_blocked_url("https://ecf.deb.uscourts.gov/cgi-bin/rss_outside.pl")


def test_rss_keeps_only_business_petitions_and_cites_feed() -> None:
    items = parse_rss_items(SAMPLE_RSS)
    records: list[EventRecord] = []
    for item in items:
        rec = rss_event_from_item(DE_SOURCE, item)
        if rec:
            records.append(rec)
    assert len(records) == 1
    rec = records[0]
    assert rec.primary_party_name == "Nerra Inc."
    assert rec.event_type == "bk_petition_rss"
    assert rec.signal_family == "business_bankruptcy"
    assert rec.docket_or_notice_no.startswith("26-10911")
    assert rec.bankruptcy_chapter == "7"
    assert rec.source_url.startswith(DE_SOURCE.listing_url)
    assert "DktRpt" not in rec.source_url
    assert "doc1" not in rec.source_url
    assert rec.occurred_on == date(2026, 8, 29)


def test_nyc_commercial_only() -> None:
    assert nyc_occupancy_class("Commercial") == "commercial"
    assert nyc_occupancy_class("Residential") == "residential"
    assert nyc_occupancy_class("Unspecified") == "unknown"


def test_edgar_index_url_does_not_point_at_filing_body() -> None:
    url = edgar_index_url("0001159167", "0001159167-26-000006")
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/1159167/"
        "000115916726000006/0001159167-26-000006-index.htm"
    )
    assert url.endswith("-index.htm")
