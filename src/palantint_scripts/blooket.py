import sys
import argparse
import asyncio
from blooket_int import main_async

def main():
    parser = argparse.ArgumentParser(description="PalantINT Blooket Swarm")
    parser.add_argument("code", help="The Blooket Game ID / Code")
    parser.add_argument("name", help="The base nickname to use")
    parser.add_argument("--count", "-c", type=int, default=4, help="Number of windows to open (default: 4)")
    parser.add_argument("--show", action="store_false", dest="headless", help="Show browser windows (default is headless mode)")
    args = parser.parse_args()
    
    try:
        asyncio.run(main_async(args.code, args.name, args.count, args.headless))
    except KeyboardInterrupt:
        print("\nExiting gracefully...")

if __name__ == "__main__":
    main()
