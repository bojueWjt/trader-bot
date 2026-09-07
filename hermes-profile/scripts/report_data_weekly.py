#!/usr/bin/env python3
import runpy
import sys
from pathlib import Path

sys.argv = ["report_data.py", "7"]
runpy.run_path(str(Path(__file__).with_name("report_data.py")), run_name="__main__")
