"""Interactive review actions: approve / swap / remove / quit.

The plan is a list of Resolution. Each interactive action mutates the list in
place (swap replaces an entry, remove nulls it out). Swap also persists the
choice as a user_confirmed alias so the next run hits step A directly.
"""

from __future__ import annotations

import sqlite3

from rich.console import Console
from rich.prompt import Prompt

from tivtaam.resolver.pipeline import (
    Resolution,
    confirm_user_choice,
)
from tivtaam.review.cli_view import render_alternatives, render_plan


def _swap(conn: sqlite3.Connection, plan: list[Resolution], console: Console) -> None:
    raw = Prompt.ask("Item # to swap")
    try:
        idx = int(raw) - 1
    except ValueError:
        console.print("[yellow]not a number[/yellow]")
        return
    if not (0 <= idx < len(plan)):
        console.print("[yellow]out of range[/yellow]")
        return
    r = plan[idx]
    if not r.alternatives:
        console.print("[yellow]no alternatives recorded for this item[/yellow]")
        return
    console.print(render_alternatives(r))
    raw_pick = Prompt.ask("Pick alt #")
    try:
        pick = int(raw_pick) - 1
    except ValueError:
        console.print("[yellow]not a number[/yellow]")
        return
    if not (0 <= pick < len(r.alternatives)):
        console.print("[yellow]out of range[/yellow]")
        return
    alt = r.alternatives[pick]
    plan[idx] = Resolution(
        generic=r.generic,
        sku=alt.sku,
        name=alt.name,
        confidence=1.0,
        source="alias",
        reason="user-selected from alternatives",
        needs_user_confirmation=False,
    )
    confirm_user_choice(conn, r.generic, alt.sku)
    console.print(f"[green]swapped to {alt.sku} {alt.name}[/green]")


def _remove(plan: list[Resolution], console: Console) -> None:
    raw = Prompt.ask("Item # to remove")
    try:
        idx = int(raw) - 1
    except ValueError:
        console.print("[yellow]not a number[/yellow]")
        return
    if not (0 <= idx < len(plan)):
        console.print("[yellow]out of range[/yellow]")
        return
    r = plan[idx]
    plan[idx] = Resolution(
        generic=r.generic,
        sku=None,
        name=None,
        confidence=0.0,
        source="ask_user",
        reason="removed by user",
        needs_user_confirmation=False,
    )
    console.print(f"[yellow]removed {r.generic}[/yellow]")


def review_loop(
    conn: sqlite3.Connection,
    plan: list[Resolution],
    console: Console,
    auto_approve: bool = False,
) -> list[Resolution] | None:
    """Interactive loop. Returns the approved plan (skipping removed items) or
    None if the user quits without approving."""
    if auto_approve:
        return [r for r in plan if r.sku]

    while True:
        console.print()
        console.print(render_plan(plan))
        action = Prompt.ask(
            "[bold]Action[/bold]  [a]pprove all  [s]wap  [r]emove  [q]uit",
            choices=["a", "s", "r", "q"],
            default="a",
        )
        if action == "a":
            return [r for r in plan if r.sku]
        if action == "s":
            _swap(conn, plan, console)
        elif action == "r":
            _remove(plan, console)
        elif action == "q":
            return None
