"""Run the complete deterministic original regression suite."""
from pathlib import Path
import sys
import unittest


def main():
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or later is required")
    root = Path(__file__).resolve().parent
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern="test_*.py")
    if suite.countTestCases() == 0:
        raise SystemExit("no tests discovered")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
