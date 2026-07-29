"""
task.py — Task model for MiniRTOS Simulator

Defines the Task class, which represents a single schedulable unit of work.
A Task is the RTOS equivalent of a thread or process — it has a priority,
a burst time (how much CPU it needs), and moves through a defined set of states.

State machine:
    READY ──► RUNNING ──► DONE
      ▲           │
      │           ▼
      └────── BLOCKED
                  (task is waiting to acquire a mutex)

This mirrors exactly how a real RTOS like ARM Mbed OS or FreeRTOS manages tasks.
"""

from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional


class TaskState(Enum):
    """
    The four possible states a task can be in at any point during simulation.

    READY   — task has work to do and is waiting for the CPU
    RUNNING — task currently has the CPU and is executing
    BLOCKED — task is waiting to acquire a mutex (cannot run until the mutex is free)
    DONE    — task has used all of its burst time and has finished
    """
    READY   = auto()
    RUNNING = auto()
    BLOCKED = auto()
    DONE    = auto()


@dataclass
class Task:
    """
    Represents a single task in the RTOS simulation.

    Attributes:
        name         : Human-readable task name (e.g., "SensorRead").
        priority     : Integer priority. Higher number = higher priority.
                       Mirrors ARM Mbed OS convention where higher value = higher priority.
        burst        : Total number of CPU ticks this task needs to complete.
        arrival_tick : The simulation tick at which this task first becomes READY.
                       Models real-world scenarios where tasks are not all created at t=0.
    """
    name         : str
    priority     : int
    burst        : int
    arrival_tick : int = 0

    # ── Runtime state (mutated by the scheduler) ──────────────────────────
    state           : TaskState     = field(default=TaskState.READY, init=False)
    remaining_burst : int           = field(default=0,               init=False)
    start_tick      : Optional[int] = field(default=None,            init=False)
    finish_tick     : Optional[int] = field(default=None,            init=False)
    wait_time       : int           = field(default=0,               init=False)
    cpu_ticks_used  : int           = field(default=0,               init=False)

    # Which mutex this task is currently blocked on (None if not blocked)
    blocked_on_mutex: Optional[str] = field(default=None, init=False)

    def __post_init__(self):
        """Initialise remaining_burst from burst after dataclass __init__."""
        self.remaining_burst = self.burst

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

    def __repr__(self) -> str:
        return (
            f"Task({self.name!r}, pri={self.priority}, "
            f"state={self.state.name}, remaining={self.remaining_burst})"
        )
