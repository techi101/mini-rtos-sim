"""
scheduler.py — Priority Preemptive Scheduler for MiniRTOS Simulator

This is the core of the simulation. At each clock tick, the scheduler:
  1. Admits any task whose arrival_tick has come round
  2. Picks the highest effective-priority runnable task
  3. Fires that task's due lock operations before it executes
  4. Runs it for one tick, preempting a lower-priority incumbent if needed
  5. Applies priority inheritance when a task blocks on a held mutex
  6. Detects circular waits in the wait-for graph
  7. Records the full timeline of events

This implements Priority Preemptive Scheduling — the same policy ARM Mbed OS
and FreeRTOS use by default.

Three design points worth stating explicitly, because they are where a naive
simulator goes wrong:

**Lock operations follow execution, not the wall clock.** A task's mutex calls
are scheduled against its own consumed CPU ticks (`Task.lock_ops`). A task
that has been preempted, blocked, or has not arrived yet cannot take a lock,
because it is not executing any instructions. Keying lock operations to
absolute simulation ticks — as an earlier version of this file did — lets a
task acquire a mutex while some *other* task holds the CPU, which no real
system can do and which makes every scenario silently dependent on the exact
tick numbers written into it.

**Ties do not cause a context switch.** When several tasks share the highest
effective priority, the incumbent keeps the CPU. That matches Mbed OS and
FreeRTOS with time-slicing disabled; round-robin among equals would need an
explicit quantum.

**Deadlock means a cycle, not "everything is stuck".** Detection walks the
wait-for graph (blocked task → owner of the mutex it wants) and looks for a
genuine cycle. Two consequences: a task blocked on a lock held by a task that
is still running is *not* reported as deadlock, and a real circular wait *is*
reported even when unrelated tasks are still happily runnable.
"""

from typing import List, Dict, Optional

from task import Task, TaskState
from mutex import Mutex


# A ScheduleEvent records everything that happened at one tick.
# Plain values only — an earlier version stored a shallow copy of the Task,
# which was taken after the task had already been mutated, so a task that
# finished on tick N was rendered as DONE for the tick it actually ran.
ScheduleEvent = Dict


class Scheduler:
    """
    Simulates a Priority Preemptive RTOS Scheduler.

    Usage:
        tasks = [
            Task("A", priority=3, burst=5,
                 lock_ops=[LockOp(1, "acquire", "bus"), LockOp(4, "release", "bus")]),
            Task("B", priority=1, burst=4),
        ]
        mutexes = {"bus": Mutex("bus")}

        sched = Scheduler(tasks, mutexes)
        timeline = sched.run(max_ticks=20)
    """

    def __init__(
        self,
        tasks: List[Task],
        mutexes: Optional[Dict[str, Mutex]] = None,
        enable_priority_inheritance: bool = True,
    ):
        self.tasks   = tasks
        self.mutexes = mutexes or {}
        self.enable_priority_inheritance = enable_priority_inheritance
        self.timeline: List[ScheduleEvent] = []

        self._validate_lock_ops()

        # Statistics
        self.preemption_count   = 0
        self.idle_ticks         = 0
        self.executing_ticks    = 0
        self.inheritance_events = 0
        self.deadlock_tick: Optional[int] = None
        self.deadlock_cycle: Optional[List[str]] = None
        self.stalled_tick: Optional[int] = None

    def _validate_lock_ops(self) -> None:
        """Fail loudly on a scenario that names a mutex that does not exist."""
        for task in self.tasks:
            for op in task.lock_ops:
                if op.mutex not in self.mutexes:
                    raise KeyError(
                        f"Task {task.name!r} has a lock op on unknown mutex "
                        f"{op.mutex!r}. Known mutexes: {sorted(self.mutexes)}"
                    )

    # ── Main loop ──────────────────────────────────────────────────────────

    def run(self, max_ticks: int) -> List[ScheduleEvent]:
        """
        Execute the simulation for up to `max_ticks` clock ticks, stopping
        early if all tasks are DONE or a circular wait is detected.

        Returns:
            A list of ScheduleEvent dicts, one per tick simulated.
        """
        current_runner: Optional[Task] = None

        for tick in range(max_ticks):
            events: List[str] = []

            # ── Step 1: Admit newly arrived tasks ────────────────────────
            for task in self.tasks:
                if task.arrival_tick == tick and not task.is_done:
                    events.append(f"ARRIVED: {task.name} entered Ready Queue")

            # ── Step 2: Find a task that can actually execute this tick ──
            # A selected task may issue a blocking acquire before it runs, in
            # which case it yields and we pick again. Bounded by the number
            # of tasks, so this cannot spin.
            runner: Optional[Task] = None
            deadlocked = False

            max_attempts = len(self.tasks) + 2
            for attempt in range(max_attempts):
                candidate = self._select(tick, current_runner)
                if candidate is None:
                    break

                if self._fire_due_lock_ops(candidate, events):
                    # candidate blocked on an acquire and cannot run
                    if current_runner is candidate:
                        current_runner = None
                        events.append(
                            f"CPU YIELD: {candidate.name} released CPU (blocked on mutex)"
                        )
                    cycle = self._find_deadlock_cycle()
                    if cycle:
                        self.deadlock_tick = tick
                        self.deadlock_cycle = cycle
                        events.append(
                            "DEADLOCK DETECTED — circular wait: "
                            + " -> ".join(cycle + [cycle[0]])
                        )
                        deadlocked = True
                        break
                    continue

                # A release hands the mutex straight to a waiter, which can
                # make a higher-priority task runnable. A real kernel
                # reschedules at that instant instead of letting the releasing
                # task finish the tick, so re-select before dispatching.
                # Lock ops are one-shot, so this converges.
                better = self._select(tick, current_runner)
                if better is not candidate and attempt < max_attempts - 1:
                    continue

                runner = candidate
                break

            if deadlocked:
                self.timeline.append(self._record(tick, None, events,
                                                  deadlock=True))
                break

            # ── Step 3: Nothing runnable — the CPU idles ─────────────────
            if runner is None:
                # Distinguish "waiting for a task to arrive" from "nothing can
                # ever run again". The latter happens when a task finishes
                # while still holding a mutex: its waiters are blocked forever,
                # but there is no cycle, so it is not a deadlock. Idling out
                # the remaining ticks would bury the problem in CPU IDLE lines.
                if self._is_stalled(tick):
                    stranded = ", ".join(
                        f"{t.name} on '{t.blocked_on_mutex}'"
                        for t in self.tasks if t.is_blocked)
                    events.append(
                        f"STALLED — no task can ever run again; blocked: {stranded}"
                    )
                    self.stalled_tick = tick
                    self.timeline.append(self._record(tick, None, events,
                                                      stalled=True))
                    break

                self.idle_ticks += 1
                events.append("CPU IDLE")
                self._accumulate_waiting(tick, runner=None)
                self.timeline.append(self._record(tick, None, events))
                current_runner = None
                continue

            # ── Step 4: Preempt the incumbent if it lost the CPU ─────────
            if (current_runner is not None
                    and current_runner is not runner
                    and current_runner.is_running):
                events.append(
                    f"PREEMPT: {runner.name} (pri={self._pri_str(runner)}) "
                    f"preempts {current_runner.name} (pri={self._pri_str(current_runner)})"
                )
                current_runner.mark_ready()
                self.preemption_count += 1

            if not runner.is_running:
                runner.mark_running(tick)
            current_runner = runner

            # ── Step 5: Execute one tick ─────────────────────────────────
            runner.remaining_burst -= 1
            runner.cpu_ticks_used  += 1
            self.executing_ticks   += 1
            self._accumulate_waiting(tick, runner)

            record = self._record(tick, runner, events)

            # ── Step 6: Completion ───────────────────────────────────────
            if runner.remaining_burst <= 0:
                # A release scheduled at the very end of the burst fires here,
                # before the task leaves the CPU — otherwise it would take its
                # mutexes to the grave and block every waiter forever.
                self._fire_due_lock_ops(runner, events, releases_only=True)
                self._warn_on_leaked_mutexes(runner, events)
                runner.mark_done(tick + 1)
                events.append(f"DONE: {runner.name} completed at tick {tick + 1}")
                record["finished"] = True
                current_runner = None

            self.timeline.append(record)

            # ── Step 7: Early exit if everything finished ────────────────
            if all(t.is_done for t in self.tasks):
                break

        return self.timeline

    # ── Selection ──────────────────────────────────────────────────────────

    def _select(self, current_tick: int, incumbent: Optional[Task]) -> Optional[Task]:
        """
        Highest effective-priority task that has arrived and is runnable.

        On a tie the incumbent keeps the CPU (no gratuitous context switch);
        otherwise declaration order breaks the tie, which keeps runs
        deterministic.
        """
        candidates = [
            t for t in self.tasks
            if (t.is_ready or t.is_running) and t.arrival_tick <= current_tick
        ]
        if not candidates:
            return None

        best = max(t.effective_priority for t in candidates)
        top = [t for t in candidates if t.effective_priority == best]
        if incumbent is not None and incumbent in top:
            return incumbent
        return top[0]

    # ── Lock operations ────────────────────────────────────────────────────

    def _fire_due_lock_ops(self, task: Task, events: List[str],
                           releases_only: bool = False) -> bool:
        """
        Run every lock op whose trigger point the task has just reached.

        Returns:
            True if the task ended up BLOCKED and must not execute this tick.
        """
        for op in task.due_lock_ops():
            if releases_only and op.action != "release":
                continue
            task.mark_op_fired(op)
            if op.action == "acquire":
                if self._do_acquire(task, op.mutex, events):
                    return True
            else:
                self._do_release(task, op.mutex, events)
        return False

    def _do_acquire(self, task: Task, mutex_name: str, events: List[str]) -> bool:
        """Returns True if the task blocked."""
        mutex = self.mutexes[mutex_name]
        if mutex.try_acquire(task):
            events.append(f"MUTEX: {task.name} ACQUIRED '{mutex_name}'")
            return False

        task.mark_blocked(mutex_name)
        events.append(
            f"MUTEX: {task.name} BLOCKED on '{mutex_name}' "
            f"(held by {mutex.owner.name})"
        )
        if self.enable_priority_inheritance:
            self._propagate_inheritance(task, events)
        return True

    def _do_release(self, task: Task, mutex_name: str, events: List[str]) -> None:
        mutex = self.mutexes[mutex_name]
        try:
            next_owner = mutex.release(task)
        except RuntimeError as exc:
            events.append(f"ERROR: {exc}")
            return

        events.append(f"MUTEX: {task.name} RELEASED '{mutex_name}'")

        # The releasing task may no longer need its donated priority.
        if self.enable_priority_inheritance:
            before = task.effective_priority
            self._recompute_inheritance(task)
            if task.effective_priority != before:
                events.append(
                    f"PRIORITY RESTORED: {task.name} {before} -> "
                    f"{task.effective_priority}"
                )

        if next_owner is not None:
            next_owner.mark_unblocked()
            events.append(
                f"MUTEX: {next_owner.name} UNBLOCKED — acquired '{mutex_name}'"
            )

    def _warn_on_leaked_mutexes(self, task: Task, events: List[str]) -> None:
        """A task that finishes still holding a mutex would strand its waiters."""
        for name in list(task.held_mutexes):
            waiters = self.mutexes[name].waiting_tasks
            if waiters:
                events.append(
                    f"WARNING: {task.name} finished still holding '{name}'; "
                    f"{', '.join(w.name for w in waiters)} will never wake"
                )

    # ── Priority inheritance ───────────────────────────────────────────────

    def _propagate_inheritance(self, blocked_task: Task, events: List[str]) -> None:
        """
        Donate `blocked_task`'s priority down the chain of mutex owners.

        Without this, a low-priority task holding a mutex can be preempted by
        an unrelated medium-priority task while a high-priority task waits on
        that mutex — the classic unbounded priority inversion (the bug that
        put Mars Pathfinder into a reset loop in 1997).

        The walk is transitive: if the owner is itself blocked on another
        mutex, the boost passes along. It is bounded by the task count, so a
        circular wait terminates it rather than spinning.
        """
        donor_priority = blocked_task.effective_priority
        node = blocked_task

        for _ in range(len(self.tasks) + 1):
            mutex_name = node.blocked_on_mutex
            if mutex_name is None:
                return
            mutex = self.mutexes.get(mutex_name)
            if mutex is None or mutex.owner is None:
                return

            owner = mutex.owner
            if owner is blocked_task or owner.effective_priority >= donor_priority:
                return

            previous = owner.effective_priority
            owner.inherited_priority = donor_priority
            self.inheritance_events += 1
            events.append(
                f"PRIORITY INHERITANCE: {owner.name} boosted {previous} -> "
                f"{donor_priority} (holds '{mutex_name}', needed by {blocked_task.name})"
            )

            if not owner.is_blocked:
                return
            node = owner

    def _recompute_inheritance(self, task: Task) -> None:
        """
        Recalculate a task's donated priority from the mutexes it still holds.

        Dropping straight back to the base priority would be wrong when the
        task holds a second contended mutex.
        """
        best: Optional[int] = None
        for name in task.held_mutexes:
            mutex = self.mutexes.get(name)
            if mutex is None:
                continue
            waiter_priority = mutex.highest_waiter_priority()
            if waiter_priority is not None and (best is None or waiter_priority > best):
                best = waiter_priority
        task.inherited_priority = best

    # ── Deadlock detection ─────────────────────────────────────────────────

    def _wait_for_edges(self) -> Dict[str, str]:
        """
        Build the wait-for graph: blocked task → owner of the mutex it wants.

        Each blocked task waits on exactly one mutex, so every node has at
        most one outgoing edge and cycle detection is an exact chain walk.
        """
        edges: Dict[str, str] = {}
        for task in self.tasks:
            if not task.is_blocked or task.blocked_on_mutex is None:
                continue
            mutex = self.mutexes.get(task.blocked_on_mutex)
            if mutex is None or mutex.owner is None or mutex.owner is task:
                continue
            edges[task.name] = mutex.owner.name
        return edges

    def _is_stalled(self, current_tick: int) -> bool:
        """
        True when no task is runnable and none ever will be again.

        Distinct from deadlock: there is no cycle here. It happens when a task
        finishes holding a mutex, stranding its waiters on a lock whose owner
        is already DONE and will never release it. Nothing else can arrive and
        nothing can wake them, so the run may as well stop and say so.
        """
        pending = [t for t in self.tasks if not t.is_done]
        if not pending:
            return False
        if any(t.arrival_tick > current_tick for t in pending):
            return False                       # someone is still to arrive
        if any(t.is_ready or t.is_running for t in pending):
            return False                       # someone can still run

        # Every pending task is blocked. If any waits on a mutex whose owner
        # is still pending, that owner may yet release it (that case is either
        # progress or a cycle, both handled elsewhere).
        for task in pending:
            mutex = self.mutexes.get(task.blocked_on_mutex or "")
            if mutex is not None and mutex.owner is not None \
                    and not mutex.owner.is_done:
                return False
        return True

    def _find_deadlock_cycle(self) -> Optional[List[str]]:
        """
        Return the task names forming a circular wait, or None.

        Note what is *not* here: any test for "all tasks are blocked". A
        circular wait between two tasks is a deadlock whether or not a third
        task is still running, and a system where every task happens to be
        blocked on a lock held by a running task is not deadlocked at all.
        """
        edges = self._wait_for_edges()
        for start in edges:
            seen: List[str] = []
            node = start
            while node in edges:
                if node in seen:
                    return seen[seen.index(node):]
                seen.append(node)
                node = edges[node]
        return None

    # ── Bookkeeping helpers ────────────────────────────────────────────────

    def _accumulate_waiting(self, tick: int, runner: Optional[Task]) -> None:
        """Charge a tick of waiting or blocking to every task that isn't running."""
        for task in self.tasks:
            if task is runner or task.is_done or task.arrival_tick > tick:
                continue
            if task.is_blocked:
                task.blocked_time += 1
            else:
                task.wait_time += 1

    def _pri_str(self, task: Task) -> str:
        if task.is_priority_boosted:
            return f"{task.priority}->{task.effective_priority}"
        return str(task.priority)

    def _record(self, tick: int, runner: Optional[Task], events: List[str],
                deadlock: bool = False, stalled: bool = False) -> ScheduleEvent:
        """Snapshot one tick as plain values, taken at the moment it ran."""
        if runner is None:
            state = "DEADLOCK" if deadlock else "STALLED" if stalled else "IDLE"
            return {
                "tick": tick, "running": None, "priority": None,
                "effective_priority": None, "remaining": None,
                "state": state, "events": events,
                "deadlock": deadlock, "stalled": stalled, "finished": False,
            }
        return {
            "tick": tick,
            "running": runner.name,
            "priority": runner.priority,
            "effective_priority": runner.effective_priority,
            "remaining": runner.remaining_burst,
            "state": "RUNNING",
            "events": events,
            "deadlock": False,
            "stalled": False,
            "finished": False,
        }

    def _find_task(self, name: str) -> Optional[Task]:
        for task in self.tasks:
            if task.name == name:
                return task
        return None

    # ── Summary statistics ────────────────────────────────────────────────

    def get_statistics(self) -> Dict:
        total_ticks = len(self.timeline)
        return {
            "total_ticks"        : total_ticks,
            "idle_ticks"         : self.idle_ticks,
            "executing_ticks"    : self.executing_ticks,
            # Utilisation counts only ticks that actually advanced a task.
            # A deadlocked tick executes nothing and must not read as 100%.
            "cpu_utilisation"    : round(self.executing_ticks / max(total_ticks, 1) * 100, 1),
            "preemption_count"   : self.preemption_count,
            "inheritance_events" : self.inheritance_events,
            "deadlock_tick"      : self.deadlock_tick,
            "deadlock_cycle"     : self.deadlock_cycle,
            "stalled_tick"       : self.stalled_tick,
            "priority_inheritance_enabled": self.enable_priority_inheritance,
            "tasks"              : [
                {
                    "name"            : t.name,
                    "priority"        : t.priority,
                    "effective_priority": t.effective_priority,
                    "state"           : t.state.name,
                    "cpu_ticks_used"  : t.cpu_ticks_used,
                    "wait_time"       : t.wait_time,
                    "blocked_time"    : t.blocked_time,
                    "finish_tick"     : t.finish_tick,
                    "turnaround_time" : t.turnaround_time,
                    "response_time"   : t.response_time,
                }
                for t in self.tasks
            ],
            "mutex_stats"        : [
                {
                    "name"             : m.name,
                    "acquisition_count": m.acquisition_count,
                    "contention_count" : m.contention_count,
                    "owner"            : m.owner.name if m.owner else None,
                }
                for m in self.mutexes.values()
            ],
        }
