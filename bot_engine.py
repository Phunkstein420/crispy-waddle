"""CRE Public Notice Signals collector.

Collects only public, license-free facts for:
  - business_bankruptcy (CM/ECF RSS petitions, EDGAR 8-K Item 1.03)
  - occupancy_distress (NYC Open Data commercial executions)

Never: PACER login, docket PDFs, doc1 links, newspaper HTML, SSNs,
lease-default letters, or license-gated FL/TX/GA press portals.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Iterable
from urllib.parse import quote, urlparse
from urllib.robotparser import RobotFileParser

import httpx
import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("cre_signals")

ALLOWED_COURT_CODES = ("deb", "nysb", "nyeb", "txsb", "cacb", "ilnb", "flsb")
COURT_RSS_PATH = "/cgi-bin/rss_outside.pl"
NYC_DATASET = "6z8x-wfk4"
NYC_API = f"https://data.cityofnewyork.us/resource/{NYC_DATASET}.json"
EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index"

# Paths we will never request (PACER docket/document surfaces).
PACER_DENY_RE = re.compile(
    r"/doc1/|/docs1/|/cgi-bin/DktRpt\.pl|\.pdf(?:$|\?)",
    re.IGNORECASE,
)
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
ENTITY_RE = re.compile(
    r"""(?ix)
    (
        \bLLC\b|\bL\.L\.C\.?\b|\bINC\.?\b|\bCORP\.?\b|\bCORPORATION\b
        |\bLTD\.?\b|\bLIMITED\b|\bL\.?P\.?\b|\bLLP\b|\bPLLC\b|\bP\.?C\.?\b
        |\bCOMPANY\b|\bHOLDINGS\b|\bPARTNERS\b|\bPARTNERSHIP\b|\bTRUST\b
        |\bPLC\b|\bN\.?A\.?\b|\bLP\b|\bCO\.\b
    )
    """
)
TITLE_RE = re.compile(
    r"^(?P<docket>\d{2}-\d{4,6}(?:-[A-Za-z0-9]+)?)[\s\-]+(?P<name>.+)$"
)
CHAPTER_RE = re.compile(r"Chapter:\s*(\d+)", re.IGNORECASE)
TYPE_RE = re.compile(r"Type:\s*([A-Za-z]+)", re.IGNORECASE)
OFFICE_RE = re.compile(r"Office:\s*(\S+)", re.IGNORECASE)
EVENT_LABEL_RE = re.compile(r"\[([^\]]+)\]")
PETITION_RE = re.compile(
    r"voluntary petition|involuntary petition|\bpetition \(chapter",
    re.IGNORECASE,
)

HOST_MIN_INTERVAL = {
    "data.cityofnewyork.us": 1.1,
    "efts.sec.gov": 1.0,
    "www.sec.gov": 1.0,
}
DEFAULT_MIN_INTERVAL = 8.0
COLLECTOR_INTERVAL_DEFAULT = 21600


@dataclass
class EventRecord:
    source_id: str
    source_event_key: str
    source_url: str
    signal_family: str
    event_type: str
    occurred_on: date | None = None
    published_on: date | None = None
    jurisdiction_state: str | None = None
    jurisdiction_local: str | None = None
    court_or_office: str | None = None
    docket_or_notice_no: str | None = None
    bankruptcy_chapter: str | None = None
    occupancy_class: str | None = None
    primary_party_name: str = ""
    primary_party_kind: str | None = "unknown"
    counterparty_name: str | None = None
    property_street: str | None = None
    property_city: str | None = None
    property_county: str | None = None
    property_state: str | None = None
    property_postal_code: str | None = None
    is_business: bool | None = True
    confidence: Decimal = Decimal("0.80")


@dataclass
class SourceRow:
    id: str
    slug: str
    name: str
    source_kind: str
    access_model: str
    license_status: str
    collection_status: str
    listing_url: str | None
    homepage_url: str
    jurisdiction_state: str | None
    jurisdiction_local: str | None
    operator: str | None
    robots_notes: str | None


def contact_email() -> str:
    return os.getenv("CONTACT_EMAIL", "dpluggs2013@yahoo.com").strip()


def user_agent() -> str:
    # SEC.gov blocks undeclared automation; they want "Name email@domain".
    return f"CREPublicNoticeSignals {contact_email()}"


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    return url


def connect() -> psycopg.Connection:
    # Transaction-mode Supavisor (port 6543) cannot reuse prepared statements.
    return psycopg.connect(
        database_url(),
        row_factory=dict_row,
        autocommit=True,
        prepare_threshold=None,
    )


def redact_ssn(text: str | None) -> str:
    if not text:
        return ""
    return SSN_RE.sub("[REDACTED]", text)


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    unescaped = html.unescape(text)
    no_tags = re.sub(r"<[^>]+>", " ", unescaped)
    return redact_ssn(re.sub(r"\s+", " ", no_tags).strip())


def char2(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^A-Za-z]", "", value).upper()
    if len(cleaned) >= 2:
        return cleaned[:2]
    return None


def is_business_name(name: str) -> bool:
    return bool(ENTITY_RE.search(name or ""))


def is_blocked_url(url: str) -> bool:
    return bool(PACER_DENY_RE.search(url or ""))


class RateLimitedFetcher:
    """HTTP GET with robots.txt, host rate limits, and a PACER deny list."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._client = httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": user_agent(), "Accept": "*/*"},
        )

    def close(self) -> None:
        self._client.close()

    def _throttle(self, host: str) -> None:
        wait = HOST_MIN_INTERVAL.get(host, DEFAULT_MIN_INTERVAL)
        if host.startswith("ecf.") and host.endswith(".uscourts.gov"):
            wait = DEFAULT_MIN_INTERVAL
        last = self._last.get(host, 0.0)
        delay = wait - (time.monotonic() - last)
        if delay > 0:
            time.sleep(delay)
        self._last[host] = time.monotonic()

    def _robots_for(self, host: str, scheme: str) -> RobotFileParser | None:
        if host in self._robots:
            return self._robots[host]
        robots_url = f"{scheme}://{host}/robots.txt"
        parser = RobotFileParser()
        try:
            self._throttle(host)
            resp = self._client.get(robots_url)
            if resp.status_code == 404:
                self._robots[host] = None
                return None
            if resp.status_code >= 400:
                # Fail closed for unknown errors except 403/401 on robots
                # (common on API hosts); treat as "no file".
                self._robots[host] = None
                return None
            parser.parse(resp.text.splitlines())
            self._robots[host] = parser
            return parser
        except httpx.HTTPError:
            self._robots[host] = None
            return None

    def allowed(self, url: str) -> bool:
        if is_blocked_url(url):
            log.info("blocked PACER/document URL (not fetched): %s", urlparse(url).path)
            return False
        parsed = urlparse(url)
        robots = self._robots_for(parsed.netloc, parsed.scheme or "https")
        if robots is None:
            return True
        return robots.can_fetch(user_agent(), url)

    def get_text(self, url: str, *, accept: str | None = None) -> str:
        if not self.allowed(url):
            raise PermissionError(f"robots or policy forbids {url}")
        parsed = urlparse(url)
        self._throttle(parsed.netloc)
        headers = {"User-Agent": user_agent()}
        if accept:
            headers["Accept"] = accept
        resp = self._client.get(url, headers=headers)
        resp.raise_for_status()
        # Never persist raw newspaper HTML; callers parse structured feeds.
        return resp.text

    def get_json(self, url: str) -> Any:
        text = self.get_text(url, accept="application/json")
        import json

        return json.loads(text)


def load_approved_sources(conn: psycopg.Connection) -> list[SourceRow]:
    rows = conn.execute(
        """
        SELECT id, slug, name, source_kind, access_model, license_status,
               collection_status, listing_url, homepage_url,
               jurisdiction_state, jurisdiction_local, operator, robots_notes
        FROM public.sources
        WHERE collection_status = 'approved'
        ORDER BY slug
        """
    ).fetchall()
    return [SourceRow(**{k: r[k] for k in SourceRow.__dataclass_fields__}) for r in rows]


def upsert_events(conn: psycopg.Connection, events: Iterable[EventRecord]) -> int:
    inserted = 0
    sql = """
        INSERT INTO public.events (
            source_id, source_event_key, source_url, signal_family, event_type,
            occurred_on, published_on, jurisdiction_state, jurisdiction_local,
            court_or_office, docket_or_notice_no, bankruptcy_chapter,
            occupancy_class, primary_party_name, primary_party_kind,
            counterparty_name, property_street, property_city, property_county,
            property_state, property_postal_code, is_business, confidence
        ) VALUES (
            %(source_id)s, %(source_event_key)s, %(source_url)s, %(signal_family)s,
            %(event_type)s, %(occurred_on)s, %(published_on)s, %(jurisdiction_state)s,
            %(jurisdiction_local)s, %(court_or_office)s, %(docket_or_notice_no)s,
            %(bankruptcy_chapter)s, %(occupancy_class)s, %(primary_party_name)s,
            %(primary_party_kind)s, %(counterparty_name)s, %(property_street)s,
            %(property_city)s, %(property_county)s, %(property_state)s,
            %(property_postal_code)s, %(is_business)s, %(confidence)s
        )
        ON CONFLICT (source_id, source_event_key) DO NOTHING
    """
    with conn.cursor() as cur:
        for event in events:
            payload = event.__dict__.copy()
            cur.execute(sql, payload)
            if cur.rowcount:
                inserted += 1
    return inserted


def parse_rss_items(xml_text: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        log.warning("RSS XML parse failed")
        return items
    channel = root.find("channel")
    if channel is None:
        return items
    for item in channel.findall("item"):
        def _text(tag: str) -> str:
            node = item.find(tag)
            return (node.text or "") if node is not None else ""

        items.append(
            {
                "title": html.unescape(_text("title")).strip(),
                "link": _text("link").strip(),
                "description": _text("description"),
                "guid": _text("guid").strip(),
                "pubDate": _text("pubDate").strip(),
            }
        )
    return items


def parse_pub_date(value: str) -> date | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.date()
    except (TypeError, ValueError):
        return None


def rss_event_from_item(source: SourceRow, item: dict[str, str]) -> EventRecord | None:
    """Map one CM/ECF RSS item. XML facts only; never follow item links."""
    description = strip_html(item.get("description", ""))
    title = redact_ssn(item.get("title", "")).strip()
    case_type = (TYPE_RE.search(description).group(1).lower() if TYPE_RE.search(description) else "")
    if case_type != "bk":
        return None
    label_match = EVENT_LABEL_RE.search(description)
    label = label_match.group(1) if label_match else ""
    if not PETITION_RE.search(label) and not PETITION_RE.search(description):
        return None
    parsed_title = TITLE_RE.match(title)
    party = (parsed_title.group("name").strip() if parsed_title else title).strip(" -")
    docket = parsed_title.group("docket") if parsed_title else None
    if not is_business_name(party):
        return None
    chapter = CHAPTER_RE.search(description)
    office = OFFICE_RE.search(description)
    pub = parse_pub_date(item.get("pubDate", ""))
    key = item.get("guid") or f"{docket}:{label}:{item.get('pubDate')}"
    feed = source.listing_url or f"https://ecf.{_court_code(source)}.uscourts.gov{COURT_RSS_PATH}"
    fragment = docket or key
    if is_blocked_url(fragment):
        fragment = docket or title[:80]
    source_url = f"{feed}#{quote(fragment, safe='-')}"
    return EventRecord(
        source_id=source.id,
        source_event_key=key[:500],
        source_url=source_url,
        signal_family="business_bankruptcy",
        event_type="bk_petition_rss",
        occurred_on=pub,
        published_on=pub,
        jurisdiction_state=char2(source.jurisdiction_state),
        jurisdiction_local=source.jurisdiction_local,
        court_or_office=source.name if not office else f"{source.name} office {office.group(1)}",
        docket_or_notice_no=docket,
        bankruptcy_chapter=chapter.group(1) if chapter else None,
        primary_party_name=party[:500],
        primary_party_kind="entity",
        is_business=True,
        confidence=Decimal("0.86"),
    )


def _court_code(source: SourceRow) -> str | None:
    if source.listing_url:
        host = urlparse(source.listing_url).hostname or ""
        parts = host.split(".")
        if len(parts) >= 3 and parts[0] == "ecf":
            return parts[1]
    slug = source.slug.replace("ecf-rss-", "")
    return slug if slug in ALLOWED_COURT_CODES else None


def collect_court_rss(fetcher: RateLimitedFetcher, source: SourceRow) -> list[EventRecord]:
    code = _court_code(source)
    if code not in ALLOWED_COURT_CODES:
        log.info("skip RSS source %s: court code not in allowlist", source.slug)
        return []
    url = source.listing_url or f"https://ecf.{code}.uscourts.gov{COURT_RSS_PATH}"
    if COURT_RSS_PATH not in url:
        log.info("skip %s: listing_url is not rss_outside.pl", source.slug)
        return []
    try:
        xml_text = fetcher.get_text(url, accept="application/rss+xml, application/xml, text/xml")
    except (httpx.HTTPError, PermissionError) as exc:
        log.warning("RSS fetch failed for %s: %s", source.slug, exc)
        return []
    events: list[EventRecord] = []
    for item in parse_rss_items(xml_text):
        # Guard: never treat the item <link> as something to fetch.
        if is_blocked_url(item.get("link", "")) or is_blocked_url(item.get("guid", "")):
            log.debug("RSS item cites PACER URL; storing feed citation only")
        record = rss_event_from_item(source, item)
        if record:
            events.append(record)
    return events


def nyc_occupancy_class(raw: str | None) -> str | None:
    value = (raw or "").strip().lower()
    if value == "commercial":
        return "commercial"
    if value == "residential":
        return "residential"
    if value:
        return "unknown"
    return None


def collect_nyc_evictions(fetcher: RateLimitedFetcher, source: SourceRow) -> list[EventRecord]:
    since = (datetime.now(timezone.utc).date() - timedelta(days=21)).isoformat()
    where = (
        "upper(residential_commercial_ind)='COMMERCIAL'"
        f" AND executed_date >= '{since}'"
    )
    events: list[EventRecord] = []
    offset = 0
    page = 100
    while offset < 500:
        url = (
            f"{NYC_API}?$limit={page}&$offset={offset}"
            f"&$order=executed_date DESC"
            f"&$where={quote(where)}"
        )
        try:
            rows = fetcher.get_json(url)
        except (httpx.HTTPError, PermissionError) as exc:
            log.warning("NYC Open Data fetch failed: %s", exc)
            break
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            occ = nyc_occupancy_class(row.get("residential_commercial_ind"))
            if occ != "commercial":
                continue
            index_no = (row.get("court_index_number") or "").strip()
            docket = (row.get("docket_number") or "").strip()
            executed = (row.get("executed_date") or "")[:10] or None
            key = f"{index_no}|{docket}|{executed}"
            if not index_no:
                continue
            street = redact_ssn(row.get("eviction_address") or "") or None
            occurred = None
            if executed:
                try:
                    occurred = date.fromisoformat(executed)
                except ValueError:
                    occurred = None
            permalink = (
                f"{NYC_API}?court_index_number={quote(index_no)}"
                f"&docket_number={quote(docket)}"
            )
            events.append(
                EventRecord(
                    source_id=source.id,
                    source_event_key=key[:500],
                    source_url=permalink,
                    signal_family="occupancy_distress",
                    event_type="occupancy_execution",
                    occurred_on=occurred,
                    published_on=occurred,
                    jurisdiction_state="NY",
                    jurisdiction_local=source.jurisdiction_local,
                    court_or_office="NYC City Marshal",
                    docket_or_notice_no=index_no or None,
                    occupancy_class="commercial",
                    primary_party_name=f"Unnamed commercial occupant ({index_no})",
                    primary_party_kind="unknown",
                    counterparty_name=None,
                    property_street=street,
                    property_city=(row.get("borough") or "").title() or None,
                    property_county=row.get("borough"),
                    property_state="NY",
                    property_postal_code=row.get("eviction_zip"),
                    is_business=True,
                    confidence=Decimal("0.90"),
                )
            )
        if len(rows) < page:
            break
        offset += page
    return events


def edgar_index_url(cik: str, adsh: str) -> str:
    cik_num = str(int(re.sub(r"\D", "", cik) or "0"))
    compact = adsh.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{compact}/{adsh}-index.htm"


def collect_edgar_8k(fetcher: RateLimitedFetcher, source: SourceRow) -> list[EventRecord]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=30)
    url = (
        f"{EDGAR_SEARCH}?q={quote('\"Item 1.03\"')}"
        f"&forms=8-K&dateRange=custom"
        f"&startdt={start.isoformat()}&enddt={end.isoformat()}&size=100"
    )
    try:
        payload = fetcher.get_json(url)
    except (httpx.HTTPError, PermissionError) as exc:
        log.warning("EDGAR search failed: %s", exc)
        return []
    hits = (((payload or {}).get("hits") or {}).get("hits")) or []
    events: list[EventRecord] = []
    seen: set[str] = set()
    for hit in hits:
        src = hit.get("_source") or {}
        items = [str(i) for i in (src.get("items") or [])]
        if "1.03" not in items:
            continue
        adsh = (src.get("adsh") or "").strip()
        if not adsh or adsh in seen:
            continue
        seen.add(adsh)
        names = src.get("display_names") or []
        party = names[0].split("  (")[0].strip() if names else "SEC registrant"
        party = redact_ssn(party)
        ciks = src.get("ciks") or ["0"]
        file_date = (src.get("file_date") or "")[:10]
        period = (src.get("period_ending") or file_date)[:10]
        try:
            published = date.fromisoformat(file_date) if file_date else None
        except ValueError:
            published = None
        try:
            occurred = date.fromisoformat(period) if period else published
        except ValueError:
            occurred = published
        states = src.get("biz_states") or src.get("inc_states") or []
        state = char2(states[0] if states else None)
        locations = src.get("biz_locations") or []
        city = None
        if locations:
            city = locations[0].split(",")[0].strip()
        events.append(
            EventRecord(
                source_id=source.id,
                source_event_key=adsh,
                source_url=edgar_index_url(ciks[0], adsh),
                signal_family="business_bankruptcy",
                event_type="bk_8k",
                occurred_on=occurred,
                published_on=published,
                jurisdiction_state=state,
                jurisdiction_local=source.jurisdiction_local,
                court_or_office="US SEC EDGAR",
                docket_or_notice_no=adsh,
                primary_party_name=party[:500],
                primary_party_kind="entity",
                property_city=city,
                property_state=state,
                is_business=True,
                confidence=Decimal("0.93"),
            )
        )
    return events


def collect_source(fetcher: RateLimitedFetcher, source: SourceRow) -> list[EventRecord]:
    if source.license_status != "none" or source.access_model == "license_required":
        log.info("catalog only (no scrape): %s", source.slug)
        return []
    if source.source_kind == "court_rss" and source.access_model == "public_rss":
        return collect_court_rss(fetcher, source)
    if source.source_kind == "open_data_api" and source.slug == "nyc-open-data-evictions":
        return collect_nyc_evictions(fetcher, source)
    if source.source_kind == "edgar" and source.slug == "sec-edgar-8k-103":
        return collect_edgar_8k(fetcher, source)
    log.info("no collector for source %s", source.slug)
    return []


def collect_once() -> dict[str, int]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    fetcher = RateLimitedFetcher()
    stats = {"sources": 0, "candidates": 0, "inserted": 0}
    try:
        with connect() as conn:
            sources = load_approved_sources(conn)
            stats["sources"] = len(sources)
            for source in sources:
                records = collect_source(fetcher, source)
                stats["candidates"] += len(records)
                if records:
                    stats["inserted"] += upsert_events(conn, records)
                log.info(
                    "source %s collected %s events",
                    source.slug,
                    len(records),
                )
    finally:
        fetcher.close()
    log.info("collect_once finished: %s", stats)
    return stats


async def run_collector_loop(stop: asyncio.Event) -> None:
    interval = int(os.getenv("COLLECTOR_INTERVAL_SECONDS", str(COLLECTOR_INTERVAL_DEFAULT)))
    # Let the HTTP health check bind before the first outbound wave.
    try:
        await asyncio.wait_for(stop.wait(), timeout=8)
        return
    except TimeoutError:
        pass
    while not stop.is_set():
        try:
            await asyncio.to_thread(collect_once)
        except Exception:
            log.exception("collector cycle failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(60, interval))
        except TimeoutError:
            continue


def main() -> None:
    collect_once()


if __name__ == "__main__":
    main()
