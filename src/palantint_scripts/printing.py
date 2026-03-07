import sys
import os
import argparse
from tsprint.cli import main as tsprint_main

def main():
    # We can just delegate to the tsprint main, 
    # but we might want to ensure environment variables are loaded here.
    from dotenv import load_dotenv
    load_dotenv()
    
    # Delegate everything to tsprint CLI logic
    tsprint_main()

if __name__ == "__main__":
    main()
