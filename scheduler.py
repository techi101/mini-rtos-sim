"""
scheduler.py — Priority Preemptive Scheduler for MiniRTOS Simulator

This is the core of the simulation. At each clock tick, the scheduler:
  1. Checks if any new tasks have arrived (their arrival_tick == current_tick)
  2. Checks if the running task should be preempted by a higher-priority task
  3. Runs one tick of the highest-priority READY task
  4. Handles mutex acquire/release events
  5. Detects deadlock conditions
  6. Records the full timeline of events

This implements Priority Preemptive Scheduling — the same algorithm used by
ARM Mbed OS and FreeRTOS as their default scheduling policy.

Key property: At every tick, the task with the HIGHEST priority that is READY
always gets the CPU. If a higher-priority task becomes ready mid-execution of
a lower-priority task, the lower-priority task is immediately PREEMPTED.
"""

import copy
from typing import List, Dict, Optional, Tuple
from task import Task, TaskState
from mutex import Mutex


# A ScheduleEvent records everything that happened at one tick
ScheduleEvent = Dict  # keys: tick, running_task, state, events (list of str)


class Scheduler:
    """
    Simulates a Priority Preemptive RTOS Scheduler.

    Usage:
        tasks   = [Task("A", priority=3, burst=5), Task("B", priority=1, burst=4)]
        mutexes = {"shared_bus": Mutex("shared_bus")}
        # mutex_requests: which task tries to acquire which mutex at which tick
        mutex_requests = {2: ("A", "shared_bus"), 5: ("A", "shared_bus_release")}

        sched = Scheduler(tasks, mutexes, mutex_requests)
        timeline = sched.run(max_ticks=20)
    """

    def __init__(
        self,
        tasks: List[Task],
        mutexes: Dict[str, Mutex],
        mutex_requests: Dict[int, Tuple[str, str]],  # tick → (task_name, "mutex_name" or "mutex_name_release")
    ):
        self.tasks            = tasks
        self.mutexes          = mutexes
        self.mutex_requests   = mutex_requests
        self.timeline: List[ScheduleEvent] = []

        # Statistics
        self.preemption_count = 0
        self.idle_ticks       = 0
        self.deadlock_tick: Optional[int] = None

    def run(self, max_ticks: int) -> List[ScheduleEvent]:
        """
        Execute the simulation for up to `max_ticks` clock ticks,
        stopping early if all tasks are DONE or a deadlock is detected.

        Returns:
            A list of ScheduleEvent dicts, one per tick simulated.
        """
        current_runner: Optional[Task] = None

        for tick in range(max_ticks):
            events: List[str] = []

            # ── Step 1: Activate newly arrived tasks ────────────────────
            for task in self.tasks:
                if task.arrival_tick == tick and task.state == TaskState.READY:
                    events.append(f"ARRIVED: {task.name} entered Ready Queue")

            # ── Step 2: Process any mutex events scheduled at this tick ──
            if tick in self.mutex_requests:
                task_name, action = self.mutex_requests[tick]
                task = self._find_task(task_name)
                if task and not task.is_done:
                    if action.endswith("_release"):
                        mutex_name = action.replace("_release", "")
                        events += self._handle_release(task, mutex_name, tick)
                        if current_runner == task and task.is_done:
                            current_runner = None
                    else:
                        events += self._handle_acquire(task, action, tick)
                        if task.is_blocked and task == current_runner:
                            # Running task got blocked — must yield CPU
                            current_runner = None
                            events.append(f"CPU YIELD: {task.name} released CPU (blocked on mutex)")

            # ── Step 3: Detect deadlock ──────────────────────────────────
            if self._detect_deadlock():
                events.append("⚠️  DEADLOCK DETECTED — simulation halted")
                self.deadlock_tick = tick
                self.timeline.append({
                    "tick": tick,
                    "running_task": current_runner,
                    "events": events,
                    "deadlock": True,
                })
                break

            # ── Step 4: Select the next task to run ─────────────────────
            candidate = self._highest_priority_ready_task(tick)

            if candidate is None:
                # No task ready — CPU is idle this tick
                self.idle_ticks += 1
                events.append("CPU IDLE")
                self.timeline.append({
                    "tick": tick,
                    "running_task": None,
                    "events": events,
                    "deadlock": False,
                })
                current_runner = None
                continue

            # ── Step 5: Check for preemption ────────────────────────────
            if (current_runner is not None
                    and current_runner.is_running
                    and candidate.priority > current_runner.priority):
                events.append(
                    f"PREEMPT: {candidate.name} (pri={candidate.priority}) "
                    f"preempts {current_runner.name} (pri={current_runner.priority})"
                )
                current_runner.mark_ready()
                self.preemption_count += 1
                current_runner = None

            # ── Step 6: Run the selected task for one tick ───────────────
            if current_runner is None or not current_runner.is_running:
                current_runner = candidate
                current_runner.mark_running(tick)

            current_runner.remaining_burst -= 1
            current_runner.cpu_ticks_used  += 1

            # Accumulate wait time for all ready (but not running) tasks
            for task in self.tasks:
                if task.is_ready and task != current_runner and task.arrival_tick <= tick:
                    task.wait_time += 1

            # ── Step 7: Check if the running task has finished ───────────
            if current_runner.remaining_burst <= 0:
                current_runner.mark_done(tick + 1)
                events.append(f"DONE: {current_runner.name} completed at tick {tick + 1}")
                current_runner = None

            # ── Step 8: Record this tick ─────────────────────────────────
            self.timeline.append({
                "tick": tick,
                "running_task": copy.copy(candidate) if candidate else None,
                "events": events,
                "deadlock": False,
            })

            # ── Step 9: Early exit if all tasks are done ─────────────────
            if all(t.is_done for t in self.tasks):
                break

        return self.timeline

    # ── Private helpers ────────────────────────────────────────────────────

    def _highest_priority_ready_task(self, current_tick: int) -> Optional[Task]:
        """
        Return the READY task with the highest priority that has already arrived.
        If the current runner is still running (not preempted), it is included.
        """
        candidates = [
            t for t in self.tasks
            if (t.is_ready or t.is_running)
            and t.arrival_tick <= current_tick
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda t: t.priority)

    def _handle_acquire(self, task: Task, mutex_name: str, tick: int) -> List[str]:
        events = []
        if mutex_name not in self.mutexes:
            return [f"ERROR: Mutex '{mutex_name}' not found"]
        mutex = self.mutexes[mutex_name]
        acquired = mutex.try_acquire(task)
        if acquired:
            events.append(f"MUTEX: {task.name} ACQUIRED '{mutex_name}'")
        else:
            task.mark_blocked(mutex_name)
            events.append(
                f"MUTEX: {task.name} BLOCKED on '{mutex_name}' "
                f"(held by {mutex.owner.name})"
            )
        return events

    def _handle_release(self, task: Task, mutex_name: str, tick: int) -> List[str]:
        events = []
        if mutex_name not in self.mutexes:
            return [f"ERROR: Mutex '{mutex_name}' not found"]
        mutex = self.mutexes[mutex_name]
        try:
            next_owner = mutex.release(task)
            events.append(f"MUTEX: {task.name} RELEASED '{mutex_name}'")
            if next_owner:
                next_owner.mark_unblocked()
                events.append(f"MUTEX: {next_owner.name} UNBLOCKED — acquired '{mutex_name}'")
        except RuntimeError as e:
            events.append(f"ERROR: {e}")
        return events

    def _detect_deadlock(self) -> bool:
        """
        Simple cycle detection: a deadlock exists if every non-done task is
        BLOCKED and at least two tasks are in a circular wait.

        A true deadlock requires a cycle in the resource-allocation graph.
        This simplified version flags it when all non-done tasks are blocked.
        """
        non_done = [t for t in self.tasks if not t.is_done]
        if not non_done:
            return False
        return all(t.is_blocked for t in non_done) and len(non_done) >= 2

    def _find_task(self, name: str) -> Optional[Task]:
        for t in self.tasks:
            if t.name == name:
                return t
        return None

    # ── Summary statistics ────────────────────────────────────────────────

    def get_statistics(self) -> Dict:
        total_ticks = len(self.timeline)
        return {
            "total_ticks"      : total_ticks,
            "idle_ticks"       : self.idle_ticks,
            "cpu_utilisation"  : round((1 - self.idle_ticks / max(total_ticks, 1)) * 100, 1),
            "preemption_count" : self.preemption_count,
            "deadlock_tick"    : self.deadlock_tick,
            "tasks"            : [
                {
                    "name"            : t.name,
                    "priority"        : t.priority,
                    "state"           : t.state.name,
                    "cpu_ticks_used"  : t.cpu_ticks_used,
                    "wait_time"       : t.wait_time,
                    "finish_tick"     : t.finish_tick,
                    "turnaround_time" : t.turnaround_time,
                }
                for t in self.tasks
            ],
            "mutex_stats"      : [
                {
                    "name"             : m.name,
                    "acquisition_count": m.acquisition_count,
                    "contention_count" : m.contention_count,
                }
                for m in self.mutexes.values()
            ],
        }
