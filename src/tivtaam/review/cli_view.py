"""Render a resolution plan as a Rich table for CLI review."""

from __future__ import annotations

from rich.table import Table

from tivtaam.resolver.pipeline import Resolution


def _status_for(r: Resolution) -> tuple[str, str]:
    if r.sku is None:
        return "FAILED", "red"
    if r.needs_user_confirmation:
        return "ASK", "yellow"
    if r.source == "alias":
        return "ALIAS", "green"
    return "OK", "green"


def render_plan(plan: list[Resolution]) -> Table:
    table = Table(title="Shopping plan", show_lines=False)
    table.add_column("#", style="dim", width=3)
    table.add_column("Generic")
    table.add_column("→ Resolved as")
    table.add_column("SKU", style="cyan")
    table.add_column("Conf", justify="right")
    table.add_column("Source")
    table.add_column("Status")
    for i, r in enumerate(plan, 1):
        status, style = _status_for(r)
        table.add_row(
            str(i),
            r.generic,
            r.name or "—",
            r.sku or "—",
            f"{r.confidence:.2f}",
            r.source,
            f"[{style}]{status}[/{style}]",
        )
    return table


def render_alternatives(r: Resolution) -> Table:
    table = Table(title=f"Alternatives for: {r.generic}")
    table.add_column("#", style="dim", width=3)
    table.add_column("SKU", style="cyan")
    table.add_column("Name")
    table.add_column("Score", justify="right")
    for i, a in enumerate(r.alternatives, 1):
        table.add_row(str(i), a.sku, a.name, f"{a.score:.3f}")
    return table
