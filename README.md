# MiniRTOS Simulator

> A Python simulation of a **Priority Preemptive RTOS Scheduler** — the scheduling policy used by ARM Mbed OS, FreeRTOS, and Zephyr. It models task preemption, mutex contention, the **priority inheritance protocol**, and **wait-for-graph deadlock detection**, and renders the result as a Gantt-style terminal chart.

---

## What Is an RTOS Scheduler?

An RTOS manages multiple tasks on a single CPU. It decides:

- **Who runs now?** — the highest-priority ready task
- **What if a more important task becomes ready?** — preempt immediately
- **What if a task needs a shared resource?** — block it on a mutex, run something else
- **What if the task holding that resource is low priority?** — raise its priority until it lets go (priority inheritance)
- **What if two tasks each hold what the other wants?** — detect the circular wait and report it

This simulator implements all five, tick by tick.

---

## Quick Start

```bash
# No dependencies beyond pytest for the test suite
python minirtos.py                       # default: mutex contention
python minirtos.py --scenario basic      # pure priority preemption
python minirtos.py --scenario inversion  # priority inversion
python minirtos.py --scenario deadlock   # circular wait (exits 1)

# The headline comparison — same scenario, protocol off:
python minirtos.py --scenario inversion --no-priority-inheritance

pytest tests/ -v
```

---

## The Result Worth Looking At

`--scenario inversion` is the classic three-task setup. `LowLogger` (priority 1)
holds a bus. `HighControl` (priority 3) needs the same bus and blocks.
`MidCrunch` (priority 2) needs nothing at all — but it outranks `LowLogger`, so
under plain priority scheduling it runs while the *highest*-priority task in
the system waits on a lock held by the *lowest*-priority one.

Same scenario, same arrivals, protocol toggled:

| `HighControl` (priority 3)   | inheritance ON | inheritance OFF |
|:-----------------------------|---------------:|----------------:|
| Ticks blocked on the mutex   | **1**          | **6**           |
| Turnaround time              | **5**          | **10**          |
| Completes at tick            | **9**          | **14**          |

With the protocol off, `HighControl` waits not just for the critical section
but for all of `MidCrunch` — a task it has nothing to do with. That is
*unbounded* priority inversion: the delay is set by unrelated medium-priority
work, so no amount of static analysis of the critical section bounds it. It is
the failure mode that put the Mars Pathfinder lander into a watchdog reset loop
in 1997.

With the protocol on, `LowLogger` is temporarily boosted to priority 3, outruns
`MidCrunch`, reaches its release, and hands the bus over.

```
  t=5   │ MUTEX: HighControl BLOCKED on 'shared_bus' (held by LowLogger)
  t=5   │ PRIORITY INHERITANCE: LowLogger boosted 1 -> 3 (holds 'shared_bus', needed by HighControl)
  t=6   │ MUTEX: LowLogger RELEASED 'shared_bus'
  t=6   │ PRIORITY RESTORED: LowLogger 3 -> 1
  t=6   │ MUTEX: HighControl UNBLOCKED — acquired 'shared_bus'
```

---

## Sample Output

```
  EXECUTION TIMELINE
  ────────────────────────────────────────────────────────────────
  Tick   0 │ LowLogger        ███████  RUNNING   │ pri=1  rem=5
  Tick   1 │ LowLogger        ███████  RUNNING   │ pri=1  rem=4
  Tick   2 │ LowLogger        ███████  RUNNING   │ pri=1  rem=3
  Tick   3 │ MidCrunch        ███████  RUNNING   │ pri=2  rem=5
  Tick   4 │ HighControl      ███████  RUNNING   │ pri=3  rem=3
  Tick   5 │ LowLogger        ███████* RUNNING   │ pri=1->3  rem=2
  Tick   6 │ HighControl      ███████  RUNNING   │ pri=3  rem=2
  Tick   7 │ HighControl      ███████  RUNNING   │ pri=3  rem=1
  Tick   8 │ HighControl      ███████  RUNNING   │ pri=3  rem=0
  ────────────────────────────────────────────────────────────────
  * = running at an inherited (boosted) priority

  FINAL STATISTICS
  ════════════════════════════════════════════════════════════════
  Total ticks simulated  : 16
  CPU utilisation        : 100.0%
  Preemption events      : 3
  Priority inheritances  : 1

  Task             State     CPU   Wait   Blocked   Finish   Turnaround
  ────────────────────────────────────────────────────────────────
  LowLogger        DONE      6     10     0         16       16
  MidCrunch        DONE      6     6      0         14       11
  HighControl      DONE      4     0      1         9        5
```

The deadlock scenario reports the cycle itself and exits with status 1:

```
  t=5   │ DEADLOCK DETECTED — circular wait: TaskAlpha -> TaskBeta -> TaskAlpha
  Circular wait          : TaskAlpha -> TaskBeta -> TaskAlpha
```

---

## How Tasks Declare Mutex Operations

A task's lock calls are scheduled against **its own consumed CPU ticks**, not
against absolute simulation ticks:

```python
Task("UARTTransmit", priority=1, burst=6, arrival_tick=0,
     lock_ops=[LockOp(1, "acquire", "uart_lock"),    # after 1 tick of its own work
               LockOp(4, "release", "uart_lock")])   # after 4 ticks of its own work
```

This matters more than it looks. Real code calls `acquire()` at a point in its
own instruction stream. A task that is preempted, blocked, or not yet created
executes no instructions and therefore cannot touch a lock. Keying lock
operations to absolute ticks — as this simulator originally did — lets a task
acquire a mutex while a *different* task holds the CPU, and makes every
scenario silently dependent on the exact tick numbers hard-coded into it.

The visible consequence: in the `mutex` scenario `UARTTransmit` is preempted at
t=6 while inside its critical section, so `DataProcess` genuinely blocks and
genuinely triggers inheritance. Change a priority or an arrival time and the
lock operations still land at the right point in each task's execution.

---

## Scenarios

| Scenario | What it shows |
|:---|:---|
| `basic` | Pure priority preemption, three tasks, no mutexes |
| `mutex` *(default)* | Preemption into a critical section → contention → blocking → inheritance → handoff |
| `inversion` | Three-task priority inversion; toggle the protocol with `--no-priority-inheritance` |
| `deadlock` | Circular wait between two tasks, detected via the wait-for graph; exits 1 |

---

## Architecture

```
mini-rtos-sim/
├── minirtos.py     ← CLI entry point + scenario definitions
├── scheduler.py    ← Scheduling, priority inheritance, deadlock detection
├── task.py         ← Task state machine + LockOp (execution-relative lock calls)
├── mutex.py        ← Mutex ownership, wait queue, priority-ordered handoff
├── renderer.py     ← Gantt-chart terminal renderer + statistics tables
├── tests/
│   ├── test_task.py       ← state machine, effective priority, lock programme
│   ├── test_mutex.py      ← acquire/release/handoff/ownership tracking
│   └── test_scheduler.py  ← preemption, inheritance, deadlock, timeline fidelity
├── requirements.txt
└── README.md
```

---

## Key Design Decisions

**Deadlock means a cycle, not "everything is stuck".**
Detection walks the wait-for graph — blocked task → owner of the mutex it wants
— and looks for a genuine cycle. Two things follow, and both are tested. A task
blocked on a lock held by a still-running task is *not* reported as a deadlock.
And a real circular wait *is* reported even when unrelated tasks are still
runnable, which a "are all tasks blocked?" heuristic misses entirely.

**Priority inheritance is transitive, and restores correctly.**
When a task blocks, its priority is donated down the chain of mutex owners; if
the owner is itself blocked, the boost passes along. On release the owner does
not simply snap back to its base priority — it recomputes from the mutexes it
*still* holds, so a task holding a second contended lock keeps the boost it
still needs.

**A stall is not a deadlock.**
If a task finishes while still holding a mutex, its waiters can never wake —
but there is no cycle, so calling it a deadlock would be wrong. The scheduler
reports that case separately as `STALLED` and halts, rather than burying it
under a wall of `CPU IDLE` lines until `--ticks` runs out.

**Ties do not cause a context switch.**
When several tasks share the highest effective priority, the incumbent keeps
the CPU. That matches Mbed OS and FreeRTOS with time-slicing disabled;
round-robin among equals would require an explicit quantum, which this
simulator does not model.

**Releasing a mutex reschedules immediately.**
A release hands the lock straight to the highest-priority waiter, which can
make a higher-priority task runnable mid-tick. The scheduler re-selects at that
instant rather than letting the releasing task finish the tick, which is what a
real kernel does on `osMutexRelease`.

**Why Python instead of C?**
The goal is to make the scheduling *decisions* legible. A production scheduler
is C for performance, but the decision logic is the same.

**Why tick-based instead of real time?**
RTOS schedulers make one decision per timer interrupt. Simulating at the tick
level mirrors that exactly.

---

## Corrections Made to an Earlier Version

This project was reviewed and repaired; the defects are recorded here because
the fixes are most of what the code now demonstrates.

| Defect | Fix |
|:---|:---|
| Mutex operations keyed to absolute simulation ticks — a task could acquire a lock while another task held the CPU, and scenarios only "worked" for their hard-coded tick numbers | `LockOp(after_ticks, …)` counts the task's *own* consumed CPU ticks |
| Deadlock detection was "every non-done task is blocked" — missed real cycles whenever an unrelated task was runnable, and flagged non-deadlocks | Wait-for graph cycle detection; the cycle is reported by name |
| README claimed "priority inversion prevention" for what was only priority-ordered wake-up; no inheritance existed and inversion was unbounded | Full priority inheritance protocol, transitive, with correct restore — and a scenario that measures it both ways |
| Timeline stored a shallow copy of the Task taken *after* mutation, so a task that finished on tick N was drawn as `DONE` for the tick it was running | Timeline entries are plain values captured at execution time |
| Equal-priority ties recorded the wrong task as running | Explicit tie rule: incumbent keeps the CPU |
| CPU utilisation counted a deadlocked tick as 100% busy | Utilisation counts only ticks that advanced a task |
| `finish_tick`/`turnaround` rendered `0` as an em-dash (falsy check) | `is not None` |
| Re-acquiring a non-recursive mutex silently queued the owner behind itself | Raises `RuntimeError` |
| A task finishing while holding a contended mutex stranded its waiters silently | End-of-burst releases fire; an unreleased contended mutex is reported, and the run halts as `STALLED` instead of idling out the clock |
| A `burst=0` task was dispatched for one tick before the completion check noticed, reporting `cpu_ticks_used=1` against `burst=0` | `Task` rejects `burst < 1`, negative arrivals, and an `acquire` scheduled at the very end of a burst |
| Zero tests for the scheduler — all 16 tests covered `Task` and `Mutex` only | 86 tests, including regression tests for every row above |

---

## Running Tests

```bash
pytest tests/ -v
```

```
tests/test_mutex.py .............................
tests/test_scheduler.py .....................................
tests/test_task.py ....................

86 passed in 0.34s
```

---

## Tech Stack

| Component | Technology |
|:---|:---|
| Language | Python 3.10+ |
| CLI | `argparse` (stdlib) |
| Data modelling | `dataclasses`, `enum` (stdlib) |
| Terminal output | ANSI colour codes (auto-detected, disabled when piped) |
| Testing | `pytest` |
| Runtime dependencies | None — stdlib only |
