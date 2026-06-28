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
@click.option(
    "--source",
    type=click.Choice(["apollo", "hunter", "pdl"]),
    default=None,
    help="Lead source: apollo, hunter (domain), or pdl (100 free credits/mo). Default: apollo, or hunter if --domain given.",
)
@click.option(
    "--domain",
    default=None,
    help="Company domain for Hunter search (e.g. acmecorp.com). Required when using Hunter.",
)
@click.option(
    "--relax-industry",
    is_flag=True,
    default=False,
    help="PDL only: if the industry filter matches nothing, broaden to geography+size. "
    "Returned leads are flagged industry_relaxed and are NOT on-vertical. Off by default (fail closed).",
)
@click.pass_context
def search(ctx, limit, source, domain, relax_industry):
    """Fetch new leads from Apollo, Hunter, or People Data Labs."""
    async def _run():
        from leadgen.config.loader import load_config, load_api_keys
        from leadgen.sources.apollo import ApolloConnector
        from leadgen.sources.hunter import HunterConnector
        from leadgen.sources.pdl import PDLConnector
        from leadgen.crm.database import LeadDatabase

        cfg = load_config(ctx.obj.get("config_path"))
        keys = load_api_keys()
        db = LeadDatabase(cfg.database.sqlite_path)
        await db.init()

        # Determine source: hunter if domain given, else apollo (or pdl if specified)
        use_hunter = source == "hunter" or (source is None and domain)
        use_pdl = source == "pdl"
        if use_hunter:
            if not domain:
                console.print("[red]X[/red] Hunter domain search requires --domain (e.g. --domain acmecorp.com)")
                return
            if not keys.hunter:
                console.print("[red]X[/red] HUNTER_API_KEY is not set. Set it in Doppler (production) or a local .env (development).")
                return
            if not cfg.sources.hunter.get("enabled", False):
                console.print("[yellow]![/yellow] Hunter is disabled in config. Set sources.hunter.enabled: true")

            with console.status(f"Fetching up to {limit} leads from Hunter for {domain}..."):
                async with HunterConnector(cfg, keys) as hunter:
                    leads = await hunter.domain_search(domain=domain, limit=limit)
        elif use_pdl:
            if not keys.pdl:
                console.print("[red]X[/red] PDL_API_KEY is not set. Set it in Doppler (production) or a local .env (development).")
                return
            if not cfg.sources.pdl.get("enabled", False):
                console.print("[yellow]![/yellow] PDL is disabled in config. Set sources.pdl.enabled: true")

            with console.status(f"Fetching {limit} leads from People Data Labs..."):
                async with PDLConnector(cfg, keys) as pdl:
                    leads = await pdl.search(limit=limit, relax_industry=relax_industry)
        else:
            if not keys.apollo:
                console.print("[red]X[/red] APOLLO_API_KEY is not set. Set it in Doppler (production) or a local .env (development).")
                return
            if not cfg.sources.apollo.get("enabled", False):
                console.print("[yellow]![/yellow] Apollo is disabled in config. Set sources.apollo.enabled: true")

            with console.status(f"Fetching {limit} leads from Apollo..."):
                async with ApolloConnector(cfg, keys) as apollo:
                    leads = await apollo.search(limit=limit)

        added = 0
        suppressed = 0
        from leadgen.crm.suppression import check_lead_suppressed

        for lead in leads:
            is_blocked, reason = await check_lead_suppressed(db, lead)
            if is_blocked:
                suppressed += 1
                continue
            is_new = await db.upsert(lead, dedupe_on_identity=True)
            if is_new:
                added += 1

        source_name = "Hunter" if use_hunter else ("People Data Labs" if use_pdl else "Apollo")
        msg = (
            f"[green]OK[/green] Fetched {len(leads)} leads from {source_name}, "
            f"{added} new added to database."
        )
        if suppressed:
            msg += f" {suppressed} suppressed (previously closed_lost/unsubscribed)."
        console.print(msg)

    asyncio.run(_run())


@main.command("import")
@click.argument("file", required=False, type=click.Path(exists=True))
@click.option("--create-sample", is_flag=True, help="Create a sample CSV at data/leads_sample.csv (gitignored)")
@click.option("--limit", default=500, help="Max leads to import from folder (when no file given)")
@click.pass_context
def import_(ctx, file, create_sample, limit):
    """Import leads from a CSV file or from the watch folder."""
    if create_sample:
        from pathlib import Path
        from leadgen.sources.csv_import import SAMPLE_CSV_CONTENT

        data_dir = Path("./data")
        data_dir.mkdir(parents=True, exist_ok=True)
        sample_path = data_dir / "leads_sample.csv"
        sample_path.write_text(SAMPLE_CSV_CONTENT, encoding="utf-8")
        console.print(f"[green]OK[/green] Created {sample_path}")
        console.print("Edit it with your leads, then run: leadgen import data/leads_sample.csv")
        return

    async def _run():
        from leadgen.config.loader import load_config, load_api_keys
        from leadgen.sources.csv_import import CSVImportConnector
        from leadgen.crm.database import LeadDatabase

        cfg = load_config(ctx.obj.get("config_path"))
        keys = load_api_keys()
        db = LeadDatabase(cfg.database.sqlite_path)
        await db.init()

        async with CSVImportConnector(cfg, keys) as importer:
            if file:
                leads = await importer.import_file(file)
            else:
                leads = await importer.import_from_folder(limit=limit)

        if not leads:
            console.print("[yellow]No leads found.[/yellow]")
            if not file:
                console.print("Run 'leadgen import --create-sample' to create a sample CSV in data/")
            return

        added = 0
        for lead in leads:
            is_new = await db.upsert(lead)
            if is_new:
                added += 1

        console.print(f"[green]OK[/green] Imported {len(leads)} leads, {added} new added to database.")

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
            f"[green]OK[/green] Scored {len(unscored)} leads. "
            f"{len(scored)} passed threshold {cfg.scoring.threshold}."
        )

    asyncio.run(_run())


@main.command("enrich")
@click.option("--limit", default=50, help="Max leads to enrich (Hunter rate limits apply)")
@click.pass_context
def enrich(ctx, limit):
    """Find emails for leads missing them using Hunter.io (first+last+domain)."""
    async def _run():
        from leadgen.config.loader import load_config, load_api_keys
        from leadgen.crm.database import LeadDatabase
        from leadgen.sources.hunter import HunterConnector
        from leadgen.models import LeadStatus

        cfg = load_config(ctx.obj.get("config_path"))
        keys = load_api_keys()
        if not keys.hunter:
            console.print("[red]X[/red] HUNTER_API_KEY is not set. Set it in Doppler (production) or a local .env (development).")
            return

        db = LeadDatabase(cfg.database.sqlite_path)
        await db.init()

        leads = await db.list(status=LeadStatus.NEW, limit=limit * 2)  # Fetch extra, filter below
        from leadgen.crm.suppression import check_lead_suppressed

        needs_email = []
        suppressed = 0
        for lead in leads:
            if lead.contact.email:
                continue
            is_blocked, _reason = await check_lead_suppressed(db, lead)
            if is_blocked:
                suppressed += 1
                continue
            needs_email.append(lead)
            if len(needs_email) >= limit:
                break
        if not needs_email:
            if suppressed:
                console.print(
                    f"[yellow]No leads need enrichment — {suppressed} skipped as suppressed.[/yellow]"
                )
            else:
                console.print(
                    "[yellow]No leads need enrichment (all have emails or none match criteria).[/yellow]"
                )
            return

        with console.status(f"Enriching {len(needs_email)} leads with Hunter..."):
            async with HunterConnector(cfg, keys) as hunter:
                enriched = await hunter.enrich_leads_batch(needs_email)

        updated = 0
        for lead in enriched:
            if lead.contact.email:
                lead.status = LeadStatus.ENRICHED
                await db.upsert(lead)
                updated += 1

        found = sum(1 for l in enriched if l.contact.email)
        verified = sum(1 for l in enriched if l.contact.email_verified)
        msg = (
            f"[green]OK[/green] Enriched {len(enriched)} leads: {found} emails found, "
            f"{verified} verified. {updated} saved to database."
        )
        if suppressed:
            msg += f" {suppressed} skipped as suppressed (no Hunter credits spent)."
        console.print(msg)

    asyncio.run(_run())


@main.command("scrape-email")
@click.argument("lead_id")
@click.option("--domain", default=None, help="Override company domain to scrape")
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Write top candidate to the lead as unverified (default: candidates only)",
)
@click.pass_context
def scrape_email_cmd(ctx, lead_id, domain, apply):
    """Scrape published contact emails from a lead's website (Hunter fallback)."""
    async def _run():
        from leadgen.config.loader import load_config
        from leadgen.crm.database import LeadDatabase
        from leadgen.sources.email_scraper import scrape_lead_email_by_id

        cfg = load_config(ctx.obj.get("config_path"))
        db = LeadDatabase(cfg.database.sqlite_path)
        await db.init()

        result = await scrape_lead_email_by_id(
            db, lead_id, domain=domain, apply=apply
        )

        if result.get("error"):
            console.print(f"[red]X[/red] {result['error']}")
            return
        if result.get("status") == "suppressed":
            console.print(f"[yellow]![/yellow] Lead suppressed: {result.get('reason')}")
            return

        console.print(
            f"[green]OK[/green] Scrape for {result.get('name', lead_id)} "
            f"({result.get('domain', '-')}) — status: {result.get('status')}"
        )
        candidates = result.get("candidates") or []
        if not candidates:
            console.print(f"  {result.get('reason', 'No emails found')}")
            return

        for candidate in candidates:
            match = "domain-match" if candidate.get("domain_match") else "other-domain"
            role = "role inbox" if candidate.get("is_role_inbox") else "named inbox"
            console.print(
                f"  {candidate['email']}  ({match}, {role})  "
                f"from {candidate['page_url']}"
            )
        if apply:
            if result.get("applied"):
                console.print(f"  Applied top candidate: {result.get('best_email')}")
            elif result.get("status") == "collision":
                console.print(f"[yellow]![/yellow] {result.get('reason')}")

    asyncio.run(_run())


@main.command("update")
@click.argument("lead_id")
@click.option("--first-name", default=None, help="Contact first name (title-cased on write)")
@click.option("--last-name", default=None, help="Contact last name (title-cased on write)")
@click.option("--full-name", default=None, help="Contact full name (title-cased; splits into first/last)")
@click.option("--email", default=None, help="Contact email to set")
@click.option("--domain", default=None, help="Company domain override")
@click.option("--phone", default=None, help="Contact phone to set")
@click.option(
    "--email-verified",
    is_flag=True,
    default=False,
    help="Mark the email as verified (manual assertion)",
)
@click.option(
    "--verify",
    is_flag=True,
    help="Run Hunter email-verifier on the email (not finder)",
)
@click.option("--status", default=None, help="Pipeline status (e.g. enriched)")
@click.option("--note", default=None, help="Note appended to the lead for provenance")
@click.pass_context
def update_lead_cmd(
    ctx,
    lead_id,
    first_name,
    last_name,
    full_name,
    email,
    domain,
    phone,
    email_verified,
    verify,
    status,
    note,
):
    """Manually set or correct a lead's contact info (name, email, domain, phone)."""
    async def _run():
        from leadgen.config.loader import load_config, load_api_keys
        from leadgen.crm.database import LeadDatabase
        from leadgen.crm.update_lead import update_lead
        from leadgen.sources.hunter import HunterConnector

        cfg = load_config(ctx.obj.get("config_path"))
        keys = load_api_keys()
        db = LeadDatabase(cfg.database.sqlite_path)
        await db.init()

        kwargs: dict = {}
        if first_name is not None:
            kwargs["first_name"] = first_name
        if last_name is not None:
            kwargs["last_name"] = last_name
        if full_name is not None:
            kwargs["full_name"] = full_name
        if email is not None:
            kwargs["email"] = email
        if domain is not None:
            kwargs["domain"] = domain
        if phone is not None:
            kwargs["phone"] = phone
        if email_verified:
            kwargs["email_verified"] = True
        if status is not None:
            kwargs["status"] = status
        if note is not None:
            kwargs["note"] = note

        if verify:
            if not keys.hunter:
                console.print("[red]X[/red] HUNTER_API_KEY is not set. Set it in Doppler (production) or a local .env (development).")
                return
            async with HunterConnector(cfg, keys) as hunter:
                result = await update_lead(
                    db, lead_id, verify=True, hunter=hunter, **kwargs
                )
        else:
            result = await update_lead(db, lead_id, **kwargs)

        if result.get("error"):
            console.print(f"[red]X[/red] {result['error']}")
            return
        if result.get("status") == "collision":
            console.print(f"[yellow]![/yellow] {result['reason']}")
            return

        console.print(
            f"[green]OK[/green] Updated {result.get('name', lead_id)} "
            f"({', '.join(result.get('updated_fields', []))})"
        )
        if any(f in result.get("updated_fields", []) for f in ("first_name", "last_name", "full_name")):
            console.print(f"  name: {result.get('name') or '-'}")
        console.print(f"  email: {result.get('email') or '-'}")
        console.print(f"  email_verified: {result.get('email_verified')}")
        console.print(f"  domain: {result.get('domain') or '-'}")
        console.print(f"  status: {result.get('status')}")

    asyncio.run(_run())


@main.command("suppress")
@click.option("--lead-id", default=None, help="Suppress by existing lead id (uses name+company).")
@click.option("--name", default=None, help="Person name (requires --company).")
@click.option("--company", default=None, help="Company name (requires --name).")
@click.option("--domain", default=None, help="Suppress all leads at this company domain.")
@click.option("--reason", default="manual", help="Reason recorded on the suppression (default: manual).")
@click.option("--list", "show_list", is_flag=True, help="List current suppressions.")
@click.option("--limit", default=50, help="Max rows when using --list.")
@click.pass_context
def suppress(ctx, lead_id, name, company, domain, reason, show_list, limit):
    """Add or list permanent suppressions (excluded from future fetch/enrich).

    Suppressions persist even if the lead row is deleted. Cleanup delete of
    unworked `new` leads does NOT add suppressions.
    """
    async def _run():
        from leadgen.config.loader import load_config
        from leadgen.crm.database import LeadDatabase

        cfg = load_config(ctx.obj.get("config_path"))
        db = LeadDatabase(cfg.database.sqlite_path)
        await db.init()

        if show_list:
            rows = await db.list_suppressions(limit=limit)
            if not rows:
                console.print("[yellow]No suppressions on file.[/yellow]")
                return
            table = Table(title=f"Suppressions (up to {limit})", show_header=True)
            table.add_column("Key", style="dim", max_width=40)
            table.add_column("Reason", max_width=16)
            table.add_column("Name", max_width=20)
            table.add_column("Company", max_width=20)
            table.add_column("Added", max_width=20)
            for row in rows:
                table.add_row(
                    row["suppression_key"][:40],
                    row["reason"],
                    row["display_name"] or "-",
                    row["company_name"] or "-",
                    row["created_at"][:19],
                )
            console.print(table)
            return

        modes = sum(bool(x) for x in (lead_id, domain, name and company))
        if modes != 1:
            raise click.UsageError(
                "Specify exactly one of: --lead-id, --domain, or both --name and --company."
            )

        if lead_id:
            lead = await db.get(lead_id)
            if not lead:
                console.print(f"[red]X[/red] No lead with id {lead_id}")
                return
            key = db.identity_key_from_lead(lead)
            if not key:
                console.print(
                    "[red]X[/red] Lead lacks both person name and company — cannot suppress."
                )
                return
            added = await db.add_suppression(
                key,
                reason,
                source_lead_id=lead.id,
                display_name=lead.display_name,
                company_name=lead.company.name,
            )
            label = f"{lead.display_name} @ {lead.company.name}"
        elif domain:
            key = db.domain_suppression_key(domain)
            added = await db.add_suppression(
                key,
                reason,
                company_name=domain,
            )
            label = f"domain {domain}"
        else:
            contact = {"full_name": name, "first_name": "", "last_name": ""}
            company_dict = {"name": company}
            key = db._name_company_key(contact, company_dict)
            if not key:
                console.print("[red]X[/red] --name and --company must both be non-empty.")
                return
            added = await db.add_suppression(
                key,
                reason,
                display_name=name,
                company_name=company,
            )
            label = f"{name} @ {company}"

        if added:
            console.print(f"[green]OK[/green] Suppressed {label} ({reason}).")
        else:
            console.print(f"[yellow]Already suppressed:[/yellow] {label}")

    asyncio.run(_run())


@main.command("list")
@click.option("--limit", default=100, help="Max leads to show")
@click.option("--status", default=None, help="Filter by status (e.g. new, scored)")
@click.pass_context
def list_(ctx, limit, status):
    """List leads in the database with id, name, company, email, source."""
    async def _run():
        from leadgen.config.loader import load_config
        from leadgen.crm.database import LeadDatabase
        from leadgen.models import LeadStatus

        cfg = load_config(ctx.obj.get("config_path"))
        db = LeadDatabase(cfg.database.sqlite_path)
        await db.init()

        status_filter = LeadStatus(status) if status else None
        leads = await db.list(status=status_filter, limit=limit)

        if not leads:
            console.print("[yellow]No leads found.[/yellow]")
            return

        table = Table(title=f"Leads (showing up to {limit})", show_header=True)
        table.add_column("ID", style="dim", max_width=12)
        table.add_column("Name", max_width=25)
        table.add_column("Company", max_width=25)
        table.add_column("Email", max_width=30)
        table.add_column("Source", max_width=8)
        table.add_column("Status", max_width=10)
        for l in leads:
            table.add_row(
                l.id[:12] + "..." if len(l.id) > 12 else l.id,
                (l.contact.full_name or f"{l.contact.first_name or ''} {l.contact.last_name or ''}".strip() or "-")[:25],
                (l.company.name or "-")[:25],
                (l.contact.email or "-")[:30],
                l.source.value,
                l.status.value,
            )
        console.print(table)

    asyncio.run(_run())


@main.command("dedupe")
@click.option("--dry-run", is_flag=True, help="Show duplicates without deleting")
@click.option("--keep", type=click.Choice(["oldest", "newest"]), default="oldest", help="Which duplicate to keep")
@click.pass_context
def dedupe(ctx, dry_run, keep):
    """Find and remove duplicate leads (by email, or name+company+domain when email missing)."""
    async def _run():
        from leadgen.config.loader import load_config
        from leadgen.crm.database import LeadDatabase

        cfg = load_config(ctx.obj.get("config_path"))
        db = LeadDatabase(cfg.database.sqlite_path)
        await db.init()

        dupes = await db.find_duplicates()
        if not dupes:
            console.print("[green]OK[/green] No duplicates found.")
            return

        total_dupes = sum(len(ids) - 1 for _, ids in dupes)
        console.print(f"Found {len(dupes)} duplicate groups ({total_dupes} extra leads).")
        for key, ids in dupes[:10]:  # Show first 10
            console.print(f"  {key[:50]}... : {len(ids)} copies")
        if len(dupes) > 10:
            console.print(f"  ... and {len(dupes) - 10} more groups")

        if dry_run:
            console.print("[yellow]Dry run. Run without --dry-run to delete duplicates.[/yellow]")
            return

        deleted = await db.delete_duplicates(keep=keep)
        console.print(f"[green]OK[/green] Removed {deleted} duplicate leads (kept {keep}).")

    asyncio.run(_run())


@main.command("purge")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt (for scripted use).")
@click.pass_context
def purge(ctx, yes):
    """Delete ALL leads from the active database. Destructive and irreversible.

    CLI-only by design: this is intentionally NOT exposed as an MCP tool so the
    agent can never wipe the database on its own.
    """
    async def _run():
        from leadgen.config.loader import load_config
        from leadgen.crm.database import LeadDatabase

        cfg = load_config(ctx.obj.get("config_path"))
        db = LeadDatabase(cfg.database.sqlite_path)
        await db.init()

        count = await db.count_all()
        if count == 0:
            console.print("[yellow]No leads to purge — the database is already empty.[/yellow]")
            return

        if not yes:
            console.print(
                f"[bold red]About to delete all {count} leads[/bold red] from "
                f"[cyan]{cfg.database.sqlite_path}[/cyan]. This cannot be undone."
            )
            if not click.confirm("Are you sure you want to purge every lead?"):
                console.print("[yellow]Aborted. No leads were deleted.[/yellow]")
                return

        deleted = await db.delete_all()
        console.print(f"[green]OK[/green] Purged {deleted} leads from the database.")

    asyncio.run(_run())


def _lead_delete_preview_rows(leads) -> list[tuple[str, str, str, str, str]]:
    """Build table rows for delete confirmation previews."""
    rows = []
    for lead in leads:
        name = (
            lead.contact.full_name
            or f"{lead.contact.first_name or ''} {lead.contact.last_name or ''}".strip()
            or "-"
        )
        rows.append(
            (
                lead.id[:12] + "..." if len(lead.id) > 12 else lead.id,
                name[:25],
                (lead.company.name or "-")[:25],
                (lead.contact.email or "-")[:30],
                lead.status.value,
            )
        )
    return rows


@main.command("delete")
@click.option(
    "--ids",
    default=None,
    help="Comma-separated lead ids to delete (mutually exclusive with --status).",
)
@click.option(
    "--status",
    default=None,
    help="Delete all leads in this status, e.g. new (mutually exclusive with --ids).",
)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt (for scripted use).")
@click.pass_context
def delete_leads(ctx, ids, status, yes):
    """Delete targeted leads from the active database. Destructive and irreversible.

    CLI-only by design: this is intentionally NOT exposed as an MCP tool so the
    agent can never delete leads on its own.
    """
    if ids and status:
        raise click.UsageError("Cannot specify both --ids and --status.")
    if not ids and not status:
        raise click.UsageError("Must specify --ids or --status.")

    async def _run():
        from leadgen.config.loader import load_config
        from leadgen.crm.database import LeadDatabase
        from leadgen.models import LeadStatus

        cfg = load_config(ctx.obj.get("config_path"))
        db = LeadDatabase(cfg.database.sqlite_path)
        await db.init()

        sample_limit = 5

        if ids:
            id_list = [i.strip() for i in ids.split(",") if i.strip()]
            if not id_list:
                console.print("[red]X[/red] --ids was empty after parsing.")
                return
            preview = await db.get_by_ids(id_list)
            count = len(preview)
            if count == 0:
                console.print("[yellow]No matching leads found for the given ids.[/yellow]")
                return
            target_desc = f"{count} lead(s) by id"
        else:
            try:
                status_filter = LeadStatus(status)
            except ValueError:
                valid = ", ".join(s.value for s in LeadStatus)
                console.print(f"[red]X[/red] Unknown status {status!r}. Valid: {valid}")
                return
            preview = await db.list(status=status_filter, limit=sample_limit)
            counts = await db.count_by_status()
            count = counts.get(status_filter.value, 0)
            if count == 0:
                console.print(
                    f"[yellow]No leads with status {status_filter.value!r} to delete.[/yellow]"
                )
                return
            target_desc = f"{count} lead(s) with status {status_filter.value!r}"

        if not yes:
            console.print(
                f"[bold red]About to delete {target_desc}[/bold red] from "
                f"[cyan]{cfg.database.sqlite_path}[/cyan]. This cannot be undone."
            )
            table = Table(title=f"Sample (up to {sample_limit})", show_header=True)
            table.add_column("ID", style="dim", max_width=12)
            table.add_column("Name", max_width=25)
            table.add_column("Company", max_width=25)
            table.add_column("Email", max_width=30)
            table.add_column("Status", max_width=10)
            for row in _lead_delete_preview_rows(preview):
                table.add_row(*row)
            console.print(table)
            if count > sample_limit:
                console.print(f"[dim]... and {count - sample_limit} more[/dim]")
            if not click.confirm(f"Delete {count} lead(s)?"):
                console.print("[yellow]Aborted. No leads were deleted.[/yellow]")
                return

        if ids:
            deleted = await db.delete_by_ids(id_list)
        else:
            deleted = await db.delete_by_status(status_filter)

        console.print(f"[green]OK[/green] Deleted {deleted} lead(s).")

    asyncio.run(_run())


@main.command()
@click.pass_context
def pipeline(ctx):
    """Show a summary of the current lead pipeline."""
    async def _run():
        from leadgen.config.loader import display_agent_name, load_config
        from leadgen.crm.database import LeadDatabase

        cfg = load_config(ctx.obj.get("config_path"))
        db = LeadDatabase(cfg.database.sqlite_path)
        await db.init()

        counts = await db.count_by_status()

        table = Table(
            title=f"LeadGen Pipeline ({display_agent_name(cfg)})",
            show_header=True,
        )
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
@click.option("--dry-run", is_flag=True, help="Simulate sending without actually sending emails")
@click.option("--limit", default=30, help="Max leads to process (default: daily_email_limit)")
@click.pass_context
def send(ctx, dry_run, limit):
    """Send approved outreach emails to queued leads."""
    async def _run():
        from leadgen.config.loader import load_config, load_api_keys
        from leadgen.crm.database import LeadDatabase
        from leadgen.outreach.email import EmailSender
        from leadgen.models import LeadStatus

        cfg = load_config(ctx.obj.get("config_path"))
        keys = load_api_keys()
        db = LeadDatabase(cfg.database.sqlite_path)
        await db.init()

        sender = EmailSender(cfg, keys, db, dry_run=dry_run)

        if not sender.use_smtp and not sender.use_sendgrid:
            console.print("[red]X[/red] No email backend configured. Set SMTP_* or SENDGRID_API_KEY in .env")
            return

        leads = await db.list(status=LeadStatus.QUEUED, limit=limit)
        if not leads:
            console.print("[yellow]No queued leads with approved outreach.[/yellow]")
            return

        with console.status(f"{'[DRY RUN] ' if dry_run else ''}Sending to {len(leads)} leads..."):
            summary = await sender.send_batch(leads)

        console.print(
            f"[green]OK[/green] {'Dry run: would have sent' if dry_run else 'Sent'} "
            f"{summary['sent']} | skipped {summary['skipped']} | failed {summary['failed']}"
        )

    asyncio.run(_run())


@main.command("apollo-test")
@click.pass_context
def apollo_test(ctx):
    """Test Apollo API key and show plan/access info."""
    async def _run():
        from leadgen.config.loader import load_api_keys

        keys = load_api_keys()
        if not keys.apollo:
            console.print("[red]X[/red] APOLLO_API_KEY is not set. Set it in Doppler (production) or a local .env (development).")
            return

        try:
            import httpx

            # Apollo health endpoint - validates key
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://api.apollo.io/v1/auth/health",
                    headers={"X-Api-Key": keys.apollo, "Cache-Control": "no-cache"},
                )
                if r.status_code == 200:
                    data = r.json()
                    console.print("[green]OK[/green] Apollo API key is valid.")
                    console.print(f"  Health: {data}")
                else:
                    console.print(f"[red]X[/red] Apollo returned {r.status_code}: {r.text[:200]}")
        except Exception as e:
            console.print(f"[red]X[/red] Apollo test failed: {e}")

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
            console.print("[red]X[/red] SMTP not configured. Set SMTP_HOST, SMTP_USERNAME, and SMTP_PASSWORD as environment variables (Doppler in production, or a local .env in development).")
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

            console.print("[green]OK[/green] SMTP connection successful.")
            console.print(f"  Host: {keys.smtp_host}:{keys.smtp_port}")
            console.print(f"  From: {keys.smtp_from_name or 'N/A'} <{keys.smtp_from_email or keys.smtp_username}>")
        except Exception as e:
            console.print(f"[red]X[/red] SMTP connection failed: {e}")
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
