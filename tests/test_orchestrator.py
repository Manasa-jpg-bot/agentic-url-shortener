import tempfile
import unittest
from pathlib import Path

from orchestrator import run


class OrchestrationTests(unittest.TestCase):
    def test_parallel_dependencies_and_approval_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = run("greenfield", Path(tmp))
            self.assertIsNone(state.failure)
            self.assertEqual(state.artifacts["tests"]["status"], "passed")
            self.assertEqual(state.artifacts["release"]["status"], "blocked")
            order = {e["node"]: i for i, e in enumerate(state.events) if e["outcome"] == "success"}
            self.assertLess(order["intake"], order["implementation"])
            self.assertLess(order["tests"], order["validation"])
            approved = run("greenfield", Path(tmp), approved=True)
            self.assertEqual(approved.artifacts["release"]["status"], "approved artifact")

    def test_ambiguous_requirement_stops_code_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = run("ambiguous", Path(tmp), approved=True)
            self.assertIsNone(state.failure)
            self.assertEqual(state.artifacts["implementation"]["status"], "proposal only")
            self.assertEqual(state.artifacts["approval"]["status"], "withheld")
            self.assertFalse((state.directory / "shortener.py").exists())


if __name__ == "__main__":
    unittest.main()
