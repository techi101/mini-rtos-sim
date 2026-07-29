"""
renderer.py — Timeline Visualiser for MiniRTOS Simulator

Takes the raw timeline output from the Scheduler and renders it
as a readable, colour-coded execution chart in the terminal —
similar to a Gantt chart used in RTOS documentation.

Also renders the final statistics table showing per-task CPU usage,
wait time, turnaround time, and mutex contention data.
"""

import os
import sys
from typing import List, Dict, Optional

# ── Colour helpers ────────────────────────────────────────────────────────────
_TTY = sys.stdout.isatty()

def _c(text, code):
    return f"\033[{code}m{text}\033[0m" if _TTY else text

BOLD   = lambda t: _c(t, "1")
GREEN  = lambda t: _c(t, "32")
RED    = lambda t: _c(t, "31")
YELLOW = lambda t: _c(t, "33")
CYAN   = lambda t: _c(t, "36")
MAGENTA= lambda t: _c(t, "35")
DIM    = lambda t: _c(t, "2")
BG_GRN = lambda t: _c(t, "42;30")
BG_RED = lambda t: _c(t, "41;37")
BG_YEL = lambda t: _c(t, "43;30")


# Assign a colour to each task by its index in the task list
_TASK_COLOURS = [GREEN, CYAN, YELLOW, MAGENTA, RED]


class Renderer:
    """Renders the simulation timeline and statistics to stdout."""

    def __init__(self, task_names: List[str]):
        self.task_names = task_names
        self._colour_map = {
            name: _TASK_COLOURS[i % len(_TASK_COLOURS)]
            for i, name in enumerate(task_names)
        }

    def render(self, timeline: List[Dict], stats: Dict) -> None:
        print()
        self._render_header()
        self._render_timeline(timeline)
        self._render_events(timeline)
        self._render_statistics(stats)
        print()

    # ── Private renderers ─────────────────────────────────────────────────

    def _render_header(self) -> None:
        print(BOLD("  MINIRTOS SIMULATOR — Priority Preemptive Scheduler"))
        print("  " + "═" * 60)
        print()

    def _render_timeline(self, timeline: List[Dict]) -> None:
        """
        Render a Gantt-style chart:

        Tick  0 │ SensorRead    ████ RUNNING   │ pri=3  rem=4
        Tick  1 │ SensorRead    ████ RUNNING   │ pri=3  rem=3
        Tick  2 │ CPU IDLE      ░░░░           │
        """
        print(BOLD("  EXECUTION TIMELINE"))
        print("  " + "─" * 60)

        for event in timeline:
            tick    = event["tick"]
            running = event.get("running_task")
            is_dead = event.get("deadlock", False)

            if is_dead:
                print(f"  Tick {tick:>3} │ {RED('⚠️  DEADLOCK — simulation halted')}")
                break

            if running is None:
                bar = DIM("░░░░░░░")
                print(f"  Tick {tick:>3} │ {DIM('CPU IDLE'):<28} {bar}")
            else:
                colour  = self._colour_map.get(running.name, lambda x: x)
                bar     = colour("███████")
                state   = running.state.name
                name    = colour(f"{running.name:<16}")
                details = DIM(f"pri={running.priority}  rem={running.remaining_burst}")
                print(f"  Tick {tick:>3} │ {name} {bar} {state:<10} │ {details}")

        print("  " + "─" * 60)
        print()

    def _render_events(self, timeline: List[Dict]) -> None:
        """Print a log of all notable events (arrivals, preemptions, mutex ops, completions)."""
        notable = [
            (e["tick"], ev)
            for e in timeline
            for ev in e.get("events", [])
            if ev and ev != "CPU IDLE"
        ]
        if not notable:
            return

        print(BOLD("  EVENT LOG"))
        print("  " + "─" * 60)
        for tick, ev in notable:
            prefix = f"  t={tick:<3} │ "
            if "PREEMPT" in ev:
                print(prefix + YELLOW(ev))
            elif "DEADLOCK" in ev:
                print(prefix + RED(ev))
            elif "BLOCKED" in ev:
                print(prefix + RED(ev))
            elif "UNBLOCKED" in ev or "ACQUIRED" in ev:
                print(prefix + GREEN(ev))
            elif "DONE" in ev:
                print(prefix + CYAN(ev))
            elif "ARRIVED" in ev:
                print(prefix + DIM(ev))
            else:
                print(prefix + ev)
        print()

    def _render_statistics(self, stats: Dict) -> None:
        """Render the final summary statistics table."""
        print(BOLD("  FINAL STATISTICS"))
        print("  " + "═" * 60)

        total = stats["total_ticks"]
        print(f"  Total ticks simulated  : {BOLD(str(total))}")
        print(f"  CPU utilisation        : {BOLD(str(stats['cpu_utilisation']) + '%')}")
        print(f"  Idle ticks             : {stats['idle_ticks']}")
        print(f"  Preemption events      : {YELLOW(str(stats['preemption_count']))}")
        if stats["deadlock_tick"] is not None:
            dtick = stats["deadlock_tick"]
            print(f"  Deadlock detected at   : {RED(f'tick {dtick}')}")
        print()

        # Per-task table
        print(f"  {'Task':<18} {'State':<10} {'CPU Ticks':<12} {'Wait':<8} {'Finish':<8} {'Turnaround'}")
        print("  " + "─" * 60)
        for t in stats["tasks"]:
            colour = self._colour_map.get(t["name"], lambda x: x)
            name    = colour(f"{t['name']:<18}")
            state   = t["state"]
            cpu     = str(t["cpu_ticks_used"])
            wait    = str(t["wait_time"])
            finish  = str(t["finish_tick"]) if t["finish_tick"] else "—"
            ta      = str(t["turnaround_time"]) if t["turnaround_time"] else "—"
            print(f"  {name} {state:<10} {cpu:<12} {wait:<8} {finish:<8} {ta}")

        # Mutex contention table
        if stats["mutex_stats"]:
            print()
            print(f"  {'Mutex':<20} {'Acquisitions':<15} {'Contention Events'}")
            print("  " + "─" * 50)
            for m in stats["mutex_stats"]:
                print(
                    f"  {m['name']:<20} {m['acquisition_count']:<15} "
                    f"{YELLOW(str(m['contention_count'])) if m['contention_count'] else '0'}"
                )

        print("  " + "═" * 60)
