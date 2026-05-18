"""Typer CLI entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from tivtaam.browser.auth import LoginRequiredError, ensure_logged_in
from tivtaam.browser.cart import CartLine, execute_plan
from tivtaam.browser.context import open_context, open_page
from tivtaam.browser.history import sync_history
from tivtaam.browser.search import search_with_cache
from tivtaam.config import load_config
from tivtaam.db.migrations import ensure_db
from tivtaam.resolver.pipeline import Resolution, record_auto_resolution, resolve
from tivtaam.review.actions import review_loop
from tivtaam.util.logging import configure, get_logger


def _ensure_utf8_console() -> None:
    """Force stdout/stderr to UTF-8 on Windows so Hebrew renders correctly."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass


_ensure_utf8_console()

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()
log = get_logger(__name__)


def _bootstrap() -> None:
    cfg = load_config()
    configure(level=cfg.secrets.log_level)


@app.command("history-sync")
def history_sync(resync: bool = typer.Option(False, "--resync", help="Force re-sync even if recent")) -> None:
    """Login (if needed) and scrape orders-history into SQLite."""
    _bootstrap()
    conn = ensure_db()
    with open_context() as ctx:
        page = open_page(ctx)
        try:
            ensure_logged_in(page)
        except LoginRequiredError as e:
            console.print(f"[red]Login required: {e}[/red]")
            raise typer.Exit(code=2) from e
        result = sync_history(conn, page, force=resync)
    console.print(f"[green]history-sync done:[/green] {result}")


@app.command("resolve")
def resolve_cmd(
    generic: str = typer.Argument(..., help="Generic item name in Hebrew, e.g. 'גבינה בולגרית'"),
    learn: bool = typer.Option(False, "--learn", help="Persist confident auto-matches as aliases"),
    use_search: bool = typer.Option(False, "--use-search", help="Open a browser and run site search if history is inconclusive"),
) -> None:
    """Resolve a single generic name to a SKU. With --use-search, falls through to /search."""
    _bootstrap()
    conn = ensure_db()
    if use_search:
        with open_context() as ctx:
            page = open_page(ctx)
            try:
                ensure_logged_in(page)
            except LoginRequiredError as e:
                console.print(f"[red]Login required: {e}[/red]")
                raise typer.Exit(code=2) from e
            res = resolve(conn, generic, page=page)
    else:
        res = resolve(conn, generic)

    table = Table(title=f"Resolution for: {generic}", show_lines=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("sku", res.sku or "-")
    table.add_row("name", res.name or "-")
    table.add_row("confidence", f"{res.confidence:.2f}")
    table.add_row("source", res.source)
    table.add_row("needs_user_confirmation", "yes" if res.needs_user_confirmation else "no")
    table.add_row("reason", res.reason)
    console.print(table)

    if res.alternatives:
        alt = Table(title="Alternatives")
        alt.add_column("#")
        alt.add_column("sku")
        alt.add_column("name")
        alt.add_column("score")
        for i, a in enumerate(res.alternatives, 1):
            alt.add_row(str(i), a.sku, a.name, f"{a.score:.3f}")
        console.print(alt)

    if learn and res.sku and not res.needs_user_confirmation:
        record_auto_resolution(conn, res)
        console.print("[green]learned as alias[/green]")


def _read_items(items: list[str] | None, file: Path | None) -> list[str]:
    out: list[str] = list(items or [])
    if file is not None:
        for line in file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


@app.command("plan")
def plan_cmd(
    items: list[str] = typer.Argument(None, help="Generic items (one per arg), or use --file"),
    file: Path = typer.Option(None, "--file", "-f", help="Read generics from file (one per line, '#' for comments)"),
    dry_run: bool = typer.Option(True, "--dry-run/--live", help="In live mode, actually click add-to-cart (Phase 2: dry-run only is supported)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip interactive review and approve everything"),
) -> None:
    """Resolve a shopping list end-to-end and print what would be added."""
    _bootstrap()
    generics = _read_items(items, file)
    if not generics:
        console.print("[red]No items provided. Pass them as args or use --file.[/red]")
        raise typer.Exit(code=2)

    conn = ensure_db()
    plan: list[Resolution] = []
    with open_context() as ctx:
        page = open_page(ctx)
        try:
            ensure_logged_in(page)
        except LoginRequiredError as e:
            console.print(f"[red]Login required: {e}[/red]")
            raise typer.Exit(code=2) from e
        for g in generics:
            console.print(f"  resolving: {g}")
            plan.append(resolve(conn, g, page=page))

        from tivtaam.basket.forgotten import find_forgotten
        forgotten = find_forgotten(conn, plan)
        if forgotten:
            ft = Table(title=f"Forgotten? — {len(forgotten)} regulars not in this plan")
            ft.add_column("sku", style="cyan"); ft.add_column("name"); ft.add_column("weeks", justify="right")
            for it in forgotten:
                ft.add_row(it.sku, it.name, str(it.weeks_seen))
            console.print(ft)

        approved = review_loop(conn, plan, console, auto_approve=yes)
        if approved is None:
            console.print("[yellow]quit without approving — nothing written[/yellow]")
            raise typer.Exit(code=0)
        if not approved:
            console.print("[yellow]nothing approved[/yellow]")
            raise typer.Exit(code=0)

        # Persist confident auto-resolutions as aliases for next time.
        for r in approved:
            if r.sku and not r.needs_user_confirmation and r.source in ("history", "search", "claude"):
                record_auto_resolution(conn, r)

        lines = [CartLine(sku=r.sku, name=r.name or r.sku) for r in approved if r.sku]
        result = execute_plan(page if not dry_run else None, lines, dry_run=dry_run)

    table = Table(title="Cart write result")
    table.add_column("status")
    table.add_column("skus")
    table.add_row("added" if not result.dry_run else "would add", ", ".join(result.added) or "—")
    if result.failed:
        table.add_row("[red]failed[/red]", ", ".join(result.failed))
    console.print(table)


@app.command("search")
def search_cmd(
    query: str = typer.Argument(..., help="Search query in Hebrew, e.g. 'חלב 3%'"),
    limit: int = typer.Option(10, "--limit", help="Max results to show"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass and refresh the cache"),
) -> None:
    """Run a live site search and print the top results (smoke-test for browser/search.py)."""
    _bootstrap()
    conn = ensure_db()
    if no_cache:
        with conn:
            conn.execute("DELETE FROM search_cache WHERE query = ?", (query,))
    with open_context() as ctx:
        page = open_page(ctx)
        try:
            ensure_logged_in(page)
        except LoginRequiredError as e:
            console.print(f"[red]Login required: {e}[/red]")
            raise typer.Exit(code=2) from e
        results = search_with_cache(conn, page, query, limit=limit)

    table = Table(title=f"Search: {query}")
    table.add_column("#", style="dim")
    table.add_column("sku", style="cyan")
    table.add_column("name")
    table.add_column("brand")
    table.add_column("size")
    table.add_column("price", justify="right")
    for i, r in enumerate(results, 1):
        table.add_row(
            str(i), r.sku, r.name or "-", r.brand or "-",
            r.size_text or "-",
            f"₪{r.price:.2f}" if r.price is not None else "-",
        )
    console.print(table)
    if not results:
        console.print("[yellow]no results[/yellow]")


@app.command("selftest")
def selftest_cmd() -> None:
    """Open the site and verify every selector in data/selectors.yaml resolves."""
    _bootstrap()
    from tivtaam.browser import selectors as sel
    with open_context() as ctx:
        page = open_page(ctx)
        try:
            ensure_logged_in(page)
        except LoginRequiredError as e:
            console.print(f"[red]Login required: {e}[/red]")
            raise typer.Exit(code=2) from e
        cfg = load_config()
        page.goto(f"{cfg.browser.base_url}/orders-history", wait_until="domcontentloaded")
        page.wait_for_timeout(2_000)
        all_keys = sel._load().keys()
        table = Table(title="Selector health")
        table.add_column("key")
        table.add_column("resolved?")
        table.add_column("count", justify="right")
        n_fail = 0
        for key in all_keys:
            loc = sel.resolve(page, key)
            count = loc.count() if loc is not None else 0
            ok = count > 0
            if not ok:
                n_fail += 1
            table.add_row(
                key,
                f"[green]yes[/green]" if ok else f"[red]no[/red]",
                str(count),
            )
        console.print(table)
        if n_fail:
            console.print(f"[red]{n_fail} selectors failed[/red]")
            raise typer.Exit(code=1)


@app.command("typical")
def typical_cmd() -> None:
    """Show the user's typical weekly basket (no browser, DB-only)."""
    _bootstrap()
    from tivtaam.basket.typical import compute_typical
    conn = ensure_db()
    items = compute_typical(conn)
    table = Table(title=f"Typical basket ({len(items)} items)")
    table.add_column("sku", style="cyan")
    table.add_column("name")
    table.add_column("weeks", justify="right")
    table.add_column("qty", justify="right")
    table.add_column("last")
    for it in items:
        table.add_row(
            it.sku, it.name, str(it.weeks_seen),
            f"{it.total_qty:.1f}", (it.last_purchased_at or "")[:10],
        )
    console.print(table)


@app.command("doctor")
def doctor() -> None:
    """Sanity-check config and DB without hitting the network."""
    _bootstrap()
    cfg = load_config()
    conn = ensure_db()
    rows = conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()
    n_products = rows["n"] if rows else 0
    rows = conn.execute("SELECT COUNT(*) AS n FROM purchases").fetchone()
    n_purchases = rows["n"] if rows else 0
    console.print(f"DB products: {n_products}, purchases: {n_purchases}")
    have_user = bool(cfg.secrets.tivtaam_username)
    have_pass = bool(cfg.secrets.tivtaam_password)
    have_anth = bool(cfg.secrets.anthropic_api_key)
    have_tg = bool(cfg.secrets.telegram_bot_token)
    console.print(
        f"Secrets present: tivtaam={have_user and have_pass}, "
        f"anthropic={have_anth}, telegram={have_tg}"
    )


if __name__ == "__main__":
    app()
