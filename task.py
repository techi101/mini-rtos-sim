"""
task.py — Task model for MiniRTOS Simulator

Defines the Task class, which represents a single schedulable unit of work.
A Task is the RTOS equivalent of a thread — it has a priority, a burst time
(how much CPU it needs), and moves through a defined set of states.

State machine:
    READY ──► RUNNING ──► DONE
      ▲           │
      │           ▼
      └────── BLOCKED
                  (task is waiting to acquire a mutex)

Two things here mirror real RTOS behaviour and are worth calling out:

1. A task's lock operations are expressed in terms of *its own consumed CPU
   ticks*, not absolute simulation ticks. Real code calls `mutex.acquire()`
   at a point in its own instruction stream; it cannot take a lock while it
   is preempted, blocked, or not yet created. See `LockOp`.

2. A task carries both a base priority and an effective priority. The
   scheduler dispatches on the effective one, which the priority-inheritance
   protocol temporarily raises while the task holds a mutex that a
   higher-priority task is waiting for.
"""

from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, List


class TaskState(Enum):
    """
    The four possible states a task can be in at any point during simulation.

    READY   — task has work to do and is waiting for the CPU
    RUNNING — task currently has the CPU and is executing
    BLOCKED — task is waiting to acquire a mutex (cannot run until it is free)
    DONE    — task has used all of its burst time and has finished
    """
    READY   = auto()
    RUNNING = auto()
    BLOCKED = auto()
    DONE    = auto()


@dataclass(frozen=True)
class LockOp:
    """
    A single mutex operation in a task's instruction stream.

    Attributes:
        after_ticks : Fires once the task has consumed exactly this many of
                      its own CPU ticks. `LockOp(2, "acquire", "uart")` means
                      "after executing 2 ticks of work, call uart.acquire()".
                      This is deliberately relative to the task's own
                      execution, so preemption and blocking shift the
                      operation in wall-clock time exactly as they would in
                      a real system.
        action      : "acquire" or "release".
        mutex       : Name of the mutex to operate on.
    """
    after_ticks : int
    action      : str
    mutex       : str

    def __post_init__(self):
        if self.action not in ("acquire", "release"):
            raise ValueError(f"LockOp action must be 'acquire' or 'release', got {self.action!r}")
        if self.after_ticks < 0:
            raise ValueError("LockOp.after_ticks must be >= 0")


@dataclass
class Task:
    """
    Represents a single task in the RTOS simulation.

    Attributes:
        name         : Human-readable task name (e.g., "SensorRead").
        priority     : Base priority. Higher number = higher priority.
                       Mirrors the ARM Mbed OS convention.
        burst        : Total number of CPU ticks this task needs to complete.
        arrival_tick : The simulation tick at which this task becomes READY.
        lock_ops     : Mutex operations, relative to this task's own consumed
                       CPU ticks. See `LockOp`.
    """
    name         : str
    priority     : int
    burst        : int
    arrival_tick : int = 0
    lock_ops     : List[LockOp] = field(default_factory=list)

    # ── Runtime state (mutated by the scheduler) ──────────────────────────
    state           : TaskState     = field(default=TaskState.READY, init=False)
    remaining_burst : int           = field(default=0,               init=False)
    start_tick      : Optional[int] = field(default=None,            init=False)
    finish_tick     : Optional[int] = field(default=None,            init=False)
    wait_time       : int           = field(default=0,               init=False)
    blocked_time    : int           = field(default=0,               init=False)
    cpu_ticks_used  : int           = field(default=0,               init=False)

    # Which mutex this task is currently blocked on (None if not blocked)
    blocked_on_mutex: Optional[str] = field(default=None, init=False)

    # Mutexes this task currently owns — needed to recompute inherited
    # priority when one of them is released.
    held_mutexes    : List[str]     = field(default_factory=list, init=False)

    # Priority temporarily donated by a higher-priority blocked task.
    # None means "no donation active".
    inherited_priority: Optional[int] = field(default=None, init=False)

    # How many lock ops have already fired (index into a tick-sorted view).
    _fired_ops      : set           = field(default_factory=set, init=False)

    def __post_init__(self):
        """Initialise remaining_burst from burst after dataclass __init__."""
        self.remaining_burst = self.burst
        if any(op.after_ticks > self.burst for op in self.lock_ops):
            raise ValueError(
                f"Task {self.name!r} has a lock op scheduled after more ticks "
                f"than its burst ({self.burst}); it would never fire."
            )

    # ── Priority ──────────────────────────────────────────────────────────

    @property
    def effective_priority(self) -> int:
        """
        The priority the scheduler actually dispatches on.

        Equal to the base priority unless priority inheritance has raised it
        while this task holds a mutex a higher-priority task is waiting for.
        """
        if self.inherited_priority is None:
            return self.priority
        return max(self.priority, self.inherited_priority)

    @property
    def is_priority_boosted(self) -> bool:
        return self.effective_priority > self.priority

    # ── Lock program ──────────────────────────────────────────────────────

    def due_lock_ops(self) -> List[LockOp]:
        """
        Lock operations that should fire now, i.e. whose `after_ticks` equals
        the number of CPU ticks this task has already consumed, and which
        have not fired yet.

        Returned in declaration order so a task can release one mutex and
        acquire another at the same point in its execution.
        """
        return [
            op for i, op in enumerate(self.lock_ops)
            if op.after_ticks == self.cpu_ticks_used and i not in self._fired_ops
        ]

    def mark_op_fired(self, op: LockOp) -> None:
        for i, candidate in enumerate(self.lock_ops):
            if candidate is op and i not in self._fired_ops:
                self._fired_ops.add(i)
                return

    # ── State transition helpers ──────────────────────────────────────────

    def mark_ready(self) -> None:
        self.state = TaskState.READY

    def mark_running(self, current_tick: int) -> None:
        if self.start_tick is None:
            self.start_tick = current_tick
        self.state = TaskState.RUNNING

    def mark_blocked(self, mutex_name: str) -> None:
        self.state = TaskState.BLOCKED
        self.blocked_on_mutex = mutex_name

    def mark_unblocked(self) -> None:
        self.state = TaskState.READY
        self.blocked_on_mutex = None

    def mark_done(self, current_tick: int) -> None:
        self.state = TaskState.DONE
        self.finish_tick = current_tick

    # ── Convenience properties ────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        return self.state == TaskState.READY

    @property
    def is_running(self) -> bool:
        return self.state == TaskState.RUNNING

    @property
    def is_blocked(self) -> bool:
        return self.state == TaskState.BLOCKED

    @property
    def is_done(self) -> bool:
        return self.state == TaskState.DONE

    @property
    def turnaround_time(self) -> Optional[int]:
        """Total time from arrival to completion (finish - arrival)."""
        if self.finish_tick is None:
            return None
        return self.finish_tick - self.arrival_tick

    @property
    def response_time(self) -> Optional[int]:
        """Time from arrival until the task first got the CPU."""
        if self.start_tick is None:
            return None
        return self.start_tick - self.arrival_tick

    def __repr__(self) -> str:
        pri = f"pri={self.priority}"
        if self.is_priority_boosted:
            pri += f"->{self.effective_priority}"
        return (
            f"Task({self.name!r}, {pri}, "
            f"state={self.state.name}, remaining={self.remaining_burst})"
        )
