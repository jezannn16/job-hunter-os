import os

import pytest

from job_hunter import HHSource


@pytest.mark.live
@pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 to use the network")
def test_public_hh_search_returns_vacancies():
    results = HHSource(timeout=12).search("python", page=0)

    assert results
    assert all(row["title"] and row["url"].startswith("https://") for row in results)
