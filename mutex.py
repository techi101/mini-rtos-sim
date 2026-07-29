"""
mutex.py — Mutex implementation for MiniRTOS Simulator

A mutex (mutual exclusion lock) is the fundamental synchronisation primitive
in embedded RTOS programming. It prevents two tasks from simultaneously
accessing a shared resource (e.g., a UART peripheral, a shared memory buffer).

How a real RTOS mutex works:
  1. Task A calls mutex.acquire() → succeeds, task A now "owns" the mutex.
  2. Task B calls mutex.acquire() → fails (mutex is held), task B is BLOCKED.
  3. Task A calls mutex.release() → mutex is free again.
  4. Task B is UNBLOCKED and moved back to READY state.

This is exactly what this class simulates. The waiting_tasks list represents
the queue of tasks that are blocked waiting for this mutex — in a real RTOS
this is managed by the kernel's block list.

ARM Mbed OS Mutex documentation:
  https://os.mbed.com/docs/mbed-os/v6.16/apis/mutex.html
"""

from typing import Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from task import Task


class Mutex:
    """
    Simulates a binary mutex (non-recursive).

    Attributes:
        name         : Identifier for this mutex (e.g., "uart_lock").
        owner        : The Task that currently holds this mutex, or None.
        waiting_tasks: Ordered list of Tasks blocked waiting to acquire it.
    """

    def __init__(self, name: str):
        self.name: str = name
        self.owner: Optional["Task"] = None
        self.waiting_tasks: List["Task"] = []
        self.acquisition_count: int = 0   # total number of times acquired
        self.contention_count: int  = 0   # times a task was blocked on this mutex

    @property
    def is_locked(self) -> bool:
        """True if any task currently holds this mutex."""
        return self.owner is not None

    @property
    def is_free(self) -> bool:
        return not self.is_locked

    def try_acquire(self, task: "Task") -> bool:
        """
        Attempt to acquire the mutex on behalf of `task`.

        Returns:
            True  — mutex was free; task now owns it.
            False — mutex was held; task has been added to the wait queue
                    and should be moved to BLOCKED state by the scheduler.
        """
        if self.is_free:
            self.owner = task
            self.acquisition_count += 1
            return True
        else:
            # Mutex is already held — block this task
            if task not in self.waiting_tasks:
                self.waiting_tasks.append(task)
                self.contention_count += 1
            return False

    def release(self, task: "Task") -> Optional["Task"]:
        """
        Release the mutex from `task`.

        If other tasks are waiting, the highest-priority waiter is granted
        the mutex immediately (priority-ordered unblocking, as in a real RTOS).

        Args:
            task: The task releasing the mutex. Must be the current owner.

        Returns:
            The next task that has been granted the mutex (now READY),
            or None if no tasks were waiting.

        Raises:
            RuntimeError: If `task` is not the current owner (invalid release).
        """
        if self.owner is not task:
            raise RuntimeError(
                f"Task '{task.name}' tried to release mutex '{self.name}' "
                f"but does not own it. Owner is: "
                f"'{self.owner.name if self.owner else 'None'}'"
            )

        if not self.waiting_tasks:
            self.owner = None
            return None

        # Grant to the highest-priority waiting task
        # (sort descending by priority — highest priority wins)
        self.waiting_tasks.sort(key=lambda t: t.priority, reverse=True)
        next_owner = self.waiting_tasks.pop(0)
        self.owner = next_owner
        self.acquisition_count += 1
        return next_owner

    def __repr__(self) -> str:
        owner_name = self.owner.name if self.owner else "free"
        return f"Mutex({self.name!r}, owner={owner_name!r}, waiting={len(self.waiting_tasks)})"
