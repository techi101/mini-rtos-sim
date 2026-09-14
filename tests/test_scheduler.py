"""
tests/test_scheduler.py — pytest tests for the Scheduler

The scheduler is the part of this project that makes claims worth checking:
priority preemption, lock operations tied to execution, priority inheritance,
and deadlock detection. Previously none of it was covered by tests, so the
classes below are organised around those four claims, plus regressions for
the specific defects that were fixed.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from task import Task, TaskState, LockOp
from mutex import Mutex
from scheduler import Scheduler


# ── Helpers ──────────────────────────────────────────────────────────────────

def run(tasks, mutexes=None, max_ticks=40, inheritance=True):
    sched = Scheduler(tasks, mutexes or {}, enable_priority_inheritance=inheritance)
    timeline = sched.run(max_ticks=max_ticks)
    return sched, timeline


def ticks_where(timeline, substring):
    """Ticks at which an event containing `substring` was logged."""
    return [e["tick"] for e in timeline
            for ev in e["events"] if substring in ev]


def run_order(timeline):
    """Names of the task running at each tick (None for idle/deadlock)."""
    return [e["running"] for e in timeline]


def by_name(sched, name):
    return next(t for t in sched.tasks if t.name == name)


# ── Priority preemptive scheduling ───────────────────────────────────────────

class TestPriorityScheduling:
    def test_highest_priority_runs_first(self):
        tasks = [
            Task("Low",  priority=1, burst=2),
            Task("High", priority=3, burst=2),
            Task("Mid",  priority=2, burst=2),
        ]
        sched, timeline = run(tasks)
        assert run_order(timeline) == ["High", "High", "Mid", "Mid", "Low", "Low"]

    def test_arrival_preempts_lower_priority(self):
        tasks = [
            Task("Low",  priority=1, burst=5, arrival_tick=0),
            Task("High", priority=3, burst=2, arrival_tick=2),
        ]
        sched, timeline = run(tasks)
        assert run_order(timeline) == ["Low", "Low", "High", "High",
                                       "Low", "Low", "Low"]
        assert sched.preemption_count == 1

    def test_task_does_not_run_before_it_arrives(self):
        tasks = [Task("Late", priority=9, burst=2, arrival_tick=3)]
        sched, timeline = run(tasks, max_ticks=8)
        assert run_order(timeline)[:3] == [None, None, None]
        assert sched.idle_ticks == 3

    def test_equal_priority_does_not_cause_context_switch(self):
        """Ties keep the incumbent; round-robin would need an explicit quantum."""
        tasks = [
            Task("A", priority=2, burst=3),
            Task("B", priority=2, burst=3),
        ]
        sched, timeline = run(tasks)
        assert run_order(timeline) == ["A", "A", "A", "B", "B", "B"]
        assert sched.preemption_count == 0

    def test_all_bursts_are_fully_executed(self):
        tasks = [
            Task("A", priority=1, burst=4),
            Task("B", priority=3, burst=3, arrival_tick=1),
            Task("C", priority=2, burst=2, arrival_tick=2),
        ]
        sched, _ = run(tasks)
        assert all(t.is_done for t in tasks)
        assert [t.cpu_ticks_used for t in tasks] == [4, 3, 2]
        assert sched.executing_ticks == 9

    def test_idle_when_nothing_has_arrived(self):
        tasks = [Task("A", priority=1, burst=1, arrival_tick=2)]
        sched, timeline = run(tasks, max_ticks=5)
        assert timeline[0]["running"] is None
        assert timeline[0]["state"] == "IDLE"


# ── Lock operations follow execution, not the wall clock ─────────────────────

class TestLockOpsFollowExecution:
    def test_acquire_fires_on_own_cpu_ticks_not_absolute_tick(self):
        """
        Regression: lock operations used to be keyed to absolute simulation
        ticks, so a task could acquire a mutex while another task held the
        CPU. Worker's acquire is one tick into *its own* execution, and
        Worker does not get the CPU until Blocker finishes at t=3 — so the
        acquire must land at t=4, not t=1.
        """
        tasks = [
            Task("Blocker", priority=3, burst=3),
            Task("Worker",  priority=1, burst=4,
                 lock_ops=[LockOp(1, "acquire", "bus"),
                           LockOp(3, "release", "bus")]),
        ]
        sched, timeline = run(tasks, {"bus": Mutex("bus")})
        assert ticks_where(timeline, "ACQUIRED") == [4]
        assert ticks_where(timeline, "RELEASED") == [6]

    def test_preempted_task_cannot_take_a_lock(self):
        """
        Worker is preempted exactly at the point it would have acquired, so
        the lock must still be free for the higher-priority task.
        """
        tasks = [
            Task("Worker", priority=1, burst=6, arrival_tick=0,
                 lock_ops=[LockOp(1, "acquire", "bus"),
                           LockOp(4, "release", "bus")]),
            Task("Urgent", priority=3, burst=2, arrival_tick=1,
                 lock_ops=[LockOp(0, "acquire", "bus"),
                           LockOp(2, "release", "bus")]),
        ]
        sched, timeline = run(tasks, {"bus": Mutex("bus")})
        # Urgent gets the lock uncontended; Worker never entered the section.
        assert sched.mutexes["bus"].contention_count == 0
        assert by_name(sched, "Urgent").blocked_time == 0

    def test_release_at_end_of_burst_still_fires(self):
        """A task must not carry its mutex into the grave."""
        tasks = [
            Task("Holder", priority=2, burst=3,
                 lock_ops=[LockOp(0, "acquire", "bus"),
                           LockOp(3, "release", "bus")]),
        ]
        sched, timeline = run(tasks, {"bus": Mutex("bus")})
        assert sched.mutexes["bus"].is_free
        assert ticks_where(timeline, "RELEASED") == [2]

    def test_unknown_mutex_is_rejected_loudly(self):
        tasks = [Task("A", priority=1, burst=2,
                      lock_ops=[LockOp(0, "acquire", "nope")])]
        with pytest.raises(KeyError):
            Scheduler(tasks, {"bus": Mutex("bus")})

    def test_lock_op_after_burst_is_rejected(self):
        with pytest.raises(ValueError):
            Task("A", priority=1, burst=2,
                 lock_ops=[LockOp(5, "acquire", "bus")])


# ── Blocking and handoff ─────────────────────────────────────────────────────

class TestBlockingAndHandoff:
    def test_blocked_task_yields_and_resumes_after_handoff(self):
        tasks = [
            Task("Holder", priority=1, burst=5, arrival_tick=0,
                 lock_ops=[LockOp(1, "acquire", "bus"),
                           LockOp(4, "release", "bus")]),
            Task("Waiter", priority=2, burst=3, arrival_tick=3,
                 lock_ops=[LockOp(1, "acquire", "bus"),
                           LockOp(2, "release", "bus")]),
        ]
        sched, timeline = run(tasks, {"bus": Mutex("bus")})
        waiter = by_name(sched, "Waiter")
        assert waiter.blocked_time > 0
        assert waiter.is_done
        assert sched.mutexes["bus"].contention_count == 1

    def test_highest_priority_waiter_wins_the_handoff(self):
        mutex = Mutex("bus")
        holder = Task("Holder", priority=1, burst=1)
        lo = Task("Lo", priority=1, burst=1)
        hi = Task("Hi", priority=5, burst=1)
        mutex.try_acquire(holder)
        mutex.try_acquire(lo)
        mutex.try_acquire(hi)
        assert mutex.release(holder) is hi

    def test_finishing_while_holding_a_contended_mutex_is_reported(self):
        tasks = [
            Task("Holder", priority=1, burst=4, arrival_tick=0,
                 lock_ops=[LockOp(1, "acquire", "bus")]),   # never released
            Task("Waiter", priority=2, burst=3, arrival_tick=3,
                 lock_ops=[LockOp(1, "acquire", "bus")]),
        ]
        sched, timeline = run(tasks, {"bus": Mutex("bus")}, max_ticks=20)
        assert ticks_where(timeline, "WARNING")


# ── Priority inheritance ─────────────────────────────────────────────────────

class TestPriorityInheritance:
    @staticmethod
    def _inversion_tasks():
        return [
            Task("Low",  priority=1, burst=6, arrival_tick=0,
                 lock_ops=[LockOp(1, "acquire", "bus"),
                           LockOp(4, "release", "bus")]),
            Task("Mid",  priority=2, burst=6, arrival_tick=3),
            Task("High", priority=3, burst=4, arrival_tick=4,
                 lock_ops=[LockOp(1, "acquire", "bus"),
                           LockOp(3, "release", "bus")]),
        ]

    def test_owner_is_boosted_when_a_higher_priority_task_blocks(self):
        sched, timeline = run(self._inversion_tasks(), {"bus": Mutex("bus")})
        assert sched.inheritance_events == 1
        assert ticks_where(timeline, "PRIORITY INHERITANCE")

    def test_priority_is_restored_after_release(self):
        sched, timeline = run(self._inversion_tasks(), {"bus": Mutex("bus")})
        low = by_name(sched, "Low")
        assert low.inherited_priority is None
        assert low.effective_priority == 1

    def test_inheritance_bounds_the_inversion(self):
        """
        The headline claim: with inheritance the high-priority task waits only
        for the critical section; without it, it also waits for every
        medium-priority task that happens to be runnable.
        """
        with_pi, _ = run(self._inversion_tasks(), {"bus": Mutex("bus")},
                         inheritance=True)
        without_pi, _ = run(self._inversion_tasks(), {"bus": Mutex("bus")},
                            inheritance=False)

        blocked_with = by_name(with_pi, "High").blocked_time
        blocked_without = by_name(without_pi, "High").blocked_time
        assert blocked_with < blocked_without
        assert by_name(with_pi, "High").turnaround_time < \
               by_name(without_pi, "High").turnaround_time

    def test_no_inheritance_when_disabled(self):
        sched, _ = run(self._inversion_tasks(), {"bus": Mutex("bus")},
                       inheritance=False)
        assert sched.inheritance_events == 0
        assert by_name(sched, "Low").effective_priority == 1

    def test_boost_does_not_lower_an_already_higher_priority(self):
        """A low-priority waiter must never drag the owner down."""
        tasks = [
            Task("Owner",  priority=5, burst=5, arrival_tick=0,
                 lock_ops=[LockOp(1, "acquire", "bus"),
                           LockOp(4, "release", "bus")]),
            Task("Beggar", priority=1, burst=2, arrival_tick=0,
                 lock_ops=[LockOp(0, "acquire", "bus")]),
        ]
        sched, _ = run(tasks, {"bus": Mutex("bus")}, max_ticks=20)
        assert by_name(sched, "Owner").effective_priority >= 5
        assert sched.inheritance_events == 0

    def test_owner_keeps_boost_while_a_second_mutex_is_contended(self):
        """Releasing one mutex must not drop a boost the other still needs."""
        owner = Task("Owner", priority=1, burst=1)
        hi = Task("Hi", priority=7, burst=1)
        mid = Task("Mid", priority=4, burst=1)
        a, b = Mutex("a"), Mutex("b")
        a.try_acquire(owner)
        b.try_acquire(owner)
        a.try_acquire(hi)      # hi waits on a
        b.try_acquire(mid)     # mid waits on b

        sched = Scheduler([owner, hi, mid], {"a": a, "b": b})
        owner.inherited_priority = 7
        a.release(owner)                      # hands 'a' to hi
        sched._recompute_inheritance(owner)
        # 'b' is still held and still wanted by mid (priority 4).
        assert owner.effective_priority == 4


# ── Deadlock detection ───────────────────────────────────────────────────────

class TestDeadlockDetection:
    @staticmethod
    def _deadlock_tasks():
        return [
            Task("Alpha", priority=1, burst=10, arrival_tick=0,
                 lock_ops=[LockOp(1, "acquire", "A"),
                           LockOp(3, "acquire", "B")]),
            Task("Beta",  priority=2, burst=10, arrival_tick=2,
                 lock_ops=[LockOp(1, "acquire", "B"),
                           LockOp(2, "acquire", "A")]),
        ]

    @staticmethod
    def _mutexes():
        return {"A": Mutex("A"), "B": Mutex("B")}

    def test_circular_wait_is_detected(self):
        sched, timeline = run(self._deadlock_tasks(), self._mutexes())
        assert sched.deadlock_tick is not None
        assert set(sched.deadlock_cycle) == {"Alpha", "Beta"}
        assert timeline[-1]["deadlock"] is True

    def test_detected_even_when_an_unrelated_task_is_still_runnable(self):
        """
        Regression: detection used to require that *every* non-done task was
        blocked, so a single unrelated runnable task hid a real deadlock.
        """
        tasks = self._deadlock_tasks()
        tasks.append(Task("Idler", priority=0, burst=20, arrival_tick=0))
        sched, _ = run(tasks, self._mutexes())
        assert sched.deadlock_tick is not None
        assert set(sched.deadlock_cycle) == {"Alpha", "Beta"}
        assert by_name(sched, "Idler").state is TaskState.READY

    def test_blocking_on_a_running_tasks_lock_is_not_a_deadlock(self):
        """
        Regression: "all non-done tasks are blocked" is not deadlock. Here
        Waiter blocks on a lock the still-runnable Holder will release.
        """
        tasks = [
            Task("Holder", priority=1, burst=5, arrival_tick=0,
                 lock_ops=[LockOp(1, "acquire", "bus"),
                           LockOp(4, "release", "bus")]),
            Task("Waiter", priority=2, burst=3, arrival_tick=3,
                 lock_ops=[LockOp(1, "acquire", "bus"),
                           LockOp(2, "release", "bus")]),
        ]
        sched, _ = run(tasks, {"bus": Mutex("bus")})
        assert sched.deadlock_tick is None
        assert all(t.is_done for t in sched.tasks)

    def test_three_way_cycle_is_detected(self):
        tasks = [
            Task("T1", priority=1, burst=10,
                 lock_ops=[LockOp(1, "acquire", "m1"), LockOp(4, "acquire", "m2")]),
            Task("T2", priority=2, burst=10, arrival_tick=2,
                 lock_ops=[LockOp(1, "acquire", "m2"), LockOp(3, "acquire", "m3")]),
            Task("T3", priority=3, burst=10, arrival_tick=4,
                 lock_ops=[LockOp(1, "acquire", "m3"), LockOp(2, "acquire", "m1")]),
        ]
        mutexes = {"m1": Mutex("m1"), "m2": Mutex("m2"), "m3": Mutex("m3")}
        sched, _ = run(tasks, mutexes, max_ticks=40)
        assert sched.deadlock_tick is not None
        assert set(sched.deadlock_cycle) == {"T1", "T2", "T3"}

    def test_no_deadlock_reported_for_a_clean_run(self):
        tasks = [Task("A", priority=1, burst=3), Task("B", priority=2, burst=3)]
        sched, _ = run(tasks)
        assert sched.deadlock_tick is None
        assert sched.deadlock_cycle is None

    def test_deadlocked_tick_is_not_counted_as_cpu_work(self):
        sched, _ = run(self._deadlock_tasks(), self._mutexes())
        stats = sched.get_statistics()
        assert stats["executing_ticks"] < stats["total_ticks"]
        assert stats["cpu_utilisation"] < 100.0


# ── Timeline fidelity ────────────────────────────────────────────────────────

class TestTimelineRecording:
    def test_running_task_is_never_recorded_as_done(self):
        """
        Regression: the timeline stored a shallow copy of the Task taken
        *after* it had been mutated, so a task that finished on tick N was
        drawn as DONE for the tick it was actually running.
        """
        tasks = [Task("A", priority=1, burst=3), Task("B", priority=2, burst=2)]
        _, timeline = run(tasks)
        for entry in timeline:
            if entry["running"] is not None:
                assert entry["state"] == "RUNNING"

    def test_remaining_burst_decreases_monotonically_per_task(self):
        tasks = [Task("A", priority=2, burst=4), Task("B", priority=1, burst=3)]
        _, timeline = run(tasks)
        seen = {}
        for entry in timeline:
            name = entry["running"]
            if name is None:
                continue
            if name in seen:
                assert entry["remaining"] < seen[name]
            seen[name] = entry["remaining"]

    def test_every_tick_is_recorded_exactly_once(self):
        tasks = [Task("A", priority=1, burst=3)]
        _, timeline = run(tasks, max_ticks=10)
        assert [e["tick"] for e in timeline] == list(range(len(timeline)))


# ── Statistics ───────────────────────────────────────────────────────────────

class TestStatistics:
    def test_wait_and_blocked_time_are_tracked_separately(self):
        tasks = [
            Task("Holder", priority=1, burst=5, arrival_tick=0,
                 lock_ops=[LockOp(1, "acquire", "bus"),
                           LockOp(4, "release", "bus")]),
            Task("Waiter", priority=2, burst=3, arrival_tick=3,
                 lock_ops=[LockOp(1, "acquire", "bus"),
                           LockOp(2, "release", "bus")]),
        ]
        sched, _ = run(tasks, {"bus": Mutex("bus")})
        waiter = by_name(sched, "Waiter")
        assert waiter.blocked_time > 0
        # Waiter is top priority whenever it is ready, so it never merely waits.
        assert waiter.wait_time == 0

    def test_utilisation_is_100_percent_when_never_idle(self):
        tasks = [Task("A", priority=1, burst=4), Task("B", priority=2, burst=4)]
        sched, _ = run(tasks)
        assert sched.get_statistics()["cpu_utilisation"] == 100.0

    def test_idle_ticks_counted_before_arrival(self):
        tasks = [Task("A", priority=1, burst=2, arrival_tick=4)]
        sched, _ = run(tasks, max_ticks=10)
        stats = sched.get_statistics()
        assert stats["idle_ticks"] == 4
        assert stats["cpu_utilisation"] == pytest.approx(33.3, abs=0.1)

    def test_turnaround_and_response_times(self):
        tasks = [Task("A", priority=1, burst=3, arrival_tick=2)]
        sched, _ = run(tasks, max_ticks=10)
        task = by_name(sched, "A")
        assert task.response_time == 0        # ran as soon as it arrived
        assert task.turnaround_time == 3


# ── Stall (distinct from deadlock) ───────────────────────────────────────────

class TestStallDetection:
    def test_stranded_waiter_halts_instead_of_idling(self):
        """
        A task that finishes while holding a mutex strands its waiters. There
        is no cycle, so it is not a deadlock -- but nothing can ever run
        again either, and idling out the remaining ticks would bury the
        problem under a wall of CPU IDLE lines.
        """
        tasks = [
            Task("Holder", priority=1, burst=4, arrival_tick=0,
                 lock_ops=[LockOp(1, "acquire", "bus")]),      # never released
            Task("Waiter", priority=2, burst=3, arrival_tick=3,
                 lock_ops=[LockOp(1, "acquire", "bus")]),
        ]
        sched, timeline = run(tasks, {"bus": Mutex("bus")}, max_ticks=60)

        assert sched.stalled_tick is not None
        assert sched.deadlock_tick is None          # no cycle: owner is DONE
        assert timeline[-1]["stalled"] is True
        assert len(timeline) < 20                   # halted, did not idle out
        assert ticks_where(timeline, "STALLED")
        assert by_name(sched, "Waiter").is_blocked

    def test_waiting_for_a_late_arrival_is_not_a_stall(self):
        tasks = [Task("Late", priority=1, burst=2, arrival_tick=5)]
        sched, timeline = run(tasks, max_ticks=20)
        assert sched.stalled_tick is None
        assert sched.idle_ticks == 5
        assert by_name(sched, "Late").is_done

    def test_clean_run_never_stalls(self):
        tasks = [
            Task("Holder", priority=1, burst=5, arrival_tick=0,
                 lock_ops=[LockOp(1, "acquire", "bus"),
                           LockOp(4, "release", "bus")]),
            Task("Waiter", priority=2, burst=3, arrival_tick=3,
                 lock_ops=[LockOp(1, "acquire", "bus"),
                           LockOp(2, "release", "bus")]),
        ]
        sched, _ = run(tasks, {"bus": Mutex("bus")})
        assert sched.stalled_tick is None
        assert all(t.is_done for t in sched.tasks)


# ── Task construction guards ─────────────────────────────────────────────────

class TestTaskValidation:
    def test_zero_burst_is_rejected(self):
        """
        A zero-burst task used to be dispatched for one tick before the
        completion check noticed, reporting cpu_ticks_used=1 against burst=0.
        """
        with pytest.raises(ValueError, match="at least one tick"):
            Task("Ghost", priority=1, burst=0)

    def test_negative_arrival_is_rejected(self):
        with pytest.raises(ValueError, match="arrival_tick"):
            Task("Early", priority=1, burst=2, arrival_tick=-1)

    def test_acquire_at_end_of_burst_is_rejected(self):
        """Taking a lock with no work left to do with it is a scenario bug."""
        with pytest.raises(ValueError, match="very end of its burst"):
            Task("A", priority=1, burst=3,
                 lock_ops=[LockOp(3, "acquire", "bus")])

    def test_release_at_end_of_burst_is_allowed(self):
        task = Task("A", priority=1, burst=3,
                    lock_ops=[LockOp(0, "acquire", "bus"),
                              LockOp(3, "release", "bus")])
        assert len(task.lock_ops) == 2
