import sys
import os

# Ensure the package is in path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from palantint_scripts.sync import main

if __name__ == "__main__":
    main()
