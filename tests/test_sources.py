from datetime import date

import pytest

from taxi.sources import MonthFile, expand_months, plan


def test_expand_months_range_and_list():
    assert expand_months("2024-11:2025-02") == [
        date(2024, 11, 1), date(2024, 12, 1), date(2025, 1, 1), date(2025, 2, 1)]
    assert expand_months("2025-03,2025-01,2025-03") == [date(2025, 1, 1), date(2025, 3, 1)]


@pytest.mark.parametrize("bad", ["2025-13", "25-01", "2025-03:2025-01", "abc"])
def test_expand_months_rejects(bad):
    with pytest.raises(ValueError):
        expand_months(bad)


def test_plan_and_urls():
    files = plan(["yellow", "fhvhv"], [date(2025, 1, 1)])
    assert [f.file_name for f in files] == [
        "yellow_tripdata_2025-01.parquet", "fhvhv_tripdata_2025-01.parquet"]
    assert MonthFile("green", date(2024, 6, 1)).url("https://x/") == \
        "https://x/trip-data/green_tripdata_2024-06.parquet"
    with pytest.raises(ValueError):
        plan(["bus"], [date(2025, 1, 1)])
