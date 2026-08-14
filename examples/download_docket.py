#!/usr/bin/env python3
"""Search CourtListener for dockets and download merged JSON bundles.

Given a docket number and a court, this script looks up the matching
docket(s) and, for each one, collects the associated docket entries,
parties, and attorneys. Everything is merged into a single JSON structure
and written to a file.

You can look up a single docket with ``--docket-number``/``--court``, or
process many at once by passing a CSV file with ``--csv``. The CSV must
have ``court`` and ``docket_number`` columns (order does not matter):

    court,docket_number
    cand,5:14-cv-01974
    scotus,22-100

Usage:
    export COURTLISTENER_API_TOKEN="your-token-here"

    # Single lookup
    python examples/download_docket.py \
        --docket-number "5:14-cv-01974" --court "cand" \
        --output docket.json

    # Bulk lookup from a CSV
    python examples/download_docket.py \
        --csv dockets.csv --output dockets.json

Requires an authenticated client; see the project README for how to obtain
an API token.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from typing import Any

from courtlistener import CourtListener
from courtlistener.exceptions import CourtListenerAPIError

REQUIRED_CSV_COLUMNS = ("court", "docket_number")


def collect_related(
    client: CourtListener, resource_name: str, docket_id: int
) -> list[dict[str, Any]]:
    """Return every record from ``resource_name`` tied to ``docket_id``.

    The client's ``list`` returns a ``ResourceIterator`` that transparently
    walks every page, so iterating it gathers the full result set.
    """
    resource = getattr(client, resource_name)
    return list(resource.list(docket=docket_id))


def build_docket_bundle(
    client: CourtListener, docket: dict[str, Any]
) -> dict[str, Any]:
    """Merge a docket with its entries, parties, and attorneys."""
    docket_id = docket["id"]
    return {
        **docket,
        "docket_entries": collect_related(
            client, "docket_entries", docket_id
        ),
        "parties": collect_related(client, "parties", docket_id),
        "attorneys": collect_related(client, "attorneys", docket_id),
    }


def search_dockets(
    client: CourtListener, docket_number: str, court: str
) -> list[dict[str, Any]]:
    """Find dockets matching a docket number within a court."""
    results = client.dockets.list(
        docket_number=docket_number, court=court
    )
    return list(results)


def bundles_for_query(
    client: CourtListener, docket_number: str, court: str
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

    bundles = [build_docket_bundle(client, docket) for docket in dockets]
    for bundle in bundles:
        print(
            f"  Docket {bundle['id']}: "
            f"{len(bundle['docket_entries'])} entries, "
            f"{len(bundle['parties'])} parties, "
            f"{len(bundle['attorneys'])} attorneys.",
            file=sys.stderr,
        )
    return bundles


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
            "one JSON file. Provide a single docket with --docket-number and "
            "--court, or many via --csv."
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
        default="dockets.json",
        help="Path to write the merged JSON file (default: dockets.json).",
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
        client = CourtListener(api_token=args.api_token)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    all_bundles: list[dict[str, Any]] = []
    try:
        with client:
            for i, query in enumerate(queries, start=1):
                print(
                    f"[{i}/{len(queries)}] Searching docket "
                    f"'{query['docket_number']}' in court "
                    f"'{query['court']}'...",
                    file=sys.stderr,
                )
                all_bundles.extend(
                    bundles_for_query(
                        client,
                        query["docket_number"],
                        query["court"],
                    )
                )
    except CourtListenerAPIError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 1

    if not all_bundles:
        print("No dockets found for any query.", file=sys.stderr)
        return 1

    # For a single-docket lookup that matched exactly one docket, emit the
    # bundle directly. Bulk runs (and multi-match single lookups) emit a
    # JSON array so every result is preserved.
    single_lookup = not args.csv
    output_data: Any = (
        all_bundles[0]
        if single_lookup and len(all_bundles) == 1
        else all_bundles
    )

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(output_data, handle, indent=args.indent, ensure_ascii=False)
        handle.write("\n")

    print(
        f"Wrote {len(all_bundles)} docket bundle(s) to {args.output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
