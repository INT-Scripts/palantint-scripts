import argparse
import asyncio
import sys
import questionary
from rich.console import Console
from rich.panel import Panel
from rich import box
from .sync import custom_style, apply_nav_keys

console = Console()

async def interactive_menu():
    while True:
        console.print()
        console.print(Panel.fit(
            "[bold white]PalantINT[/bold white] [blue]Unified Command Center[/blue]",
            border_style="blue",
            box=box.HEAVY,
        ))
        console.print()

        choice = await apply_nav_keys(questionary.select(
            "What would you like to do?",
            choices=[
                questionary.Choice("🔄 ETL Data Pipeline (Scraping & Sync)", value="sync"),
                questionary.Choice("🔑 Admin Management (Manage admin user)", value="admin"),
                questionary.Choice("🗺️  Map Processing (Process floors SVG)", value="map"),
                questionary.Choice("📦 3D Asset Pipeline (Process & Align GLTF)", value="3d"),
                questionary.Choice("❌ Exit", value="exit"),
            ],
            style=custom_style
        )).ask_async()

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

        elif choice == "3d":
            from .process_3d import process_3d_assets
            process_3d_assets()
        
        elif choice in ("exit", "BACK", None):
            console.print("\n[dim]Goodbye.[/dim]")
            break

async def run_admin(args):
    from .admin import create_first_admin
    username = args.username
    password = args.password
    if not username:
        username = await questionary.text("Enter Admin Username:", style=custom_style).ask_async()
    if not password:
        password = await questionary.password("Enter Admin Password:", style=custom_style).ask_async()
    if username and password:
        await create_first_admin(username, password)

def main():
    parser = argparse.ArgumentParser(
        description="PalantINT — Unified Command Line Interface",
        prog="palantint"
    )
    parser.add_argument("--log-file", help="Path to write log output")
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

    # 3D
    three_d_parser = subparsers.add_parser("3d", help="Process and align 3D tiles (GLTF assets)")

    args = parser.parse_args()

    # Redirection to log-file if supplied
    if args.log_file:
        try:
            log_file_obj = open(args.log_file, "a", encoding="utf-8")
            sys.stdout = log_file_obj
            sys.stderr = log_file_obj
            
            # Configure console file target
            global console
            console.file = log_file_obj
            
            # Also import and patch sync's console object
            from .sync import console as sync_console
            sync_console.file = log_file_obj
        except Exception as e:
            sys.stderr.write(f"Error opening log file {args.log_file}: {e}\n")
            sys.exit(1)

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
        asyncio.run(run_pipeline(args))

    elif args.command == "admin":
        asyncio.run(run_admin(args))

    elif args.command == "map":
        from .map_gen import main as map_main
        map_main()

    elif args.command == "3d":
        from .process_3d import process_3d_assets
        process_3d_assets()

    else:
        parser.print_help()

if __name__ == "__main__":
    main()
