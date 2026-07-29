"""
minirtos.py — CLI Entry Point for MiniRTOS Simulator

Usage:
    python minirtos.py                      (run the default embedded scenario)
    python minirtos.py --scenario basic     (3 simple tasks, no mutexes)
    python minirtos.py --scenario mutex     (mutex contention between 2 tasks)
    python minirtos.py --scenario deadlock  (deliberate deadlock demonstration)
    python minirtos.py --ticks 30           (run for 30 ticks instead of default)

The simulator demonstrates Priority Preemptive Scheduling — the scheduling
algorithm used by ARM Mbed OS, FreeRTOS, and Zephyr RTOS.

Exit codes:
    0 — simulation completed normally
    1 — deadlock was detected during simulation
"""

import argparse
import sys
from typing import List, Dict, Tuple

from task import Task
from mutex import Mutex
from scheduler import Scheduler
from renderer import Renderer


# ─────────────────────────────────────────────────────────────────────────────
#  Scenario definitions
#  Each scenario represents a realistic embedded system configuration.
# ─────────────────────────────────────────────────────────────────────────────

def scenario_basic() -> Tuple[List[Task], Dict[str, Mutex], Dict]:
    """
    Three tasks with different priorities and burst times.
    No mutexes — demonstrates pure priority preemption.

    Models a typical embedded system:
      SensorRead   — highest priority (must respond to hardware events fast)
      DataProcess  — medium priority  (processes the sensor data)
      DisplayUpdate— lowest priority  (updates a display at low frequency)
    """
    tasks = [
        Task("SensorRead",    priority=3, burst=5, arrival_tick=0),
        Task("DataProcess",   priority=2, burst=8, arrival_tick=2),
        Task("DisplayUpdate", priority=1, burst=6, arrival_tick=4),
    ]
    return tasks, {}, {}


def scenario_mutex() -> Tuple[List[Task], Dict[str, Mutex], Dict]:
    """
    Two tasks competing for a shared UART peripheral (protected by a mutex).

    Timeline:
      t=0  SensorRead arrives, runs (highest priority)
      t=2  UARTTransmit arrives, acquires uart_lock, starts running
      t=4  DataProcess arrives, tries to acquire uart_lock → BLOCKED
      t=6  UARTTransmit releases uart_lock → DataProcess UNBLOCKED
    """
    tasks = [
        Task("SensorRead",   priority=3, burst=5, arrival_tick=0),
        Task("UARTTransmit", priority=2, burst=6, arrival_tick=2),
        Task("DataProcess",  priority=2, burst=8, arrival_tick=4),
    ]
    mutexes = {
        "uart_lock": Mutex("uart_lock"),
    }
    # (tick) → (task_name, "mutex_name" to acquire OR "mutex_name_release" to release)
    mutex_requests = {
        2: ("UARTTransmit", "uart_lock"),           # t=2: UARTTransmit acquires lock
        4: ("DataProcess",  "uart_lock"),            # t=4: DataProcess tries → BLOCKED
        8: ("UARTTransmit", "uart_lock_release"),    # t=8: UARTTransmit releases → DataProcess unblocks
    }
    return tasks, mutexes, mutex_requests


def scenario_deadlock() -> Tuple[List[Task], Dict[str, Mutex], Dict]:
    """
    Demonstrates a deadlock — two tasks each holding one mutex while
    waiting for the other. The simulator detects and reports this.

    This is a textbook example of the "deadly embrace" problem.
    Real RTOS prevention: enforce a global mutex acquisition order.
    """
    tasks = [
        Task("TaskAlpha", priority=2, burst=10, arrival_tick=0),
        Task("TaskBeta",  priority=2, burst=10, arrival_tick=0),
    ]
    mutexes = {
        "mutex_A": Mutex("mutex_A"),
        "mutex_B": Mutex("mutex_B"),
    }
    mutex_requests = {
        0: ("TaskAlpha", "mutex_A"),    # Alpha acquires A
        1: ("TaskBeta",  "mutex_B"),    # Beta  acquires B
        2: ("TaskAlpha", "mutex_B"),    # Alpha tries B → BLOCKED (Beta holds it)
        3: ("TaskBeta",  "mutex_A"),    # Beta  tries A → BLOCKED (Alpha holds it)
        # Both tasks are now BLOCKED → deadlock
    }
    return tasks, mutexes, mutex_requests


SCENARIOS = {
    "basic"    : scenario_basic,
    "mutex"    : scenario_mutex,
    "deadlock" : scenario_deadlock,
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
            "  basic    — 3 tasks, no mutexes (pure preemption demo)\n"
            "  mutex    — 2 tasks share a mutex (contention + blocking demo)\n"
            "  deadlock — deliberate deadlock to demonstrate detection\n\n"
            "Examples:\n"
            "  python minirtos.py\n"
            "  python minirtos.py --scenario mutex\n"
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
    return ap


def main() -> int:
    # Reconfigure stdout to UTF-8 so box-drawing characters render
    # correctly on Windows terminals (which default to cp1252).
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    args = build_cli().parse_args()

    # Load the scenario
    tasks, mutexes, mutex_requests = SCENARIOS[args.scenario]()

    print(f"\n  Scenario : {args.scenario.upper()}")
    print(f"  Tasks    : {', '.join(t.name for t in tasks)}")
    print(f"  Max ticks: {args.ticks}")

    # Run the simulation
    sched    = Scheduler(tasks, mutexes, mutex_requests)
    timeline = sched.run(max_ticks=args.ticks)
    stats    = sched.get_statistics()

    # Render results
    renderer = Renderer([t.name for t in tasks])
    renderer.render(timeline, stats)

    # Exit code 1 if deadlock was detected
    return 1 if stats["deadlock_tick"] is not None else 0


if __name__ == "__main__":
    sys.exit(main())
