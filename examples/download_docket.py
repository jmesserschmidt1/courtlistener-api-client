#!/usr/bin/env python3
"""Search CourtListener for a docket and download a merged JSON bundle.

Given a docket number and a court, this script looks up the matching
docket(s) and, for each one, collects the associated docket entries,
parties, and attorneys. Everything is merged into a single JSON structure
and written to a file.

Usage:
    export COURTLISTENER_API_TOKEN="your-token-here"
    python examples/download_docket.py \
        --docket-number "5:14-cv-01974" \
        --court "cand" \
        --output docket.json

Requires an authenticated client; see the project README for how to obtain
an API token.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from courtlistener import CourtListener
from courtlistener.exceptions import CourtListenerAPIError


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Search CourtListener by docket number and court, then download "
            "the docket, docket entries, parties, and attorneys merged into "
            "one JSON file."
        )
    )
    parser.add_argument(
        "--docket-number",
        required=True,
        help="The docket number to search for, e.g. '5:14-cv-01974'.",
    )
    parser.add_argument(
        "--court",
        required=True,
        help="The court ID to scope the search to, e.g. 'cand' or 'scotus'.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="docket.json",
        help="Path to write the merged JSON file (default: docket.json).",
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

    try:
        client = CourtListener(api_token=args.api_token)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        with client:
            dockets = search_dockets(
                client, args.docket_number, args.court
            )
            if not dockets:
                print(
                    "No dockets found for docket number "
                    f"'{args.docket_number}' in court '{args.court}'.",
                    file=sys.stderr,
                )
                return 1

            print(
                f"Found {len(dockets)} docket(s). Collecting related "
                "records...",
                file=sys.stderr,
            )
            bundles = [
                build_docket_bundle(client, docket) for docket in dockets
            ]
    except CourtListenerAPIError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 1

    # A single docket match is the common case; unwrap it so the output is
    # the docket bundle directly rather than a one-element list.
    output_data: Any = bundles[0] if len(bundles) == 1 else bundles

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(output_data, handle, indent=args.indent, ensure_ascii=False)
        handle.write("\n")

    for bundle in bundles:
        print(
            f"Docket {bundle['id']}: "
            f"{len(bundle['docket_entries'])} entries, "
            f"{len(bundle['parties'])} parties, "
            f"{len(bundle['attorneys'])} attorneys.",
            file=sys.stderr,
        )
    print(f"Wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
