import json
from datetime import datetime
from unittest.mock import patch

import pytest

from job_hunter import (
    HHSource,
    add_vacancy,
    complete_followup,
    connect,
    due_reminders,
    list_vacancies,
    match_resume,
    set_status,
)


def test_status_history_and_search(tmp_path):
    db = connect(tmp_path / "jobs.db")
    vid = add_vacancy(db, "Data Engineer", "Acme", description="Required: Python, SQL")
    set_status(db, vid, "applied")
    assert list_vacancies(db, "acme")[0]["status"] == "applied"
    assert [r["new_status"] for r in db.execute("select new_status from status_history order by id")] == [
        "saved",
        "applied",
    ]
    set_status(db, vid, "applied")
    assert db.execute("select count(*) from status_history").fetchone()[0] == 2


def test_invalid_data_and_status(tmp_path):
    db = connect(tmp_path / "jobs.db")
    with pytest.raises(ValueError):
        add_vacancy(db, "", "Acme")
    with pytest.raises(ValueError):
        set_status(db, 404, "offer")


def test_status_transition_is_constrained_and_audited(tmp_path):
    db = connect(tmp_path / "jobs.db")
    vacancy = add_vacancy(db, "Engineer", "Acme")
    with pytest.raises(ValueError, match="Cannot move"):
        set_status(db, vacancy, "offer")
    assert db.execute("select count(*) from status_history").fetchone()[0] == 1


def test_followup_reminders_are_timezone_aware_daily_idempotent_and_completable(tmp_path):
    db = connect(tmp_path / "jobs.db")
    vacancy = add_vacancy(db, "Engineer", "Acme")
    app_id = db.execute("select id from applications where vacancy_id=?", (vacancy,)).fetchone()[0]
    task_id = db.execute(
        "insert into follow_up_tasks(application_id,due_at,note) values(?,?,?)",
        (app_id, "2026-03-29T01:30:00+01:00", "Review interview notes"),
    ).lastrowid
    now_at = datetime.fromisoformat("2026-03-29T03:15:00+02:00")

    assert due_reminders(db, now_at) [0]["id"] == task_id
    assert due_reminders(db, now_at, record=True)[0]["note"] == "Review interview notes"
    assert due_reminders(db, now_at, record=True) == []
    next_day = datetime.fromisoformat("2026-03-30T09:00:00+02:00")
    assert len(due_reminders(db, next_day, record=True)) == 1
    complete_followup(db, task_id)
    assert due_reminders(db, next_day, record=True) == []
    with pytest.raises(ValueError, match="Pending follow-up"):
        complete_followup(db, task_id)


def test_due_reminder_rejects_naive_clock(tmp_path):
    db = connect(tmp_path / "jobs.db")
    with pytest.raises(ValueError, match="timezone"):
        due_reminders(db, datetime(2026, 1, 1))


def test_match_is_evidence_based_and_bilingual():
    result = match_resume("Обязательно: Python и SQL. Желательно: Docker.", "Опыт: Python, SQL. Работал с PostgreSQL")
    assert "python" in result["matched"] and "sql" in result["matched"]
    assert "docker" in result["unmentioned"]
    assert result["score"] == 67
    assert "probability" in result["score_note"]


def test_empty_description_has_no_score():
    assert match_resume("", "Resume")["score"] is None


def test_hh_adapter_pagination_user_agent_timeout_and_cache():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            assert limit == 2_000_001
            return json.dumps(
                {
                    "items": [
                        {"name": "Инженер", "employer": {"name": "Company"}, "alternate_url": "https://hh.ru/vacancy/1"}
                    ]
                }
            ).encode()

    seen = []

    def fake(req, timeout):
        seen.append((req.full_url, req.get_header("User-agent"), timeout))
        return Response()

    source = HHSource(timeout=3)
    with patch("urllib.request.urlopen", fake):
        assert source.search("python", page=2)[0]["source"] == "hh.ru"
        source.search("python", page=2)
    assert (
        len(seen) == 1 and "page=2" in seen[0][0] and seen[0][1].startswith("PersonalAutomationLab") and seen[0][2] == 3
    )
