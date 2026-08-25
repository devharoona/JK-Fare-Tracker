
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup


APP_NAME = "jk-fare-tracker"
DB_PATH = Path("jk_fares.sqlite3")

REQUEST_TIMEOUT = 30
USER_AGENT = (
    "JKFareTracker/1.0 "
    "(public transport fare monitoring; contact administrator)"
)

OFFICIAL_SOURCES = {
    "jkrtc_passenger_fares": (
        "https://jksrtc.co.in/passenger.php"
    ),
    "jkrtc_timetable_fares": (
        "https://jksrtc.co.in/timetablejammu.php"
    ),
    "jkrtc_home": (
        "https://jksrtc.co.in/"
    ),
}

GOVERNMENT_REFERENCE = {
    "2026_fare_revision": {
        "effective_reference": "2026-04-29",
        "increase_percent": Decimal("18"),
        "authority": "Government of Jammu and Kashmir, Transport Department",
        "legal_basis": "Motor Vehicles Act, 1988, Section 67",
        "previous_notification": "SRO-97 dated 2021-03-19",
        "scope": [
            "big buses",
            "medium buses",
            "mini buses",
            "taxi/maxi cabs",
            "petrol auto-rickshaws",
            "Tata Magic stage carriages",
        ],
    },
    "electric_fares": {
        "e_rickshaw_per_km": Decimal("15"),
        "e_auto_first_km": Decimal("25"),
        "e_auto_subsequent_km": Decimal("20"),
    },
    "observed_srinagar_auto": {
        "first_2_km": Decimal("46"),
        "subsequent_km": Decimal("20"),
        "waiting_per_hour": Decimal("25"),
    },
}


@dataclass(frozen=True)
class FareRecord:
    source: str
    authority: str
    mode: str
    vehicle_class: str
    origin: str
    destination: str
    fare: Decimal
    currency: str
    unit: str
    observed_at: str
    source_url: str
    source_hash: str
    status: str = "ACTIVE"


class FareDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS source_snapshots (
                source TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                content TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fares (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                authority TEXT NOT NULL,
                mode TEXT NOT NULL,
                vehicle_class TEXT NOT NULL,
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                fare TEXT NOT NULL,
                currency TEXT NOT NULL,
                unit TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                status TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_fares_route
                ON fares(origin, destination);

            CREATE INDEX IF NOT EXISTS idx_fares_mode
                ON fares(mode);

            CREATE INDEX IF NOT EXISTS idx_fares_observed
                ON fares(observed_at);

            CREATE TABLE IF NOT EXISTS change_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                previous_hash TEXT,
                current_hash TEXT NOT NULL,
                change_type TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def get_snapshot(self, source: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM source_snapshots WHERE source = ?",
            (source,),
        ).fetchone()

    def save_snapshot(
        self,
        source: str,
        url: str,
        content_hash: str,
        content: str,
        fetched_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO source_snapshots
                (source, url, content_hash, fetched_at, content)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                url = excluded.url,
                content_hash = excluded.content_hash,
                fetched_at = excluded.fetched_at,
                content = excluded.content
            """,
            (
                source,
                url,
                content_hash,
                fetched_at,
                content,
            ),
        )
        self.connection.commit()

    def record_change(
        self,
        source: str,
        previous_hash: str | None,
        current_hash: str,
        change_type: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO change_events
                (source, detected_at, previous_hash,
                 current_hash, change_type)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                source,
                utc_now(),
                previous_hash,
                current_hash,
                change_type,
            ),
        )
        self.connection.commit()

    def insert_fares(self, fares: Iterable[FareRecord]) -> None:
        rows = [
            (
                fare.source,
                fare.authority,
                fare.mode,
                fare.vehicle_class,
                fare.origin,
                fare.destination,
                str(fare.fare),
                fare.currency,
                fare.unit,
                fare.observed_at,
                fare.source_url,
                fare.source_hash,
                fare.status,
            )
            for fare in fares
        ]

        self.connection.executemany(
            """
            INSERT INTO fares (
                source,
                authority,
                mode,
                vehicle_class,
                origin,
                destination,
                fare,
                currency,
                unit,
                observed_at,
                source_url,
                source_hash,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.connection.commit()

    def current_fares(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT *
            FROM fares
            WHERE status = 'ACTIVE'
            ORDER BY mode, origin, destination, vehicle_class
            """
        ).fetchall()

    def changes(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT *
            FROM change_events
            ORDER BY detected_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def close(self) -> None:
        self.connection.close()


class SourceFetcher:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/xml;q=0.9,*/*;q=0.8"
                ),
            }
        )

    def fetch(self, url: str) -> tuple[str, str]:
        response = self.session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()

        content = response.text
        digest = hashlib.sha256(
            content.encode("utf-8", errors="replace")
        ).hexdigest()

        return content, digest


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def clean_money(value: str) -> Decimal | None:
    value = value.replace(",", "")
    value = value.replace("₹", "")
    value = value.replace("Rs.", "")
    value = value.replace("Rs", "")
    value = value.strip()

    if not value:
        return None

    match = re.search(r"\d+(?:\.\d+)?", value)

    if not match:
        return None

    try:
        return Decimal(match.group())
    except InvalidOperation:
        return None


def normalize_route(value: str) -> str:
    value = normalize_space(value)
    value = value.replace("\xa0", " ")
    return value.title()


def parse_jkrtc_passenger_page(
    html: str,
    source_url: str,
    source_hash: str,
) -> list[FareRecord]:
    soup = BeautifulSoup(html, "html.parser")
    observed_at = utc_now()
    records: list[FareRecord] = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")

        if not rows:
            continue

        headers = [
            normalize_space(cell.get_text(" ", strip=True)).lower()
            for cell in rows[0].find_all(["th", "td"])
        ]

        if "from" not in headers or "to" not in headers:
            continue

        header_index = {
            header: index
            for index, header in enumerate(headers)
        }

        from_index = header_index.get("from")
        to_index = header_index.get("to")

        if from_index is None or to_index is None:
            continue

        for row in rows[1:]:
            cells = [
                normalize_space(cell.get_text(" ", strip=True))
                for cell in row.find_all(["td", "th"])
            ]

            if len(cells) <= max(from_index, to_index):
                continue

            origin = normalize_route(cells[from_index])
            destination = normalize_route(cells[to_index])

            if not origin or not destination:
                continue

            for index, header in enumerate(headers):
                if index in (from_index, to_index):
                    continue

                if index >= len(cells):
                    continue

                fare = clean_money(cells[index])

                if fare is None:
                    continue

                vehicle_class = normalize_space(header)

                if not vehicle_class:
                    vehicle_class = "unspecified"

                records.append(
                    FareRecord(
                        source="JKRTC",
                        authority=(
                            "Jammu & Kashmir Road Transport "
                            "Corporation"
                        ),
                        mode="bus",
                        vehicle_class=vehicle_class,
                        origin=origin,
                        destination=destination,
                        fare=fare,
                        currency="INR",
                        unit="per passenger",
                        observed_at=observed_at,
                        source_url=source_url,
                        source_hash=source_hash,
                    )
                )

    return deduplicate_records(records)


def parse_jkrtc_timetable_page(
    html: str,
    source_url: str,
    source_hash: str,
) -> list[FareRecord]:
    soup = BeautifulSoup(html, "html.parser")
    text = normalize_space(soup.get_text(" ", strip=True))

    records: list[FareRecord] = []

    match = re.search(
        r"19\s*seater\s*\(AC\)\s*"
        r".{0,80}?"
        r"(?:Rs\.?|₹)\s*([\d.]+)",
        text,
        re.IGNORECASE,
    )

    if match:
        records.append(
            FareRecord(
                source="JKRTC",
                authority=(
                    "Jammu & Kashmir Road Transport Corporation"
                ),
                mode="bus",
                vehicle_class="19-seater AC",
                origin="Jammu",
                destination="Srinagar",
                fare=Decimal(match.group(1)),
                currency="INR",
                unit="per passenger",
                observed_at=utc_now(),
                source_url=source_url,
                source_hash=source_hash,
            )
        )

    match = re.search(
        r"34\s*seater\s*\(Non-AC\)\s*"
        r".{0,80}?"
        r"(?:Rs\.?|₹)\s*([\d.]+)",
        text,
        re.IGNORECASE,
    )

    if match:
        records.append(
            FareRecord(
                source="JKRTC",
                authority=(
                    "Jammu & Kashmir Road Transport Corporation"
                ),
                mode="bus",
                vehicle_class="34-seater Non-AC",
                origin="Jammu",
                destination="Srinagar",
                fare=Decimal(match.group(1)),
                currency="INR",
                unit="per passenger",
                observed_at=utc_now(),
                source_url=source_url,
                source_hash=source_hash,
            )
        )

    for seat_class in ("42-seater Non-AC", "49-seater Non-AC"):
        pattern = (
            re.escape(seat_class.split("-")[0])
            + r"\s*seater\s*\(Non-AC\)\s*"
            r".{0,80}?"
            r"(?:Rs\.?|₹)\s*([\d.]+)"
        )

        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            records.append(
                FareRecord(
                    source="JKRTC",
                    authority=(
                        "Jammu & Kashmir Road Transport "
                        "Corporation"
                    ),
                    mode="bus",
                    vehicle_class=seat_class,
                    origin="Jammu",
                    destination="Srinagar",
                    fare=Decimal(match.group(1)),
                    currency="INR",
                    unit="per passenger",
                    observed_at=utc_now(),
                    source_url=source_url,
                    source_hash=source_hash,
                )
            )

    return deduplicate_records(records)


def deduplicate_records(
    records: Iterable[FareRecord],
) -> list[FareRecord]:
    unique: dict[tuple[str, ...], FareRecord] = {}

    for record in records:
        key = (
            record.source,
            record.mode,
            record.vehicle_class,
            record.origin,
            record.destination,
            str(record.fare),
            record.unit,
        )
        unique[key] = record

    return list(unique.values())


def government_reference_records() -> list[FareRecord]:
    now = utc_now()

    records = [
        FareRecord(
            source="J&K Transport Department",
            authority="Government of Jammu and Kashmir",
            mode="electric_auto",
            vehicle_class="E-Rickshaw",
            origin="Jammu & Kashmir",
            destination="Jammu & Kashmir",
            fare=Decimal("15"),
            currency="INR",
            unit="per km",
            observed_at=now,
            source_url=(
                "https://transport.jk.gov.in/"
            ),
            source_hash="government-notification-2026",
        ),
        FareRecord(
            source="J&K Transport Department",
            authority="Government of Jammu and Kashmir",
            mode="electric_auto",
            vehicle_class="E-Auto",
            origin="Jammu & Kashmir",
            destination="Jammu & Kashmir",
            fare=Decimal("25"),
            currency="INR",
            unit="first km",
            observed_at=now,
            source_url=(
                "https://transport.jk.gov.in/"
            ),
            source_hash="government-notification-2026",
        ),
        FareRecord(
            source="J&K Transport Department",
            authority="Government of Jammu and Kashmir",
            mode="electric_auto",
            vehicle_class="E-Auto",
            origin="Jammu & Kashmir",
            destination="Jammu & Kashmir",
            fare=Decimal("20"),
            currency="INR",
            unit="each subsequent km",
            observed_at=now,
            source_url=(
                "https://transport.jk.gov.in/"
            ),
            source_hash="government-notification-2026",
        ),
        FareRecord(
            source="J&K Transport Department",
            authority="Government of Jammu and Kashmir",
            mode="auto_rickshaw",
            vehicle_class="Petrol Auto-Rickshaw",
            origin="Srinagar",
            destination="Srinagar",
            fare=Decimal("46"),
            currency="INR",
            unit="first 2 km",
            observed_at=now,
            source_url=(
                "https://transport.jk.gov.in/"
            ),
            source_hash="government-rate-reference-2026",
        ),
        FareRecord(
            source="J&K Transport Department",
            authority="Government of Jammu and Kashmir",
            mode="auto_rickshaw",
            vehicle_class="Petrol Auto-Rickshaw",
            origin="Srinagar",
            destination="Srinagar",
            fare=Decimal("20"),
            currency="INR",
            unit="each subsequent km",
            observed_at=now,
            source_url=(
                "https://transport.jk.gov.in/"
            ),
            source_hash="government-rate-reference-2026",
        ),
        FareRecord(
            source="J&K Transport Department",
            authority="Government of Jammu and Kashmir",
            mode="auto_rickshaw",
            vehicle_class="Petrol Auto-Rickshaw",
            origin="Srinagar",
            destination="Srinagar",
            fare=Decimal("25"),
            currency="INR",
            unit="waiting per hour",
            observed_at=now,
            source_url=(
                "https://transport.jk.gov.in/"
            ),
            source_hash="government-rate-reference-2026",
        ),
    ]

    return records


def calculate_2026_hiked_fare(
    previous_fare: Decimal,
) -> Decimal:
    return (
        previous_fare * Decimal("1.18")
    ).quantize(Decimal("0.01"))


def fetch_and_store_jkrtc(db: FareDatabase) -> dict:
    fetcher = SourceFetcher()
    result = {
        "sources": [],
        "records": 0,
        "changes": 0,
    }

    for name, url in OFFICIAL_SOURCES.items():
        try:
            html, content_hash = fetcher.fetch(url)
        except requests.RequestException as exc:
            result["sources"].append(
                {
                    "source": name,
                    "status": "ERROR",
                    "error": str(exc),
                }
            )
            continue

        previous = db.get_snapshot(name)
        previous_hash = (
            previous["content_hash"]
            if previous is not None
            else None
        )

        if previous_hash is None:
            change_type = "INITIAL"
        elif previous_hash != content_hash:
            change_type = "SOURCE_CHANGED"
        else:
            change_type = "UNCHANGED"

        if change_type != "UNCHANGED":
            db.record_change(
                source=name,
                previous_hash=previous_hash,
                current_hash=content_hash,
                change_type=change_type,
            )

        db.save_snapshot(
            source=name,
            url=url,
            content_hash=content_hash,
            content=html,
            fetched_at=utc_now(),
        )

        records: list[FareRecord] = []

        if name == "jkrtc_passenger_fares":
            records = parse_jkrtc_passenger_page(
                html,
                url,
                content_hash,
            )

        elif name == "jkrtc_timetable_fares":
            records = parse_jkrtc_timetable_page(
                html,
                url,
                content_hash,
            )

        db.insert_fares(records)

        result["records"] += len(records)

        if change_type != "UNCHANGED":
            result["changes"] += 1

        result["sources"].append(
            {
                "source": name,
                "status": change_type,
                "records": len(records),
                "sha256": content_hash,
                "fetched_at": utc_now(),
            }
        )

    return result


def seed_government_rates(db: FareDatabase) -> None:
    records = government_reference_records()
    db.insert_fares(records)


def print_fares(db: FareDatabase) -> None:
    rows = db.current_fares()

    if not rows:
        print("No fare records available.")
        return

    print(
        f"{'MODE':<18}"
        f"{'CLASS':<28}"
        f"{'FROM':<20}"
        f"{'TO':<20}"
        f"{'FARE':<10}"
        f"{'UNIT':<20}"
    )
    print("-" * 116)

    for row in rows:
        print(
            f"{row['mode']:<18}"
            f"{row['vehicle_class'][:27]:<28}"
            f"{row['origin'][:19]:<20}"
            f"{row['destination'][:19]:<20}"
            f"₹{row['fare']:<9}"
            f"{row['unit'][:19]:<20}"
        )


def print_changes(db: FareDatabase) -> None:
    rows = db.changes()

    if not rows:
        print("No source changes recorded.")
        return

    for row in rows:
        print(
            f"[{row['detected_at']}] "
            f"{row['source']} "
            f"{row['change_type']}"
        )

        if row["previous_hash"]:
            print(
                f"  previous: {row['previous_hash']}"
            )

        print(
            f"  current:  {row['current_hash']}"
        )


def export_json(db: FareDatabase, path: Path) -> None:
    rows = db.current_fares()

    payload = {
        "application": APP_NAME,
        "generated_at": utc_now(),
        "jurisdiction": "Jammu and Kashmir, India",
        "fares": [],
    }

    for row in rows:
        payload["fares"].append(
            {
                "source": row["source"],
                "authority": row["authority"],
                "mode": row["mode"],
                "vehicle_class": row["vehicle_class"],
                "origin": row["origin"],
                "destination": row["destination"],
                "fare": row["fare"],
                "currency": row["currency"],
                "unit": row["unit"],
                "observed_at": row["observed_at"],
                "source_url": row["source_url"],
                "source_hash": row["source_hash"],
                "status": row["status"],
            }
        )

    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def search_fares(
    db: FareDatabase,
    origin: str | None,
    destination: str | None,
    mode: str | None,
) -> None:
    clauses = ["status = 'ACTIVE'"]
    parameters: list[str] = []

    if origin:
        clauses.append("LOWER(origin) LIKE ?")
        parameters.append(f"%{origin.lower()}%")

    if destination:
        clauses.append("LOWER(destination) LIKE ?")
        parameters.append(f"%{destination.lower()}%")

    if mode:
        clauses.append("LOWER(mode) = ?")
        parameters.append(mode.lower())

    query = f"""
        SELECT *
        FROM fares
        WHERE {' AND '.join(clauses)}
        ORDER BY origin, destination, vehicle_class
    """

    rows = db.connection.execute(
        query,
        parameters,
    ).fetchall()

    if not rows:
        print("No matching recovery fare records.")
        return

    for row in rows:
        print(
            f"{row['origin']} -> {row['destination']} | "
            f"{row['vehicle_class']} | "
            f"₹{row['fare']} {row['unit']} | "
            f"{row['source']}"
        )


def monitor(
    db: FareDatabase,
    interval_seconds: int,
) -> None:
    while True:
        print(
            f"\n[{utc_now()}] "
            f"Checking official JKRTC fare sources."
        )

        result = fetch_and_store_jkrtc(db)

        print(
            f"Records: {result['records']} | "
            f"Changed sources: {result['changes']}"
        )

        for source in result["sources"]:
            print(
                f"  {source['source']}: "
                f"{source['status']}"
            )

        time.sleep(interval_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Track Jammu & Kashmir government/public "
            "transport fares."
        )
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    update = subparsers.add_parser(
        "update",
        help="Fetch current JKRTC source pages.",
    )
    update.set_defaults(command_handler="update")

    seed = subparsers.add_parser(
        "seed-government",
        help="Store current government fare references.",
    )
    seed.set_defaults(command_handler="seed-government")

    show = subparsers.add_parser(
        "show",
        help="Show stored fare records.",
    )
    show.set_defaults(command_handler="show")

    changes = subparsers.add_parser(
        "changes",
        help="Show source change events.",
    )
    changes.set_defaults(command_handler="changes")

    export = subparsers.add_parser(
        "export",
        help="Export current fare records as JSON.",
    )
    export.add_argument(
        "path",
        type=Path,
        help="Output JSON path.",
    )
    export.set_defaults(command_handler="export")

    search = subparsers.add_parser(
        "search",
        help="Search stored fares.",
    )
    search.add_argument("--from", dest="origin")
    search.add_argument("--to", dest="destination")
    search.add_argument("--mode")
    search.set_defaults(command_handler="search")

    monitor_parser = subparsers.add_parser(
        "monitor",
        help="Continuously monitor official JKRTC pages.",
    )
    monitor_parser.add_argument(
        "--interval",
        type=int,
        default=3600,
        help="Polling interval in seconds.",
    )
    monitor_parser.set_defaults(command_handler="monitor")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    db = FareDatabase(DB_PATH)

    try:
        if args.command_handler == "update":
            result = fetch_and_store_jkrtc(db)
            print(json.dumps(result, indent=2))

        elif args.command_handler == "seed-government":
            seed_government_rates(db)
            print(
                "Government fare references stored."
            )

        elif args.command_handler == "show":
            print_fares(db)

        elif args.command_handler == "changes":
            print_changes(db)

        elif args.command_handler == "export":
            export_json(db, args.path)
            print(
                f"Exported {args.path.resolve()}"
            )

        elif args.command_handler == "search":
            search_fares(
                db,
                args.origin,
                args.destination,
                args.mode,
            )

        elif args.command_handler == "monitor":
            monitor(db, args.interval)

        return 0

    except KeyboardInterrupt:
        print("\nMonitor stopped.")
        return 130

    except Exception as exc:
        print(
            f"Fatal recovery-tracker error: {exc}",
            file=sys.stderr,
        )
        return 1

    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())