#!/usr/bin/env python3
"""Search CourtListener for dockets and download merged JSON bundles.

Given a docket number and a court, this script looks up the matching
docket(s) and, for each one, collects the associated docket entries,
parties, and attorneys. Everything is merged into a single JSON structure
and written to its own file named ``<court>_<docket_number>.json`` (colons
in the docket number are replaced with dashes), e.g.
``cand_5-14-cv-01974.json``.

To keep API usage low, attorneys are read from the nested data already
present in the ``/parties/`` response, so a docket costs three requests
(docket search + docket entries + parties) plus any pagination pages,
rather than four. Use ``--separate-attorneys`` to instead pull full
attorney contact records from the dedicated ``/attorneys/`` endpoint.

Bulk runs are throttled to stay under CourtListener's rate limits: by
default HTTP requests are spaced so any 60-second window holds fewer than
15 (every page fetch counts). Tune this with ``--requests-per-minute``, or
set it to 0 to disable throttling.

You can look up a single docket with ``--docket-number``/``--court``, or
process many at once by passing a CSV file with ``--csv``. The CSV must
have ``court`` and ``docket_number`` columns (order does not matter):

    court,docket_number
    cand,5:14-cv-01974
    scotus,22-100

Usage:
    export COURTLISTENER_API_TOKEN="your-token-here"

    # Single lookup, written to the current directory
    python examples/download_docket.py \
        --docket-number "5:14-cv-01974" --court "cand"

    # Bulk lookup from a CSV, all files written into ./out
    python examples/download_docket.py --csv dockets.csv --output ./out

Requires an authenticated client; see the project README for how to obtain
an API token.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from typing import Any, Callable

from courtlistener import CourtListener
from courtlistener.exceptions import CourtListenerAPIError

REQUIRED_CSV_COLUMNS = ("court", "docket_number")

# Stay comfortably under CourtListener's rate limits on bulk runs. This is a
# ceiling on HTTP requests per minute (every page fetch counts). Fixed-interval
# spacing lets the first request through immediately, so a rolling minute can
# hold one more request than the nominal rate; 13/min therefore keeps every
# 60-second window strictly under 15 requests.
DEFAULT_REQUESTS_PER_MINUTE = 13.0


class RateLimiter:
    """Space out calls so no more than ``max_per_minute`` happen per minute.

    Enforces a fixed minimum interval between successive ``wait()`` calls,
    which caps the sustained rate. A non-positive ``max_per_minute``
    disables throttling entirely.
    """

    def __init__(
        self,
        max_per_minute: float,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_interval = (
            60.0 / max_per_minute if max_per_minute > 0 else 0.0
        )
        self._sleep = sleep
        self._monotonic = monotonic
        self._last: float | None = None

    def wait(self) -> None:
        """Block until enough time has passed since the previous call."""
        if self.min_interval <= 0:
            return
        now = self._monotonic()
        if self._last is not None:
            elapsed = now - self._last
            if elapsed < self.min_interval:
                self._sleep(self.min_interval - elapsed)
        self._last = self._monotonic()


def install_rate_limit(client: CourtListener, limiter: RateLimiter) -> None:
    """Throttle every HTTP request the client makes through ``limiter``.

    Wrapping ``_request`` (rather than the per-docket helpers) ensures
    pagination pages are throttled too, since each page is its own request.
    """
    original = client._request

    def throttled(method: str, path: str, **kwargs: Any) -> Any:
        limiter.wait()
        return original(method, path, **kwargs)

    client._request = throttled  # type: ignore[method-assign]


def collect_related(
    client: CourtListener, resource_name: str, docket_id: int
) -> list[dict[str, Any]]:
    """Return every record from ``resource_name`` tied to ``docket_id``.

    The client's ``list`` returns a ``ResourceIterator`` that transparently
    walks every page, so iterating it gathers the full result set.
    """
    resource = getattr(client, resource_name)
    return list(resource.list(docket=docket_id))


def collect_parties(
    client: CourtListener, docket_id: int
) -> list[dict[str, Any]]:
    """Return the docket's parties with their attorneys nested.

    ``filter_nested_results`` scopes each party's embedded ``attorneys`` to
    this docket, so a single ``/parties/`` request carries the attorney data
    too and the separate ``/attorneys/`` call can be skipped.
    """
    return list(
        client.parties.list(docket=docket_id, filter_nested_results=True)
    )


def attorneys_from_parties(
    parties: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten and de-duplicate the attorneys embedded in ``parties``.

    Each party carries an ``attorneys`` list; the same attorney can appear
    under several parties, so entries are de-duplicated by attorney id
    (falling back to ``attorney_id``). Entries without an id are kept as-is.
    """
    seen: dict[Any, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for party in parties:
        for attorney in party.get("attorneys") or []:
            key = attorney.get("id", attorney.get("attorney_id"))
            if key is None:
                ordered.append(attorney)
            elif key not in seen:
                seen[key] = attorney
                ordered.append(attorney)
    return ordered


def build_docket_bundle(
    client: CourtListener,
    docket: dict[str, Any],
    separate_attorneys: bool = False,
) -> dict[str, Any]:
    """Merge a docket with its entries, parties, and attorneys.

    By default attorneys are taken from the nested data in the parties
    response, saving a request. Pass ``separate_attorneys=True`` to fetch
    full attorney records (name, contact, phone, email) from the dedicated
    ``/attorneys/`` endpoint instead, at the cost of one more request.
    """
    docket_id = docket["id"]
    parties = collect_parties(client, docket_id)
    if separate_attorneys:
        attorneys = collect_related(client, "attorneys", docket_id)
    else:
        attorneys = attorneys_from_parties(parties)
    return {
        **docket,
        "docket_entries": collect_related(
            client, "docket_entries", docket_id
        ),
        "parties": parties,
        "attorneys": attorneys,
    }


def search_dockets(
    client: CourtListener, docket_number: str, court: str
) -> list[dict[str, Any]]:
    """Find dockets matching a docket number within a court."""
    results = client.dockets.list(
        docket_number=docket_number, court=court
    )
    return list(results)


def docket_filename(court: str, docket_number: str) -> str:
    """Build a filesystem-safe ``<court>_<docket_number>.json`` name.

    Colons in the docket number become dashes (e.g. ``5:14-cv-01974`` ->
    ``5-14-cv-01974``); any path separators are also neutralized so the
    name can never escape the output directory.
    """
    safe = docket_number.replace(":", "-")
    for sep in (os.sep, os.altsep, "/"):
        if sep:
            safe = safe.replace(sep, "-")
    court_safe = court.replace(os.sep, "-")
    if os.altsep:
        court_safe = court_safe.replace(os.altsep, "-")
    return f"{court_safe}_{safe}.json"


def bundles_for_query(
    client: CourtListener,
    docket_number: str,
    court: str,
    separate_attorneys: bool = False,
) -> list[dict[str, Any]]:
    """Search for a docket number/court and build bundles for each match."""
    dockets = search_dockets(client, docket_number, court)
    if not dockets:
        print(
            f"  No dockets found for docket number '{docket_number}' "
            f"in court '{court}'.",
            file=sys.stderr,
        )
        return []

    bundles = [
        build_docket_bundle(client, docket, separate_attorneys)
        for docket in dockets
    ]
    for bundle in bundles:
        print(
            f"  Docket {bundle['id']}: "
            f"{len(bundle['docket_entries'])} entries, "
            f"{len(bundle['parties'])} parties, "
            f"{len(bundle['attorneys'])} attorneys.",
            file=sys.stderr,
        )
    return bundles


def write_bundle(
    bundle: dict[str, Any],
    output_dir: str,
    filename: str,
    indent: int,
) -> str:
    """Write a single docket bundle to ``output_dir/filename``."""
    path = os.path.join(output_dir, filename)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(bundle, handle, indent=indent, ensure_ascii=False)
        handle.write("\n")
    return path


def read_csv_queries(path: str) -> list[dict[str, str]]:
    """Read (court, docket_number) pairs from a CSV file.

    Returns a list of ``{"court": ..., "docket_number": ...}`` dicts. Rows
    missing either value are skipped with a warning.
    """
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV file '{path}' is empty.")
        fieldnames = {name.strip() for name in reader.fieldnames}
        missing = [c for c in REQUIRED_CSV_COLUMNS if c not in fieldnames]
        if missing:
            raise ValueError(
                f"CSV file '{path}' is missing required column(s): "
                f"{', '.join(missing)}. Found: {', '.join(reader.fieldnames)}."
            )

        queries: list[dict[str, str]] = []
        for line_no, row in enumerate(reader, start=2):
            court = (row.get("court") or "").strip()
            docket_number = (row.get("docket_number") or "").strip()
            if not court or not docket_number:
                print(
                    f"Skipping CSV row {line_no}: both 'court' and "
                    "'docket_number' are required.",
                    file=sys.stderr,
                )
                continue
            queries.append(
                {"court": court, "docket_number": docket_number}
            )
    return queries


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Search CourtListener by docket number and court, then download "
            "the docket, docket entries, parties, and attorneys merged into "
            "one JSON file per docket. Provide a single docket with "
            "--docket-number and --court, or many via --csv. Each docket is "
            "saved as <court>_<docket_number>.json (colons become dashes)."
        )
    )
    parser.add_argument(
        "--docket-number",
        help="The docket number to search for, e.g. '5:14-cv-01974'.",
    )
    parser.add_argument(
        "--court",
        help="The court ID to scope the search to, e.g. 'cand' or 'scotus'.",
    )
    parser.add_argument(
        "--csv",
        help=(
            "Path to a CSV file with 'court' and 'docket_number' columns for "
            "bulk lookups. Mutually exclusive with --docket-number/--court."
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        default=".",
        help=(
            "Directory to write the per-docket JSON files into "
            "(default: current directory). Created if it does not exist."
        ),
    )
    parser.add_argument(
        "--api-token",
        default=None,
        help=(
            "CourtListener API token. Falls back to the "
            "COURTLISTENER_API_TOKEN environment variable when omitted."
        ),
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="Indentation for the output JSON (default: 2).",
    )
    parser.add_argument(
        "--requests-per-minute",
        type=float,
        default=DEFAULT_REQUESTS_PER_MINUTE,
        help=(
            "Cap on HTTP requests per minute, counting every page fetch "
            f"(default: {DEFAULT_REQUESTS_PER_MINUTE:g}, keeping any 60s "
            "window under 15). Set to 0 to disable throttling."
        ),
    )
    parser.add_argument(
        "--separate-attorneys",
        action="store_true",
        help=(
            "Fetch full attorney records from the /attorneys/ endpoint "
            "instead of deriving them from the nested parties data. Adds one "
            "request (plus pagination) per docket but includes attorney "
            "contact details."
        ),
    )
    args = parser.parse_args()

    # Validate the input mode: exactly one of CSV or a docket-number/court
    # pair must be supplied.
    if args.csv:
        if args.docket_number or args.court:
            parser.error(
                "--csv cannot be combined with --docket-number/--court."
            )
    else:
        if not (args.docket_number and args.court):
            parser.error(
                "Provide both --docket-number and --court, or use --csv."
            )

    try:
        queries = (
            read_csv_queries(args.csv)
            if args.csv
            else [
                {
                    "court": args.court,
                    "docket_number": args.docket_number,
                }
            ]
        )
    except (OSError, ValueError) as exc:
        print(f"Error reading CSV: {exc}", file=sys.stderr)
        return 1

    if not queries:
        print("No valid queries to process.", file=sys.stderr)
        return 1

    try:
        os.makedirs(args.output, exist_ok=True)
    except OSError as exc:
        print(f"Error creating output directory: {exc}", file=sys.stderr)
        return 1

    try:
        client = CourtListener(api_token=args.api_token)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.requests_per_minute > 0:
        install_rate_limit(
            client, RateLimiter(args.requests_per_minute)
        )
        print(
            f"Throttling to {args.requests_per_minute:g} requests/minute.",
            file=sys.stderr,
        )

    written = 0
    try:
        with client:
            for i, query in enumerate(queries, start=1):
                court = query["court"]
                docket_number = query["docket_number"]
                print(
                    f"[{i}/{len(queries)}] Searching docket "
                    f"'{docket_number}' in court '{court}'...",
                    file=sys.stderr,
                )
                bundles = bundles_for_query(
                    client,
                    docket_number,
                    court,
                    separate_attorneys=args.separate_attorneys,
                )
                base = docket_filename(court, docket_number)
                for bundle in bundles:
                    # A single query can match more than one docket; keep
                    # each file distinct by suffixing the docket id.
                    filename = (
                        base
                        if len(bundles) == 1
                        else f"{base[:-len('.json')]}_{bundle['id']}.json"
                    )
                    path = write_bundle(
                        bundle, args.output, filename, args.indent
                    )
                    written += 1
                    print(f"  Wrote {path}", file=sys.stderr)
    except CourtListenerAPIError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 1

    if not written:
        print("No dockets found for any query.", file=sys.stderr)
        return 1

    print(
        f"Wrote {written} docket file(s) to {args.output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
