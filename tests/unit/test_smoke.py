"""Smoke tests that keep CI meaningful before Phase 1 lands real logic."""

from enrich_pipeline import __version__
from enrich_pipeline.cli import build_parser


def test_version_is_set() -> None:
    assert __version__


def test_cli_parser_builds() -> None:
    parser = build_parser()
    assert parser.prog == "enrich"
