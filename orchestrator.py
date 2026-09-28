"""Bounded, auditable SDLC workflow for the three assessment scenarios.

Agents are deterministic, specialized executors. They share a persisted run state;
the orchestrator alone decides when a node may run and what it may write.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GRAPH = {
    "intake": [],
    "architecture": ["intake"],
    "impact": ["intake"],
    "implementation": ["architecture", "impact"],
    "tests": ["implementation"],
    "documentation": ["implementation"],
    "validation": ["tests", "documentation"],
    "approval": ["validation"],
    "release": ["approval"],
}
SCENARIOS = {
    "greenfield": "Build a URL shortener with create, redirect, per-link click analytics, persistent storage, and tests.",
    "brownfield": "Improve the existing URL shortener: add DELETE /api/links/{code} with safe missing-link behavior and tests.",
    "ambiguous": "Make links expire sometime later. Clarify duration and what happens after expiry before changing code.",
}


@dataclass
class State:
    scenario: str
    requirement: str
    directory: Path
    approved: bool
    done: set[str]
    artifacts: dict
    events: list
    failure: str | None = None

    def save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {"scenario": self.scenario, "requirement": self.requirement,
                   "done": sorted(self.done), "artifacts": self.artifacts,
                   "events": self.events, "failure": self.failure}
        (self.directory / "state.json").write_text(json.dumps(payload, indent=2) + "\n")

    def emit(self, node, outcome, detail):
        self.events.append({"time": time.time(), "node": node, "outcome": outcome, "detail": detail})
        self.save()


def write(state, filename, data):
    target = state.directory / filename
    if target.resolve().parent != state.directory.resolve():
        raise ValueError("write outside run directory denied")
    target.write_text(data)
    return filename


def agent(state: State, node: str):
    """Only explicit artifacts and the bounded test command are authorized tools."""
    if node == "intake":
        ambiguous = state.scenario == "ambiguous" or any(x in state.requirement.lower() for x in ("sometime", "somehow", "better"))
        return {"assumptions": ["Local SQLite persistence", "HTTP only on localhost by default"],
                "questions": (["What is the expiry duration?", "Should expired links return 404 or 410?"] if ambiguous else []),
                "acceptance": ["Create URL", "Redirect with click count", "Return clear errors", "Automated tests pass"]}
    if node == "architecture":
        return {"components": ["HTTP handler", "SQLite Store", "SDLC orchestrator", "run state + artifacts"],
                "decisions": ["single process prototype", "explicit DAG with parallel independent stages", "approval before release"]}
    if node == "impact":
        if state.scenario == "brownfield":
            return {"modules": ["shortener.py Store", "shortener.py Handler", "tests/test_shortener.py"],
                    "flow": "DELETE request -> validate code -> Store.delete -> 204 or 404"}
        return {"modules": ["shortener.py", "tests/test_shortener.py"], "flow": "POST -> SQLite -> GET redirect -> click count"}
    if node == "implementation":
        if state.artifacts["intake"]["questions"]:
            return {"status": "proposal only", "reason": "Unresolved requirements block code changes"}
        source = (ROOT / "shortener.py").read_text()
        if state.scenario == "brownfield":
            marker = "    def stats(self, code: str) -> dict | None:\n"
            addition = ("    def delete(self, code: str) -> bool:\n"
                        "        with self.lock:\n"
                        "            with self._db() as db:\n"
                        "                return db.execute(\"DELETE FROM links WHERE code=?\", (code,)).rowcount == 1\n\n")
            if source.count(marker) != 1:
                raise ValueError("codebase changed: delete insertion point missing")
            source = source.replace(marker, addition + marker)
            marker2 = "        def do_GET(self):\n"
            addition2 = ("        def do_DELETE(self):\n"
                         "            match = re.fullmatch(r\"/api/links/([A-Za-z0-9_-]{3,32})\", self.path)\n"
                         "            if not match:\n"
                         "                return self.respond(404, {\"error\": \"not found\"})\n"
                         "            if not store.delete(match.group(1)):\n"
                         "                return self.respond(404, {\"error\": \"not found\"})\n"
                         "            self.send_response(204)\n"
                         "            self.end_headers()\n\n")
            if source.count(marker2) != 1:
                raise ValueError("codebase changed: handler insertion point missing")
            source = source.replace(marker2, addition2 + marker2)
        write(state, "shortener.py", source)
        return {"status": "staged", "sha256": hashlib.sha256(source.encode()).hexdigest(), "file": "shortener.py"}
    if node == "tests":
        if state.artifacts["implementation"]["status"] == "proposal only":
            return {"status": "blocked", "reason": "Need clarified acceptance criteria"}
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests"), "-p", "test_shortener.py", "-v"],
                                cwd=state.directory, capture_output=True, text=True, timeout=30)
        write(state, "test-results.txt", result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("tests failed; see test-results.txt")
        return {"status": "passed", "log": "test-results.txt"}
    if node == "documentation":
        text = (f"# {state.scenario.title()} scenario\n\n"
                f"Requirement: {state.requirement}\n\n"
                f"Impact: {json.dumps(state.artifacts['impact'])}\n\n"
                "Run locally: `python shortener.py`. See repository README for API examples.\n")
        return {"file": write(state, "scenario.md", text)}
    if node == "validation":
        ambiguous = bool(state.artifacts["intake"]["questions"])
        status = "clarification needed" if ambiguous else "ready for human review"
        return {"status": status, "risks": ["No authentication or rate limits", "SQLite is single-host", "No deployment automation"],
                "checks": ["tests passed" if not ambiguous else "implementation blocked safely", "artifacts retained"]}
    if node == "approval":
        if state.artifacts["validation"]["status"] == "clarification needed":
            return {"status": "withheld", "reason": "Human must clarify requirements"}
        return {"status": "granted" if state.approved else "pending", "reason": "human release approval required"}
    if node == "release":
        if state.artifacts["approval"]["status"] != "granted":
            return {"status": "blocked", "reason": state.artifacts["approval"]["reason"]}
        return {"status": "approved artifact", "file": state.artifacts["implementation"]["file"]}
    raise KeyError(node)


def run(scenario: str, output: Path, approved=False, requirement=None):
    if scenario not in SCENARIOS:
        raise ValueError("unknown scenario")
    target = output.resolve() / scenario
    requested = requirement or SCENARIOS[scenario]
    if target.exists():
        prior = json.loads((target / "state.json").read_text()) if (target / "state.json").exists() else {}
        if prior.get("requirement") != requested:
            # Changed upstream input invalidates every downstream artifact; retain prior run for audit.
            backup = target.with_name(target.name + "-previous-" + str(int(time.time())))
            shutil.move(target, backup)
        else:
            shutil.rmtree(target)  # Recompute deterministic local output; no external side effects.
    state = State(scenario, requested, target, approved, set(), {}, [])
    state.save()
    with ThreadPoolExecutor(max_workers=2) as pool:
        while len(state.done) < len(GRAPH):
            ready = [n for n, deps in GRAPH.items() if n not in state.done and set(deps) <= state.done]
            if not ready:
                raise RuntimeError("dependency graph deadlocked")
            futures = [(n, pool.submit(agent, state, n)) for n in ready]
            for node, future in futures:
                for attempt in range(2):
                    try:
                        result = future.result() if attempt == 0 else agent(state, node)
                        state.artifacts[node] = result
                        state.done.add(node)
                        state.emit(node, "success", result.get("status", "complete"))
                        break
                    except Exception as exc:
                        state.emit(node, "retry" if attempt == 0 else "safe-stop", str(exc))
                        if attempt:
                            state.failure = f"{node}: {exc}"
                            state.save()
                            return state
    write(state, "summary.md", "# Engineering summary\n\n" +
          f"Scenario: {scenario}\n\nPlan: {', '.join(GRAPH)}\n\n" +
          f"Outcome: {state.artifacts['release']}\n\n" +
          f"Assumptions: {state.artifacts['intake']['assumptions']}\n\n" +
          f"Questions: {state.artifacts['intake']['questions']}\n\n" +
          f"Risks and limitations: {state.artifacts['validation']['risks']}\n")
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=[*SCENARIOS, "all"])
    parser.add_argument("--output", type=Path, default=ROOT / "runs")
    parser.add_argument("--approve", action="store_true", help="explicit human authorization for local release gate")
    parser.add_argument("--requirement", help="override scenario requirement")
    args = parser.parse_args()
    if args.scenario == "all" and args.requirement:
        parser.error("--requirement requires one scenario")
    for name in (SCENARIOS if args.scenario == "all" else [args.scenario]):
        state = run(name, args.output, args.approve, args.requirement)
        print(f"{name}: {state.failure or state.artifacts.get('release')}")
        if state.failure:
            sys.exit(1)
