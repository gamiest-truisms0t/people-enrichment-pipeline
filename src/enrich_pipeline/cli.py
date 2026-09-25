"""Local command-line entry point.

Phase 1 turns this into the local runner:
    enrich run --input data/sample/names.csv --provider mock --out ./out
"""

from __future__ import annotations

import argparse
import sys

from enrich_pipeline import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="enrich", description="People-enrichment pipeline")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
