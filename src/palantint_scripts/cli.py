import argparse
import asyncio
import sys
import questionary
from rich.console import Console
from rich.panel import Panel
from rich import box
from .sync import custom_style

console = Console()

async def interactive_menu():
    console.print()
    console.print(Panel.fit(
        "[bold white]PalantINT[/bold white] [blue]Unified Command Center[/blue]",
        border_style="blue",
        box=box.HEAVY,
    ))
    console.print()

    choice = await questionary.select(
        "What would you like to do?",
        choices=[
            questionary.Choice("🔄 ETL Data Pipeline (Scraping & Sync)", value="sync"),
            questionary.Choice("🔑 Admin Management (Manage admin user)", value="admin"),
            questionary.Choice("🗺️  Map Processing (Process floors SVG)", value="map"),
        ],
        style=custom_style
    ).ask_async()

    if choice == "sync":
        from .sync import run_pipeline
        await run_pipeline()
    
    elif choice == "admin":
        from .admin import create_first_admin
        username = await questionary.text("Enter Admin Username:", style=custom_style).ask_async()
        password = await questionary.password("Enter Admin Password:", style=custom_style).ask_async()
        if username and password:
            await create_first_admin(username, password)
            
    elif choice == "map":
        from .map_gen import main as map_main
        map_main()

def main():
    parser = argparse.ArgumentParser(
        description="PalantINT — Unified Command Line Interface",
        prog="palantint"
    )
    subparsers = parser.add_subparsers(dest="command", help="Operational commands")

    # Sync
    sync_parser = subparsers.add_parser("sync", help="Synchronize data (ETL Pipeline)")
    sync_parser.add_argument("--scrape", action="store_true", help="Run scraping phase only")
    sync_parser.add_argument("--load", action="store_true", help="Run load phase only")
    sync_parser.add_argument("--export", action="store_true", help="Run export phase only")
    sync_parser.add_argument("--purge", action="store_true", help="Wipe database before loading")
    sync_parser.add_argument("--hydrate", action="store_true", help="Force re-verify all records")

    # Admin
    admin_parser = subparsers.add_parser("admin", help="Manage administrative users")
    admin_parser.add_argument("username", nargs='?', help="Username of the admin")
    admin_parser.add_argument("password", nargs='?', help="Password of the admin")

    # Map
    map_parser = subparsers.add_parser("map", help="Generate interactive floor plans (SVG processing)")

    args = parser.parse_args()

    if args.command is None and len(sys.argv) == 1:
        # Start interactive mode
        try:
            asyncio.run(interactive_menu())
        except KeyboardInterrupt:
            console.print("\n[dim]Aborted.[/dim]")
        return

    # Non-interactive logic
    if args.command == "sync":
        from .sync import run_pipeline
        asyncio.run(run_pipeline())

    elif args.command == "admin":
        from .admin import create_first_admin
        if not args.username or not args.password:
            console.print("[red]Error: Username and Password required for CLI admin command.[/red]")
            return
        asyncio.run(create_first_admin(args.username, args.password))

    elif args.command == "map":
        from .map_gen import main as map_main
        map_main()

    else:
        parser.print_help()

if __name__ == "__main__":
    main()
