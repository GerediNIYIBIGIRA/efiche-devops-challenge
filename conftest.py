import sys
import os

# Adds the project root to sys.path so `ops_agent` is importable
# when running pytest or uvicorn from this directory.
sys.path.insert(0, os.path.dirname(__file__))
