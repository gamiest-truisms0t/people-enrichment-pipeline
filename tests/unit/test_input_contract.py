"""docs/input-contract.json is generated from the ingestion code and must not drift."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_committed_input_contract_is_current() -> None:
    module = runpy.run_path(str(ROOT / "scripts" / "input_contract.py"), run_name="contract")
    rendered = module["render_text"]()
    committed = (ROOT / "docs" / "input-contract.json").read_text(encoding="utf-8")
    assert committed == rendered, "run `make input-contract` and commit the result"
    contract = json.loads(rendered)
    assert contract["columns"]["required"] == ["first_name", "last_name"]
    assert "consent" in contract["columns"]["optional"]
    assert "opt_in" in contract["columns"]["header_aliases"]["consent"]
