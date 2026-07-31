import sys
import os

# Add bot directory to path
sys.path.insert(0, os.path.dirname(__file__))

from bot import main

if __name__ == "__main__":
    main()
