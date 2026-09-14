"""
renderer.py — Timeline Visualiser for MiniRTOS Simulator

Takes the raw timeline output from the Scheduler and renders it as a
readable, colour-coded execution chart in the terminal — similar to a Gantt
chart used in RTOS documentation.

Also renders the final statistics table showing per-task CPU usage, wait
time, blocked time, turnaround time, and mutex contention data.

The timeline entries are plain dicts of values captured at the moment each
tick ran, so what is drawn here is what the scheduler actually did — the
renderer never re-reads mutable task state after the fact.
"""

import sys
from typing import List, Dict

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
        self._render_header(stats)
        self._render_timeline(timeline)
        self._render_events(timeline)
        self._render_statistics(stats)
        print()

    # ── Private renderers ─────────────────────────────────────────────────

    def _render_header(self, stats: Dict) -> None:
        print(BOLD("  MINIRTOS SIMULATOR — Priority Preemptive Scheduler"))
        pi = ("priority inheritance: ON" if stats["priority_inheritance_enabled"]
              else "priority inheritance: OFF")
        print("  " + DIM(pi))
        print("  " + "═" * 64)
        print()

    def _render_timeline(self, timeline: List[Dict]) -> None:
        """
        Render a Gantt-style chart:

        Tick  0 │ SensorRead    ████ RUNNING   │ pri=3  rem=4
        Tick  2 │ CPU IDLE      ░░░░           │
        """
        print(BOLD("  EXECUTION TIMELINE"))
        print("  " + "─" * 64)

        for entry in timeline:
            tick = entry["tick"]

            if entry.get("deadlock"):
                print(f"  Tick {tick:>3} │ {RED('DEADLOCK — simulation halted')}")
                break

            name = entry.get("running")
            if name is None:
                print(f"  Tick {tick:>3} │ {DIM('CPU IDLE'):<28} {DIM('░░░░░░░')}")
                continue

            colour = self._colour_map.get(name, lambda x: x)
            base, eff = entry["priority"], entry["effective_priority"]
            pri = f"pri={base}" if eff == base else f"pri={base}->{eff}"
            marker = "*" if eff != base else " "
            details = DIM(f"{pri}  rem={entry['remaining']}")
            label = colour(f"{name:<16}")
            print(f"  Tick {tick:>3} │ {label} {colour('███████')}{marker}"
                  f" {'RUNNING':<9} │ {details}")

        print("  " + "─" * 64)
        print(DIM("  * = running at an inherited (boosted) priority"))
        print()

    def _render_events(self, timeline: List[Dict]) -> None:
        """Print a log of all notable events."""
        notable = [
            (e["tick"], ev)
            for e in timeline
            for ev in e.get("events", [])
            if ev and ev != "CPU IDLE"
        ]
        if not notable:
            return

        print(BOLD("  EVENT LOG"))
        print("  " + "─" * 64)
        for tick, ev in notable:
            prefix = f"  t={tick:<3} │ "
            if "DEADLOCK" in ev:
                print(prefix + RED(BOLD(ev)))
            elif "INHERITANCE" in ev:
                print(prefix + MAGENTA(ev))
            elif "PRIORITY RESTORED" in ev:
                print(prefix + DIM(ev))
            elif "PREEMPT" in ev:
                print(prefix + YELLOW(ev))
            elif "BLOCKED" in ev or "WARNING" in ev or "ERROR" in ev:
                print(prefix + RED(ev))
            elif "UNBLOCKED" in ev or "ACQUIRED" in ev or "RELEASED" in ev:
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
        print("  " + "═" * 64)

        print(f"  Total ticks simulated  : {BOLD(str(stats['total_ticks']))}")
        print(f"  Ticks executing work   : {stats['executing_ticks']}")
        print(f"  CPU utilisation        : {BOLD(str(stats['cpu_utilisation']) + '%')}")
        print(f"  Idle ticks             : {stats['idle_ticks']}")
        print(f"  Preemption events      : {YELLOW(str(stats['preemption_count']))}")
        print(f"  Priority inheritances  : {MAGENTA(str(stats['inheritance_events']))}")
        if stats["deadlock_tick"] is not None:
            print(f"  Deadlock detected at   : {RED('tick ' + str(stats['deadlock_tick']))}")
            cycle = stats["deadlock_cycle"] or []
            print(f"  Circular wait          : {RED(' -> '.join(cycle + cycle[:1]))}")
        print()

        # Per-task table
        header = (f"  {'Task':<16} {'State':<9} {'CPU':<5} {'Wait':<6} "
                  f"{'Blocked':<9} {'Finish':<8} {'Turnaround'}")
        print(header)
        print("  " + "─" * 64)
        for t in stats["tasks"]:
            colour = self._colour_map.get(t["name"], lambda x: x)
            # `is not None`, not truthiness: a task can legitimately finish at
            # tick 0, and "0" must not render as an em-dash.
            finish = str(t["finish_tick"]) if t["finish_tick"] is not None else "—"
            ta = str(t["turnaround_time"]) if t["turnaround_time"] is not None else "—"
            name = colour("{:<16}".format(t["name"]))
            print(f"  {name} {t['state']:<9} {t['cpu_ticks_used']:<5} "
                  f"{t['wait_time']:<6} {t['blocked_time']:<9} {finish:<8} {ta}")

        # Mutex contention table
        if stats["mutex_stats"]:
            print()
            print(f"  {'Mutex':<18} {'Acquisitions':<14} {'Contention':<12} {'Held by'}")
            print("  " + "─" * 64)
            for m in stats["mutex_stats"]:
                contention = (YELLOW(str(m["contention_count"]))
                              if m["contention_count"] else "0")
                owner = m["owner"] or "free"
                print(f"  {m['name']:<18} {m['acquisition_count']:<14} "
                      f"{contention:<12} {owner}")

        print("  " + "═" * 64)
