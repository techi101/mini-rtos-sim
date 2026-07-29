"""
tests/test_task.py — pytest tests for the Task class

Verifies state transitions, timing tracking, and computed properties.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from task import Task, TaskState


@pytest.fixture
def simple_task():
    return Task("Alpha", priority=2, burst=5, arrival_tick=0)


class TestTaskInitialState:
    def test_initial_state_is_ready(self, simple_task):
        assert simple_task.state == TaskState.READY

    def test_remaining_burst_equals_burst(self, simple_task):
        assert simple_task.remaining_burst == 5

    def test_start_tick_is_none(self, simple_task):
        assert simple_task.start_tick is None

    def test_finish_tick_is_none(self, simple_task):
        assert simple_task.finish_tick is None

    def test_is_ready_true(self, simple_task):
        assert simple_task.is_ready is True

    def test_is_running_false(self, simple_task):
        assert simple_task.is_running is False


class TestTaskStateTransitions:
    def test_mark_running_sets_state(self, simple_task):
        simple_task.mark_running(current_tick=3)
        assert simple_task.state == TaskState.RUNNING
        assert simple_task.start_tick == 3

    def test_mark_running_does_not_reset_start_tick(self, simple_task):
        simple_task.mark_running(current_tick=3)
        simple_task.mark_ready()
        simple_task.mark_running(current_tick=7)
        assert simple_task.start_tick == 3   # first start_tick preserved

    def test_mark_blocked_sets_state(self, simple_task):
        simple_task.mark_blocked("uart_lock")
        assert simple_task.state == TaskState.BLOCKED
        assert simple_task.blocked_on_mutex == "uart_lock"

    def test_mark_unblocked_clears_mutex(self, simple_task):
        simple_task.mark_blocked("uart_lock")
        simple_task.mark_unblocked()
        assert simple_task.state == TaskState.READY
        assert simple_task.blocked_on_mutex is None

    def test_mark_done_sets_finish_tick(self, simple_task):
        simple_task.mark_running(0)
        simple_task.mark_done(5)
        assert simple_task.state == TaskState.DONE
        assert simple_task.finish_tick == 5

    def test_turnaround_time(self, simple_task):
        simple_task.mark_running(0)
        simple_task.mark_done(7)
        # arrival=0, finish=7 → turnaround = 7
        assert simple_task.turnaround_time == 7

    def test_turnaround_with_late_arrival(self):
        t = Task("Beta", priority=1, burst=4, arrival_tick=3)
        t.mark_running(3)
        t.mark_done(10)
        # turnaround = finish(10) - arrival(3) = 7
        assert t.turnaround_time == 7

    def test_turnaround_none_when_not_done(self, simple_task):
        assert simple_task.turnaround_time is None
