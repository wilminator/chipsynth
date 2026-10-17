# Copyright (C) 2026 Michael Allen Wilmes
"""chipsynth must stay a library: it never imports the game (blobs), and pygame only inside output.py (lazily) and the composer/ subpackage."""
import ast
import sys
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
ALLOWED = {"numpy", "scipy"}


def imports(path):
    """[(module name, level, lazy)] for every import in a file; lazy = inside a function."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    def walk(node, lazy):
        for ch in ast.iter_child_nodes(node):
            inner = lazy or isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef))
            if isinstance(ch, ast.Import):
                found.extend((a.name.split(".")[0], 0, lazy) for a in ch.names)
            elif isinstance(ch, ast.ImportFrom):
                found.append(((ch.module or "").split(".")[0], ch.level, lazy))
            walk(ch, inner)
    walk(tree, False)
    return found


class BoundaryTests(unittest.TestCase):
    def files(self):
        return sorted(p for p in PKG.glob("*.py"))

    def composer_files(self):
        return sorted((PKG / "composer").glob("*.py"))

    def test_never_imports_the_game(self):
        for p in self.files() + self.composer_files() + sorted((PKG / "tests").glob("*.py")):
            for mod, level, _ in imports(p):
                self.assertNotEqual(mod, "blobs", "%s imports the game" % p.name)

    def test_only_numpy_and_scipy_besides_the_standard_library(self):
        std = set(sys.stdlib_module_names)
        for p in self.files() + self.composer_files():
            in_composer = p.parent.name == "composer"
            for mod, level, lazy in imports(p):
                if level or mod in std or mod == "chipsynth":
                    continue
                if mod == "pygame":
                    if in_composer:
                        continue                # the composer is the optional extra that needs pygame
                    self.assertEqual(p.name, "output.py", "%s imports pygame" % p.name)
                    self.assertTrue(lazy, "output.py must import pygame lazily")
                    continue
                self.assertIn(mod, ALLOWED, "%s imports %s" % (p.name, mod))

    def test_composer_relative_imports_stay_inside_the_package(self):
        for p in self.composer_files():
            for mod, level, _ in imports(p):
                self.assertLessEqual(level, 1, "%s reaches above composer/" % p.name)

    def test_the_core_loads_without_pygame_or_the_game(self):
        import subprocess
        code = ("import sys; sys.modules['pygame'] = None; import chipsynth; "
                "assert 'blobs' not in sys.modules; chipsynth.parse('V1: c')")
        subprocess.check_call([sys.executable, "-c", code], cwd=str(PKG.parent))


if __name__ == "__main__":
    unittest.main()
