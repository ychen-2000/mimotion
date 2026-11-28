#!/usr/bin/env python3
"""Local self-tests for backfill/date/step changes. Safe: no network, no GitHub writes."""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
import types
from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _timezone
from pathlib import Path


def _install_test_stubs() -> None:
    """Provide lightweight stand-ins for runtime-only deps so logic can be tested offline."""
    if "pytz" not in sys.modules:
        pytz = types.ModuleType("pytz")
        pytz.timezone = lambda _name: _timezone(_timedelta(hours=8))
        sys.modules["pytz"] = pytz
    if "requests" not in sys.modules:
        requests = types.ModuleType("requests")
        requests.exceptions = types.SimpleNamespace(RequestException=Exception)
        sys.modules["requests"] = requests
    if "Crypto" not in sys.modules:
        crypto = types.ModuleType("Crypto")
        cipher = types.ModuleType("Crypto.Cipher")
        random_mod = types.ModuleType("Crypto.Random")
        cipher.AES = types.SimpleNamespace(block_size=16, new=lambda *a, **k: None)
        random_mod.get_random_bytes = lambda n: b"\x00" * n
        sys.modules["Crypto"] = crypto
        sys.modules["Crypto.Cipher"] = cipher
        sys.modules["Crypto.Random"] = random_mod


_install_test_stubs()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def main() -> int:
    backfill = importlib.import_module("backfill")

    dates = backfill.expand_dates(["2026-11-23..2026-11-27"])
    check(
        dates == ["2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27"],
        "date range expands inclusively to 23-27",
    )
    check(backfill.expand_dates(["2026-11-20", "2026-11-20"]) == ["2026-11-20"], "duplicate dates deduped")

    for bad in ["2026-02-30", "2026-11-31", "2027-01-01", "2026-11-27..2026-11-23"]:
        try:
            backfill.expand_dates([bad])
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid date/range accepted: {bad}")
    print("PASS invalid/future/backward ranges rejected")

    # Step strategy: scheduled runs use time ratio; manual dispatch uses full range with 9000 floor.
    import main as app

    app.config = {"MIN_STEP": "18000", "MAX_STEP": "25000"}
    app.time_bj = app.get_beijing_time().replace(hour=9, minute=0, second=0, microsecond=0)

    os.environ.pop("GITHUB_EVENT_NAME", None)
    scheduled_min, scheduled_max = app.get_min_max_for_run()
    check((scheduled_min, scheduled_max) == (7363, 10227), "scheduled 09:00 uses time ratio")

    os.environ["GITHUB_EVENT_NAME"] = "workflow_dispatch"
    manual_min, manual_max = app.get_min_max_for_run()
    check((manual_min, manual_max) == (18000, 25000), "manual dispatch uses full configured range")

    app.config = {"MIN_STEP": "5000", "MAX_STEP": "6000"}
    manual_min, manual_max = app.get_min_max_for_run()
    check((manual_min, manual_max) == (9000, 9000), "manual dispatch floors at 9000 steps")
    os.environ.pop("GITHUB_EVENT_NAME", None)

    # Date resolver: explicit date wins; default falls back to Beijing today.
    import util.zepp_helper as zepp

    os.environ["MIMOTION_DATE"] = "2026-11-23"
    check(zepp.resolve_target_date() == "2026-11-23", "MIMOTION_DATE override resolved")
    os.environ.pop("MIMOTION_DATE", None)
    check(
        re.fullmatch(r"\d{4}-\d{2}-\d{2}", zepp.resolve_target_date()) is not None,
        "default target date is YYYY-MM-DD (Beijing today)",
    )
    os.environ["MIMOTION_DATE"] = "2026-13-01"
    try:
        zepp.resolve_target_date()
    except SystemExit:
        print("PASS invalid MIMOTION_DATE rejected in zepp_helper")
    else:
        raise AssertionError("invalid MIMOTION_DATE accepted in zepp_helper")
    os.environ.pop("MIMOTION_DATE", None)

    # Workflow wiring exists.
    workflow = (ROOT / ".github/workflows/run.yml").read_text(encoding="utf-8")
    for needle in [
        "target_date:",
        "MIMOTION_DATE: ${{ inputs.target_date }}",
        "GITHUB_EVENT_NAME: ${{ github.event_name }}",
        "if: failure()",
        "账号刷步失败时推送 PushPlus",
        "git diff --cached --quiet",
    ]:
        check(needle in workflow, f"workflow contains {needle!r}")

    print("ALL SELF-TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
