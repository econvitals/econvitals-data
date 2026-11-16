#!/usr/bin/env python3
"""The Stance Matrix's RBI reader is macrolib's (`macrolib/rbi.py`, the one RBI parser since
2026-11-15). This builder runs in a GitHub Action that cannot install macrolib (a private repo),
so it carries `rbi.py` beside it as a BYTE-IDENTICAL copy — and this test fails the moment the two
differ. Fix by copying macrolib's file over this one, never by editing the copy:

    cp ~/Code/macrolib/macrolib/rbi.py builders/central-bank-stance/rbi.py

Skips where no macrolib checkout is present (the Action). Standard library only, no network:
    python3 builders/central-bank-stance/test_rbi_copy.py
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
CANONICAL = Path.home() / "Code" / "macrolib" / "macrolib" / "rbi.py"


class OneRBIParser(unittest.TestCase):
    def test_the_copy_is_macrolibs_file_byte_for_byte(self):
        if not CANONICAL.exists():
            self.skipTest(f"no macrolib checkout at {CANONICAL.parent.parent}")
        self.assertEqual((HERE / "rbi.py").read_bytes(), CANONICAL.read_bytes(),
                         "builders/central-bank-stance/rbi.py has drifted from macrolib/rbi.py; "
                         "copy macrolib's over it (see this file's docstring)")

    def test_fast_sources_reads_india_through_it(self):
        import fast_sources as fs
        self.assertIs(fs.FAST["IN"][1], fs.rbi_in)
        self.assertIs(fs.rbi_in, fs._rbi.rbi_in)
        self.assertEqual(Path(fs._rbi.__file__).name, "rbi.py")

    def test_the_copy_stands_alone(self):
        src = (HERE / "rbi.py").read_text()
        self.assertNotIn("from .", src)
        self.assertNotIn("import macrolib", src)


if __name__ == "__main__":
    unittest.main()
