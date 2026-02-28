"""
LeadGen CLI
Command-line interface for running LeadGen / Legion.

Usage:
    leadgen search         # Fetch new leads from configured sources
    leadgen score          # AI-score unscored leads
    leadgen review         # Interactively review and approve outreach drafts
    leadgen send           # Send approved outreach
    leadgen pipeline       # Show pipeline summary
    leadgen mcp            # Start the MCP server
"""

import asyncio
import logging

import click
from rich.console import Console
from rich.table import Table

console = Console()


@click.group()
@click.option("--config", default=None, help="Path to config YAML file")
@click.option("--debug", is_flag=True, help="Enable debug logging")
@click.pass_context
def main(ctx, config, debug):
    """LeadGen (Legion) — AI-powered lead generation engine."""
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


@main.command()
@click.option("--limit", default=50, help="Number of leads to fetch")
@click.pass_context
def search(ctx, limit):
    """Fetch new leads from Apollo.io matching your ICP."""
    async def _run():
        from leadgen.config.loader import load_config, load_api_keys
        from leadgen.sources.apollo import ApolloConnector
        from leadgen.crm.database import LeadDatabase

        cfg = load_config(ctx.obj.get("config_path"))
        keys = load_api_keys()
        db = LeadDatabase(cfg.database.sqlite_path)
        await db.init()

        with console.status(f"Fetching {limit} leads from Apollo..."):
            async with ApolloConnector(cfg, keys) as apollo:
                leads = await apollo.search(limit=limit)

        added = 0
        for lead in leads:
            is_new = await db.upsert(lead)
            if is_new:
                added += 1

        console.print(f"[green]✓[/green] Fetched {len(leads)} leads, {added} new added to database.")

    asyncio.run(_run())


@main.command()
@click.option("--limit", default=20, help="Max leads to score")
@click.pass_context
def score(ctx, limit):
    """AI-score unscored leads against your ICP."""
    async def _run():
        from leadgen.config.loader import load_config, load_api_keys
        from leadgen.ai.scorer import LeadScorer
        from leadgen.crm.database import LeadDatabase
        from leadgen.models import LeadStatus

        cfg = load_config(ctx.obj.get("config_path"))
        keys = load_api_keys()
        db = LeadDatabase(cfg.database.sqlite_path)
        await db.init()

        unscored = await db.list(status=LeadStatus.NEW, limit=limit)
        if not unscored:
            console.print("[yellow]No unscored leads found.[/yellow]")
            return

        scorer = LeadScorer(cfg, keys)
        with console.status(f"Scoring {len(unscored)} leads..."):
            scored = await scorer.score_batch(unscored)

        for lead in unscored:
            lead.status = LeadStatus.SCORED
            await db.upsert(lead)

        console.print(
            f"[green]✓[/green] Scored {len(unscored)} leads. "
            f"{len(scored)} passed threshold {cfg.scoring.threshold}."
        )

    asyncio.run(_run())


@main.command()
@click.pass_context
def pipeline(ctx):
    """Show a summary of the current lead pipeline."""
    async def _run():
        from leadgen.config.loader import load_config
        from leadgen.crm.database import LeadDatabase

        cfg = load_config(ctx.obj.get("config_path"))
        db = LeadDatabase(cfg.database.sqlite_path)
        await db.init()

        counts = await db.count_by_status()

        table = Table(title="LeadGen Pipeline", show_header=True)
        table.add_column("Status", style="cyan")
        table.add_column("Count", justify="right", style="white")

        total = 0
        for status, count in sorted(counts.items()):
            table.add_row(status, str(count))
            total += count
        table.add_row("─" * 20, "─" * 6)
        table.add_row("[bold]TOTAL[/bold]", f"[bold]{total}[/bold]")

        console.print(table)

    asyncio.run(_run())


@main.command()
@click.pass_context
def smtp_test(ctx):
    """Test SMTP connection without sending any email."""
    async def _run():
        from leadgen.config.loader import load_config, load_api_keys

        cfg = load_config(ctx.obj.get("config_path"))
        keys = load_api_keys()

        if not keys.smtp_host or not keys.smtp_username or not keys.smtp_password:
            console.print("[red]✗[/red] SMTP not configured. Add SMTP_HOST, SMTP_USERNAME, and SMTP_PASSWORD to .env")
            console.print("See docs/SMTP_SETUP.md for Gmail setup instructions.")
            return

        try:
            import aiosmtplib

            console.print(f"Connecting to {keys.smtp_host}:{keys.smtp_port}...")
            smtp = aiosmtplib.SMTP(
                hostname=keys.smtp_host,
                port=keys.smtp_port,
                use_tls=False,
                start_tls=False,
            )
            await smtp.connect()
            await smtp.starttls()
            await smtp.login(keys.smtp_username, keys.smtp_password)
            await smtp.quit()

            console.print("[green]✓[/green] SMTP connection successful.")
            console.print(f"  Host: {keys.smtp_host}:{keys.smtp_port}")
            console.print(f"  From: {keys.smtp_from_name or 'N/A'} <{keys.smtp_from_email or keys.smtp_username}>")
        except Exception as e:
            console.print(f"[red]✗[/red] SMTP connection failed: {e}")
            console.print("See docs/SMTP_SETUP.md for troubleshooting.")

    asyncio.run(_run())


@main.command()
@click.pass_context
def mcp(ctx):
    """Start the MCP server for Claude Desktop integration."""
    import asyncio
    from leadgen.mcp_server.server import main as mcp_main

    console.print("[bold green]Starting LeadGen MCP server...[/bold green]")
    console.print("Add this to your Claude Desktop config:")
    console.print("""
{
  "mcpServers": {
    "leadgen": {
      "command": "python",
      "args": ["-m", "leadgen.mcp"]
    }
  }
}
""")
    asyncio.run(mcp_main())


if __name__ == "__main__":
    main()
