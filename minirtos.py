"""
minirtos.py — CLI Entry Point for MiniRTOS Simulator

Usage:
    python minirtos.py                        (default: mutex contention)
    python minirtos.py --scenario basic       (3 tasks, no mutexes)
    python minirtos.py --scenario mutex       (mutex contention between 2 tasks)
    python minirtos.py --scenario inversion   (priority inversion)
    python minirtos.py --scenario deadlock    (circular wait demonstration)
    python minirtos.py --scenario inversion --no-priority-inheritance
    python minirtos.py --ticks 30             (run for 30 ticks)

The simulator demonstrates Priority Preemptive Scheduling — the scheduling
algorithm used by ARM Mbed OS, FreeRTOS, and Zephyr RTOS.

Mutex operations are declared per task with `LockOp(after_ticks, action,
mutex)`, where `after_ticks` counts that task's *own* consumed CPU ticks.
This is what a real task does: it calls acquire() at a point in its own
instruction stream, which slides in wall-clock time whenever the task is
preempted or blocked.

Exit codes:
    0 — simulation completed normally
    1 — deadlock was detected during simulation
"""

import argparse
import sys
from typing import List, Dict, Tuple

from task import Task, LockOp
from mutex import Mutex
from scheduler import Scheduler
from renderer import Renderer


Scenario = Tuple[List[Task], Dict[str, Mutex]]


# ─────────────────────────────────────────────────────────────────────────────
#  Scenario definitions
#  Each scenario represents a realistic embedded system configuration.
# ─────────────────────────────────────────────────────────────────────────────

def scenario_basic() -> Scenario:
    """
    Three tasks with different priorities and burst times.
    No mutexes — demonstrates pure priority preemption.

    Models a typical embedded system:
      SensorRead    — highest priority (must respond to hardware events fast)
      DataProcess   — medium priority  (processes the sensor data)
      DisplayUpdate — lowest priority  (updates a display at low frequency)

    Expected: SensorRead runs to completion first; DataProcess arrives at
    t=2 but waits; DisplayUpdate arrives at t=4 and runs last.
    """
    tasks = [
        Task("SensorRead",    priority=3, burst=5, arrival_tick=0),
        Task("DataProcess",   priority=2, burst=8, arrival_tick=2),
        Task("DisplayUpdate", priority=1, burst=6, arrival_tick=4),
    ]
    return tasks, {}


def scenario_mutex() -> Scenario:
    """
    Two tasks competing for a shared UART peripheral behind a mutex.

    UARTTransmit takes uart_lock one tick into its own execution and holds it
    for three more of its ticks. DataProcess arrives later at a higher
    priority, preempts UARTTransmit, reaches for the same lock one tick into
    its own execution, finds it held, and blocks until the handoff.

    Note that no absolute tick numbers appear here. Contention arises from
    the scheduling itself: DataProcess outranks UARTTransmit, so it preempts
    into the middle of the critical section. Change any priority or arrival
    time and the lock operations still happen at the right point in each
    task's own execution.
    """
    tasks = [
        Task("SensorRead",   priority=3, burst=3, arrival_tick=0),
        Task("UARTTransmit", priority=1, burst=6, arrival_tick=0,
             lock_ops=[LockOp(1, "acquire", "uart_lock"),
                       LockOp(4, "release", "uart_lock")]),
        Task("DataProcess",  priority=2, burst=4, arrival_tick=6,
             lock_ops=[LockOp(1, "acquire", "uart_lock"),
                       LockOp(3, "release", "uart_lock")]),
    ]
    return tasks, {"uart_lock": Mutex("uart_lock")}


def scenario_inversion() -> Scenario:
    """
    The classic three-task priority inversion.

    LowLogger (pri 1) takes the shared bus. HighControl (pri 3) then needs
    the same bus and blocks. MidCrunch (pri 2) needs nothing at all — but it
    outranks LowLogger, so under plain priority scheduling it runs while the
    highest-priority task in the system sits waiting on a lock held by the
    lowest-priority one. The delay is bounded only by how long the medium
    task feels like running, which is why it is called *unbounded* priority
    inversion.

    Priority inheritance fixes it: LowLogger temporarily runs at HighControl's
    priority, so it outruns MidCrunch, reaches its release, and hands the bus
    over.

    Run it both ways to see the difference:
        python minirtos.py --scenario inversion
        python minirtos.py --scenario inversion --no-priority-inheritance

    This is the failure mode that put the Mars Pathfinder lander into a
    watchdog reset loop in 1997.
    """
    tasks = [
        Task("LowLogger",   priority=1, burst=6, arrival_tick=0,
             lock_ops=[LockOp(1, "acquire", "shared_bus"),
                       LockOp(4, "release", "shared_bus")]),
        Task("MidCrunch",   priority=2, burst=6, arrival_tick=3),
        Task("HighControl", priority=3, burst=4, arrival_tick=4,
             lock_ops=[LockOp(1, "acquire", "shared_bus"),
                       LockOp(3, "release", "shared_bus")]),
    ]
    return tasks, {"shared_bus": Mutex("shared_bus")}


def scenario_deadlock() -> Scenario:
    """
    A genuine circular wait — the "deadly embrace".

    TaskAlpha takes mutex_A then wants mutex_B; TaskBeta takes mutex_B then
    wants mutex_A. The interleaving that makes this happen is produced by the
    scheduler itself: TaskBeta arrives later but at a higher priority, so it
    preempts TaskAlpha in between Alpha's two acquisitions.

    Watch the event log: priority inheritance fires first (Alpha is boosted
    because Beta is waiting on a lock Alpha holds), and only when Alpha then
    reaches for mutex_B does the cycle close. Inheritance bounds inversion;
    it does nothing about lock-ordering bugs. The fix for this one is a
    global acquisition order.
    """
    tasks = [
        Task("TaskAlpha", priority=1, burst=10, arrival_tick=0,
             lock_ops=[LockOp(1, "acquire", "mutex_A"),
                       LockOp(3, "acquire", "mutex_B"),
                       LockOp(8, "release", "mutex_B"),
                       LockOp(9, "release", "mutex_A")]),
        Task("TaskBeta",  priority=2, burst=10, arrival_tick=2,
             lock_ops=[LockOp(1, "acquire", "mutex_B"),
                       LockOp(2, "acquire", "mutex_A"),
                       LockOp(8, "release", "mutex_A"),
                       LockOp(9, "release", "mutex_B")]),
    ]
    mutexes = {"mutex_A": Mutex("mutex_A"), "mutex_B": Mutex("mutex_B")}
    return tasks, mutexes


SCENARIOS = {
    "basic"     : scenario_basic,
    "mutex"     : scenario_mutex,
    "inversion" : scenario_inversion,
    "deadlock"  : scenario_deadlock,
}


# ─────────────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────────────

def build_cli() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="minirtos",
        description=(
            "MiniRTOS Simulator — Demonstrates Priority Preemptive Scheduling\n"
            "as used in ARM Mbed OS, FreeRTOS, and Zephyr RTOS."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Scenarios:\n"
            "  basic     — 3 tasks, no mutexes (pure preemption demo)\n"
            "  mutex     — 2 tasks share a mutex (contention + blocking)\n"
            "  inversion — priority inversion, with and without inheritance\n"
            "  deadlock  — circular wait, detected via the wait-for graph\n\n"
            "Examples:\n"
            "  python minirtos.py\n"
            "  python minirtos.py --scenario inversion\n"
            "  python minirtos.py --scenario inversion --no-priority-inheritance\n"
            "  python minirtos.py --scenario deadlock --ticks 15\n"
        ),
    )
    ap.add_argument(
        "--scenario", "-s",
        choices=list(SCENARIOS.keys()),
        default="mutex",
        help="Which scenario to simulate (default: mutex)",
    )
    ap.add_argument(
        "--ticks", "-t",
        type=int,
        default=25,
        metavar="N",
        help="Maximum number of clock ticks to simulate (default: 25)",
    )
    ap.add_argument(
        "--no-priority-inheritance",
        action="store_true",
        help="Disable the priority-inheritance protocol, to show the "
             "unbounded inversion it prevents",
    )
    return ap


def main(argv=None) -> int:
    # Reconfigure stdout to UTF-8 so box-drawing characters render correctly
    # on Windows terminals (which default to cp1252).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = build_cli().parse_args(argv)

    tasks, mutexes = SCENARIOS[args.scenario]()

    print(f"\n  Scenario : {args.scenario.upper()}")
    print(f"  Tasks    : {', '.join(t.name for t in tasks)}")
    print(f"  Max ticks: {args.ticks}")

    sched = Scheduler(
        tasks, mutexes,
        enable_priority_inheritance=not args.no_priority_inheritance,
    )
    timeline = sched.run(max_ticks=args.ticks)
    stats = sched.get_statistics()

    Renderer([t.name for t in tasks]).render(timeline, stats)

    return 1 if stats["deadlock_tick"] is not None else 0


if __name__ == "__main__":
    sys.exit(main())
