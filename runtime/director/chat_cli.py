"""Opt-in, no-cost local terminal conversation with The Director.

Create a conversation (from the repository root)::

    python -m runtime.director.chat_cli --database ./director_chat.sqlite3 \
        --thread research --goal-id research --objective "Review local evidence" \
        --metric quality --target 8 --direction INCREASE --observed-value 3

Resume later with ``--database ./director_chat.sqlite3 --thread research``.
The conversation is durable but the declared world snapshot is selected on
each invocation. Use --observed-value or --world-json to provide fresh data,
or --coordinator-db PATH to read an existing local coordinator's declared
goal, latest world events, and dependency-ready analytical alternatives.

This CLI is a foreground, symbolic reasoning prototype, NOT a pretrained LLM,
daemon, network listener, or channel to unknown entities. No host tool, shell,
paid service, or worker is launched. The database is plaintext: do not enter
passwords, secrets, or private memories by default. Speakers and evidence
origins are caller claims, not authenticated actors.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import TextIO

from .coordinator import DirectorCoordinator
from .dialogue import DirectorDialogue
from .reasoning import LocalReasoner
from .strategy import generate_candidates
from .world_state import WorldState


_GOAL_ARGS = ("goal_id", "objective", "metric", "target", "direction")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local, opt-in Director symbolic reasoning conversation ($0; no network)."
    )
    parser.add_argument("--database", type=Path, required=True,
                        help="Operator-selected local SQLite file; contains plaintext conversation history.")
    parser.add_argument("--thread", default="main", help="Conversation identifier to create or resume.")
    parser.add_argument("--goal-id", help="Goal identifier, required when creating a new thread.")
    parser.add_argument("--objective", help="Human-specified objective for a new thread.")
    parser.add_argument("--metric", help="Measurable objective metric for a new thread.")
    parser.add_argument("--target", type=float, help="Finite target value for a new thread.")
    parser.add_argument("--direction", choices=("INCREASE", "DECREASE"),
                        help="Target direction for a new thread.")
    observations = parser.add_mutually_exclusive_group()
    observations.add_argument("--observed-value", type=float,
                              help="Optional, unverified local observation for this session only.")
    observations.add_argument("--world-json", type=Path,
                              help="Optional prebuilt WorldState.snapshot() JSON file, read only.")
    observations.add_argument("--coordinator-db", type=Path,
                              help="Read the current goal and declared world events from an existing local coordinator DB, read only.")
    return parser


def _declared_goal(args: argparse.Namespace) -> dict[str, object] | None:
    vals = {name: getattr(args, name) for name in _GOAL_ARGS}
    if all(value is None for value in vals.values()):
        return None
    if any(value is None for value in vals.values()):
        missing = ", ".join("--" + key.replace("_", "-") for key, val in vals.items() if val is None)
        raise ValueError("new thread goal requires all five flags; missing: " + missing)
    if not math.isfinite(args.target) or abs(args.target) > 10**12:
        raise ValueError("--target must be finite and bounded")
    return {"goal_id": args.goal_id, "objective": args.objective, "metric": args.metric,
            "target_value": args.target, "direction": args.direction}


def _read_goal(database: Path, thread_id: str) -> dict[str, object]:
    # Read from the dialogue's own persisted thread table. This lookup never
    # writes and does not confer authority; DirectorDialogue validates goals.
    with closing(sqlite3.connect(database)) as conn:
        record = conn.execute("SELECT goal_json FROM director_dialogue_threads WHERE thread_id=?",
                              (thread_id,)).fetchone()
    if record is None:
        raise KeyError(thread_id)
    return json.loads(record[0])


def _coordinator_snapshot(path: Path) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]]]:
    """Read a consistent snapshot without initializing or mutating a store.

    This is intentionally a read-only SQLite view of the coordinator's
    existing public persistence contract. It does not need or open a mailbox,
    authenticate records, launch agents, or acquire effect authority.
    """
    if not path.is_file():
        raise ValueError("--coordinator-db must be an existing SQLite file")
    uri = path.resolve(strict=False).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        row = conn.execute("SELECT config_json FROM director_strategy_config WHERE singleton=1").fetchone()
        events = conn.execute("SELECT event_json FROM director_strategy_events ORDER BY event_seq").fetchall()
        # Reuse the coordinator's pure read-only graph readiness check, so
        # dialogue cannot recommend a dependency-blocked or resource-blocked
        # node. Unlike coordinator.tick, this cannot dispatch assignments.
        ready = DirectorCoordinator._graph_ready_ids(conn)
        conn.execute("COMMIT")
    if row is None:
        raise ValueError("coordinator has no configured goal")
    config = json.loads(row[0])
    if not isinstance(config, dict) or not isinstance(config.get("goal"), dict):
        raise ValueError("coordinator configuration has no valid declared goal")
    world = WorldState()
    for event in events:
        world.ingest(json.loads(event["event_json"]))
    snapshot = world.snapshot()
    candidates = generate_candidates(config["goal"], config["capabilities"], snapshot,
                                     max_candidates=32)
    options = [
        {"option_id": item["capability_id"], "kind": item["kind"],
         "expected_delta": item["expected_delta"], "cost_units": 0,
         "assumptions": ["Coordinator-declared analytical capability; simulated effect only"]}
        for item in candidates if ready is None or item["capability_id"] in ready
    ]
    return config["goal"], snapshot, options


def _world(args: argparse.Namespace, metric: str) -> dict[str, object]:
    if args.world_json is not None:
        source = args.world_json
        if not source.is_file() or source.stat().st_size > 128 * 1024:
            raise ValueError("--world-json must be an existing, bounded JSON file")
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("--world-json is not valid UTF-8 JSON") from exc
        return WorldState.from_snapshot(raw).snapshot()

    world = WorldState()
    if args.observed_value is None:
        return world.snapshot()
    observed = args.observed_value
    if not math.isfinite(observed) or abs(observed) > 10**12:
        raise ValueError("--observed-value must be finite and bounded")
    world.ingest({
        "event_id": "cli.declared.observation.1",
        "claim_id": metric,
        "value": observed,
        "evidence_class": "OBSERVATION",
        "source_ref": "operator_cli_declared",
        "lineage_id": "manual_session_input",
    })
    return world.snapshot()


def _show_history(dialogue: DirectorDialogue, thread: str, out: TextIO) -> None:
    turns = dialogue.history(thread, limit=256)
    if not turns:
        print("(No turns yet.)", file=out)
    for turn in turns:
        print(f"[{turn['seq']}] {turn['role'].lower()}: {turn['content']}", file=out)


def run_chat(argv: Sequence[str] | None = None, *, stdin: TextIO | None = None,
             stdout: TextIO | None = None) -> int:
    """Run a foreground REPL. Returns 0 on user quit/EOF, 2 on setup error.

    Standard input/output are injectable for testing without launching a
    process or creating a network connection. Commands never invoke a shell.
    """
    stream_in = sys.stdin if stdin is None else stdin
    stream_out = sys.stdout if stdout is None else stdout
    args = _parser().parse_args(argv)
    try:
        requested_goal = _declared_goal(args)
        if not args.database.parent.is_dir():
            raise ValueError("database parent directory must already exist")
        if args.coordinator_db is not None and args.database.resolve(strict=False) == args.coordinator_db.resolve(strict=False):
            raise ValueError("dialogue and coordinator must use separate SQLite files")
        coordinator_goal = None
        if args.coordinator_db is not None:
            coordinator_goal, _snapshot, _options = _coordinator_snapshot(args.coordinator_db)
            if requested_goal is not None and requested_goal != coordinator_goal:
                raise ValueError("declared goal conflicts with coordinator objective")
        with DirectorDialogue(args.database, LocalReasoner()) as dialogue:
            try:
                dialogue.history(args.thread, limit=1)
            except KeyError:
                selected_goal = coordinator_goal if coordinator_goal is not None else requested_goal
                if selected_goal is None:
                    raise ValueError("new thread requires --goal-id, --objective, --metric, --target, --direction")
                # Check the selected world source before writing a new thread.
                world = _snapshot if args.coordinator_db is not None else _world(args, selected_goal["metric"])
                dialogue.configure_thread(args.thread, selected_goal)
                print(f"Created thread {args.thread}.", file=stream_out)
            else:
                selected_goal = _read_goal(args.database, args.thread)
                if coordinator_goal is not None and selected_goal != coordinator_goal:
                    raise ValueError("existing dialogue goal differs from coordinator goal")
                if requested_goal is not None:
                    # Idempotent exact-goal check, never silently rewrite.
                    dialogue.configure_thread(args.thread, requested_goal)
                world = _snapshot if args.coordinator_db is not None else _world(args, selected_goal["metric"])
                print(f"Resumed thread {args.thread}.", file=stream_out)
            goal = _read_goal(args.database, args.thread)

            def current_context() -> tuple[dict[str, object], list[dict[str, object]]]:
                if args.coordinator_db is not None:
                    live_goal, live_world, live_options = _coordinator_snapshot(args.coordinator_db)
                    if live_goal != goal:
                        raise ValueError("coordinator objective changed; dialogue requires a new thread")
                    return live_world, live_options
                return world, []
            print("The Director: local symbolic reasoning only; declared evidence is unverified.", file=stream_out)
            print("Commands: /history, /pause, /resume, /quit, /help.", file=stream_out)

            while True:
                print("director> ", end="", flush=True, file=stream_out)
                line = stream_in.readline()
                if line == "":
                    print("Conversation closed (EOF).", file=stream_out)
                    return 0
                question = line.strip()
                if not question:
                    continue
                command = question.lower()
                if command in ("/quit", "/exit"):
                    print("Conversation closed.", file=stream_out)
                    return 0
                if command == "/help":
                    print("Ask a question, or use /history, /pause, /resume, /quit.", file=stream_out)
                    continue
                if command == "/history":
                    _show_history(dialogue, args.thread, stream_out)
                    continue
                if command == "/pause":
                    dialogue.set_paused(args.thread, True)
                    print("Dialogue paused. /resume to continue.", file=stream_out)
                    continue
                if command == "/resume":
                    dialogue.set_paused(args.thread, False)
                    print("Dialogue resumed.", file=stream_out)
                    try:
                        snapshot, options = current_context()
                        result = dialogue.resume(args.thread, world_snapshot=snapshot,
                                                 alternatives=options)
                    except ValueError as exc:
                        if "no pending question" not in str(exc):
                            print(f"Resume error: {exc}", file=stream_out)
                        continue
                    except Exception as exc:
                        print(f"Resume error: {type(exc).__name__}: {exc}", file=stream_out)
                        continue
                    print("Director: " + result["reply"]["content"], file=stream_out)
                    continue
                if question.startswith("/"):
                    print("Unknown command. Use /help.", file=stream_out)
                    continue
                try:
                    snapshot, options = current_context()
                    result = dialogue.ask(args.thread, question, world_snapshot=snapshot,
                                          alternatives=options)
                except (ValueError, RuntimeError) as exc:
                    print(f"Not answered: {exc}", file=stream_out)
                    continue
                print("Director: " + result["reply"]["content"], file=stream_out)
    except (KeyError, ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"Director setup error: {exc}", file=stream_out)
        return 2


def main() -> int:
    return run_chat()


if __name__ == "__main__":
    raise SystemExit(main())
