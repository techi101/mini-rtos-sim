"""
tests/test_task.py — pytest tests for the Task class

Verifies state transitions, timing tracking, and computed properties.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from task import Task, TaskState, LockOp


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


# ── Priority inheritance support ─────────────────────────────────────────────

class TestEffectivePriority:
    def test_effective_equals_base_by_default(self, simple_task):
        assert simple_task.effective_priority == simple_task.priority
        assert simple_task.is_priority_boosted is False

    def test_inherited_priority_raises_effective(self, simple_task):
        simple_task.inherited_priority = 7
        assert simple_task.effective_priority == 7
        assert simple_task.is_priority_boosted is True

    def test_inheritance_never_lowers_priority(self, simple_task):
        """A low-priority waiter must not drag the owner below its base."""
        simple_task.inherited_priority = 1     # base is 2
        assert simple_task.effective_priority == 2
        assert simple_task.is_priority_boosted is False

    def test_clearing_inheritance_restores_base(self, simple_task):
        simple_task.inherited_priority = 9
        simple_task.inherited_priority = None
        assert simple_task.effective_priority == 2


# ── Lock programme ───────────────────────────────────────────────────────────

class TestLockOps:
    def test_op_is_due_at_matching_cpu_tick(self):
        t = Task("A", priority=1, burst=5,
                 lock_ops=[LockOp(2, "acquire", "bus")])
        assert t.due_lock_ops() == []          # cpu_ticks_used == 0
        t.cpu_ticks_used = 2
        assert len(t.due_lock_ops()) == 1

    def test_op_does_not_fire_twice(self):
        op = LockOp(0, "acquire", "bus")
        t = Task("A", priority=1, burst=5, lock_ops=[op])
        assert t.due_lock_ops() == [op]
        t.mark_op_fired(op)
        assert t.due_lock_ops() == []

    def test_ops_at_same_point_return_in_declaration_order(self):
        release = LockOp(2, "release", "a")
        acquire = LockOp(2, "acquire", "b")
        t = Task("A", priority=1, burst=5, lock_ops=[release, acquire])
        t.cpu_ticks_used = 2
        assert t.due_lock_ops() == [release, acquire]

    def test_invalid_action_rejected(self):
        with pytest.raises(ValueError):
            LockOp(0, "lock", "bus")

    def test_negative_trigger_rejected(self):
        with pytest.raises(ValueError):
            LockOp(-1, "acquire", "bus")

    def test_op_scheduled_past_burst_rejected(self):
        with pytest.raises(ValueError):
            Task("A", priority=1, burst=3,
                 lock_ops=[LockOp(4, "release", "bus")])


class TestResponseTime:
    def test_response_time_is_none_before_first_run(self, simple_task):
        assert simple_task.response_time is None

    def test_response_time_measures_arrival_to_first_cpu(self):
        t = Task("B", priority=1, burst=4, arrival_tick=3)
        t.mark_running(5)
        assert t.response_time == 2
