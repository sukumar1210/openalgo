"""
What the Python Strategies page says a strategy will do must match what the
scheduler will do.

Startup and the scheduler arm only strategies whose ``is_scheduled`` is set.
The status shown on the page ignored it, so a strategy whose schedule had been
switched off still read "Scheduled - Starts today at 09:00 IST" and then never
started. The trader had no way to tell.
"""

from blueprints import python_strategy as ps

SCHEDULE = {
    "schedule_start": "09:00",
    "schedule_stop": "15:30",
    "schedule_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
}


def test_a_strategy_with_no_schedule_armed_is_shown_as_stopped():
    status, message = ps.get_schedule_status({**SCHEDULE, "is_scheduled": False})

    assert status == "stopped"
    assert "will not start on its own" in message


def test_a_strategy_with_its_schedule_armed_is_still_shown_as_scheduled():
    status, _message = ps.get_schedule_status({**SCHEDULE, "is_scheduled": True})

    assert status == "scheduled"


def test_a_manual_stop_still_reads_as_a_manual_stop():
    status, _message = ps.get_schedule_status(
        {**SCHEDULE, "is_scheduled": True, "manually_stopped": True}
    )

    assert status == "manually_stopped"
