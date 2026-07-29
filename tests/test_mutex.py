"""
tests/test_mutex.py — pytest tests for the Mutex class

Verifies acquire/release semantics, priority-ordered unblocking,
and error handling for invalid releases.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from mutex import Mutex
from task import Task, TaskState


@pytest.fixture
def mutex():
    return Mutex("test_lock")

@pytest.fixture
def task_a():
    return Task("Alpha", priority=3, burst=5)

@pytest.fixture
def task_b():
    return Task("Beta", priority=2, burst=5)

@pytest.fixture
def task_c():
    return Task("Gamma", priority=1, burst=5)


class TestMutexAcquire:
    def test_free_mutex_acquired_successfully(self, mutex, task_a):
        result = mutex.try_acquire(task_a)
        assert result is True
        assert mutex.owner == task_a
        assert mutex.is_locked

    def test_locked_mutex_returns_false(self, mutex, task_a, task_b):
        mutex.try_acquire(task_a)
        result = mutex.try_acquire(task_b)
        assert result is False
        assert mutex.owner == task_a  # owner unchanged

    def test_blocked_task_added_to_wait_queue(self, mutex, task_a, task_b):
        mutex.try_acquire(task_a)
        mutex.try_acquire(task_b)
        assert task_b in mutex.waiting_tasks

    def test_contention_count_increments(self, mutex, task_a, task_b):
        mutex.try_acquire(task_a)
        mutex.try_acquire(task_b)
        assert mutex.contention_count == 1

    def test_acquisition_count_increments(self, mutex, task_a):
        mutex.try_acquire(task_a)
        assert mutex.acquisition_count == 1

    def test_duplicate_wait_not_added_twice(self, mutex, task_a, task_b):
        mutex.try_acquire(task_a)
        mutex.try_acquire(task_b)
        mutex.try_acquire(task_b)  # second attempt — should not double-add
        assert mutex.waiting_tasks.count(task_b) == 1


class TestMutexRelease:
    def test_release_frees_mutex_when_no_waiters(self, mutex, task_a):
        mutex.try_acquire(task_a)
        next_owner = mutex.release(task_a)
        assert next_owner is None
        assert mutex.is_free

    def test_release_grants_to_waiting_task(self, mutex, task_a, task_b):
        mutex.try_acquire(task_a)
        mutex.try_acquire(task_b)   # task_b is now waiting
        next_owner = mutex.release(task_a)
        assert next_owner == task_b
        assert mutex.owner == task_b

    def test_release_grants_highest_priority_waiter(self, mutex, task_a, task_b, task_c):
        """Priority-ordered release: highest priority waiter gets the mutex first."""
        mutex.try_acquire(task_a)
        mutex.try_acquire(task_c)   # pri=1, waiting
        mutex.try_acquire(task_b)   # pri=2, waiting — higher priority than task_c
        next_owner = mutex.release(task_a)
        assert next_owner == task_b   # task_b (pri=2) wins over task_c (pri=1)

    def test_invalid_release_raises_error(self, mutex, task_a, task_b):
        mutex.try_acquire(task_a)
        with pytest.raises(RuntimeError):
            mutex.release(task_b)   # task_b doesn't own the mutex


class TestMutexProperties:
    def test_is_free_initially(self, mutex):
        assert mutex.is_free

    def test_is_locked_after_acquire(self, mutex, task_a):
        mutex.try_acquire(task_a)
        assert mutex.is_locked

    def test_is_free_after_release(self, mutex, task_a):
        mutex.try_acquire(task_a)
        mutex.release(task_a)
        assert mutex.is_free
