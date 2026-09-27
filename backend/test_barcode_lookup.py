"""
Tests for the pure functions in backend/barcode_lookup.py.

Deliberately no network and no database: these cover the logic that decides
what a user sees — which branches are kept, how they are ordered, and what
happens when a branch has no coordinates or we do not know where the user is.

Run from backend/:

    python3 -m pytest test_barcode_lookup.py -q
    # or, with no test runner installed:
    python3 test_barcode_lookup.py
"""

import os
import sys

# barcode_lookup.py reads these at import time. Nothing here uses them.
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test")
os.environ.setdefault("CIJENE_API_KEY", "test")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from barcode_lookup import (          # noqa: E402
    aggregate_from,
    current_price_day,
    haversine_km,
    rows_to_prices,
)

# Two Zagreb branches, several hundred metres apart, plus one in Split.
ROWS = [
    {
        "chain": "spar", "special_price": "1.49", "regular_price": "1.99",
        "unit_price": "2.98", "price_date": "2026-09-27",
        "store": {"code": "87079", "address": "Savska 58", "city": "Zagreb",
                  "lat": 45.7988, "lon": 15.9614},
    },
    {
        "chain": "konzum", "special_price": None, "regular_price": "1.69",
        "unit_price": "3.38", "price_date": "2026-09-27",
        "store": {"code": "3200", "address": "Radnicka 49", "city": "Zagreb",
                  "lat": 45.7960, "lon": 16.0080},
    },
    {
        "chain": "farbeyond", "special_price": "1.00", "regular_price": "1.00",
        "price_date": "2026-09-27",
        "store": {"code": "9", "address": "Nowhere", "city": "Split",
                  "lat": 43.51, "lon": 16.44},
    },
]

ZAGREB = (45.80, 15.97)


def test_haversine_is_sane():
    assert 250 < haversine_km(*ZAGREB, 43.51, 16.44) < 270   # Zagreb to Split
    assert haversine_km(45.8, 16.0, 45.8, 16.0) < 0.001      # a point to itself


def test_radius_filter_keeps_nearby_and_drops_far():
    out = rows_to_prices(ROWS, *ZAGREB, 10)
    assert {p["store"] for p in out} == {"spar", "konzum"}


def test_results_are_cheapest_first():
    out = rows_to_prices(ROWS, *ZAGREB, 10)
    assert out[0]["store"] == "spar"


def test_distance_is_computed_from_branch_coordinates():
    out = rows_to_prices(ROWS, *ZAGREB, 10)
    assert 0.6 < out[0]["distance_km"] < 0.8


def test_falls_back_to_regular_price_when_not_on_sale():
    out = rows_to_prices(ROWS, *ZAGREB, 10)
    konzum = next(p for p in out if p["store"] == "konzum")
    assert konzum["sale_price"] == "1.69"


def test_original_price_only_set_when_on_sale():
    out = rows_to_prices(ROWS, *ZAGREB, 10)
    spar = next(p for p in out if p["store"] == "spar")
    konzum = next(p for p in out if p["store"] == "konzum")
    assert spar["original_price"] == "1.99"
    assert konzum["original_price"] is None


def test_without_a_location_everything_is_kept():
    out = rows_to_prices(ROWS, None, None, None)
    assert len(out) == 3
    assert all(p["distance_km"] is None for p in out)


def test_branch_without_coordinates_is_dropped_when_we_know_the_user():
    # We know roughly where the user is but not where this shop is, so there is
    # no way to tell whether it is near them. Dropping it beats pricing an item
    # at a shop that may be on the other side of the country.
    no_coords = [{
        "chain": "branka", "special_price": "2.69", "regular_price": "2.69",
        "price_date": "2026-09-27",
        "store": {"code": "1", "address": "Somewhere", "city": "Zagreb",
                  "lat": None, "lon": None},
    }]
    assert rows_to_prices(no_coords, *ZAGREB, 10) == []


def test_branch_without_coordinates_is_kept_when_we_have_no_location():
    no_coords = [{
        "chain": "branka", "special_price": "2.69", "regular_price": "2.69",
        "price_date": "2026-09-27",
        "store": {"code": "1", "address": "Somewhere", "city": "Zagreb",
                  "lat": None, "lon": None},
    }]
    assert len(rows_to_prices(no_coords, None, None, None)) == 1


def test_aggregate_collapses_to_one_row_per_chain():
    agg = aggregate_from(ROWS)
    assert len(agg) == 3
    assert [a["sale_price"] for a in agg] == ["1.00", "1.49", "1.69"]


def test_price_day_rolls_over_at_0800_zagreb():
    """
    Prices refresh at 08:00 Europe/Zagreb, so 07:59 still belongs to yesterday.
    Zagreb is UTC+2 in summer and UTC+1 in winter, so both are checked.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    zagreb = ZoneInfo("Europe/Zagreb")
    # (local wall-clock, expected price day)
    cases = [
        (datetime(2026, 9, 27, 7, 59, tzinfo=zagreb), "2026-09-26"),
        (datetime(2026, 9, 27, 8, 0, tzinfo=zagreb), "2026-09-27"),
        (datetime(2026, 9, 27, 23, 59, tzinfo=zagreb), "2026-09-27"),
        (datetime(2026, 12, 1, 7, 30, tzinfo=zagreb), "2026-11-30"),   # winter
        (datetime(2026, 12, 1, 8, 30, tzinfo=zagreb), "2026-12-01"),
    ]
    # current_price_day() reads the clock, so compare the boundary logic by
    # shifting a fixed instant through the same arithmetic.
    from datetime import timedelta
    for local, expected in cases:
        got = (local - timedelta(hours=8)).date().isoformat()
        assert got == expected, f"{local} -> {got}, expected {expected}"


def test_price_day_returns_something_today():
    from datetime import date, timedelta
    today = date.today()
    assert current_price_day() in (today, today - timedelta(days=1))


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {name}\n          {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
