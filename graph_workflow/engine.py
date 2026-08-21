"""The engine: deterministic interpretation of a declared topology.

Scheduling proceeds by rank. All run state lives here, mutated only by the
scheduler thread; worker threads execute node bodies and enqueue results.
When a node commits, the runtime updates the trace, re-evaluates every gating
expression that references the committing node — in reverse-rank order — and
updates the ready set. The next node to start running is always the
lowest-rank ready node that no governing semaphore disallows. The run is over
when no node is ready and no node is running.
"""

from __future__ import annotations

import copy
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import runners
from .errors import TraceReplayError
from .expressions import DrawState, Evaluator, dependency_map
from .model import GENERATOR_ID, UNDEFINED, Graph, Node
from .monitor import Monitor
from .providers import get_adapter, kill_tree
from .trace import TraceWriter, json_restore, read_events
from .validation import validate

DEFAULT_PAUSE_MESSAGE = "Halt for now. Do not call any new tools until I say 'continue'"
DEFAULT_RESUME_MESSAGE = "continue"


@dataclass
class NodeState:
    """All mutable runtime state in the system lives on nodes."""

    decl: Node
    state: str = "idle"  # idle | ready | running | paused
    output_state: Any = UNDEFINED
    outputs: dict = field(default_factory=dict)
    invocations: int = 0
    commits: int = 0
    last_commit_seq: int = -1
    metrics: dict = field(default_factory=dict)
    captured_inputs: dict = field(default_factory=dict)
    open_invocation: int = 0
    interrupt: bool = False
    paused_open: bool = False
    #: Set by crash recovery: this node was mid-invocation when the process
    #: died and must re-run regardless of later gate demotions.
    rerun_pending: bool = False
    #: What the model said when it ended its turn under Pause; Resume mines
    #: this for declared outputs the continue-reply doesn't restate.
    pause_text: str | None = None
    session_id: str | None = None
    proc: Any = None  # LiveInvocation (LLM) or Popen (human shell)


@dataclass
class RunResult:
    run_id: str
    run_dir: Path
    trace_path: Path
    seed: int
    reason: str
    states: dict[str, NodeState]

    def output_state(self, node_id: str):
        return self.states[node_id].output_state

    def outputs(self, node_id: str) -> dict:
        return dict(self.states[node_id].outputs)


class Runtime:
    """One run of a graph definition. Construct, then call ``run()`` (blocks
    until the run-over condition or user intervention). ``pause()``,
    ``resume()``, and ``stop()`` may be called from other threads; external
    processes write ``control.json`` in the run directory instead."""

    def __init__(
        self,
        graph: Graph,
        seed: int | None = None,
        namespace: dict | None = None,
        run_dir=None,
        validate_graph: bool = True,
        console: bool = True,
        max_commits: int | None = None,
        _resume_events: list[dict] | None = None,
        _run_id: str | None = None,
    ):
        self.graph = graph
        if namespace is None:
            # The graph definition's own namespace: default to the caller's
            # module globals, matching run()/resume_run().
            import sys as _sys

            namespace = _sys._getframe(1).f_globals
        self.namespace = namespace
        if validate_graph:
            warnings = validate(graph, self.namespace)
        else:
            warnings = []
        if seed is None:
            import secrets

            seed = secrets.randbits(63)
        self.seed = int(seed)
        self.max_commits = max_commits

        if _run_id is not None:
            self.run_id = _run_id
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.run_id = f"{_slug(graph.name)}-{stamp}-s{self.seed % 100000}"
        if run_dir is None:
            base = Path(self.namespace.get("__file__", ".")).resolve().parent
            run_dir = base / "runs" / self.run_id
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = self.run_dir / "trace.jsonl"

        self.states: dict[str, NodeState] = {
            nid: NodeState(decl=n) for nid, n in graph.nodes.items()
        }
        self._gen_state = False
        self.draws = DrawState(self.seed)
        for edge in graph.edges.values():
            for gate in edge.all_gates():
                self.draws.register(gate)
        self.evaluator = Evaluator(
            graph, self.namespace, self.states, self.draws,
            gen_state=lambda: self._gen_state, warn=self._warn,
        )
        self.deps = dependency_map(graph)

        self._sem_free = {
            "script": graph.semaphores.script,
            "llm": graph.semaphores.llm,
            "human": graph.semaphores.human,
        }
        self._prov_free = dict(graph.semaphores.providers)

        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._proc_lock = threading.Lock()
        self._pausing = False
        self._paused = False
        self._stop_reason: str | None = None
        self._ended = False
        self._total_commits = 0
        self._startup_warnings = warnings

        if _resume_events is not None:
            self._replay(_resume_events)
            self.trace = TraceWriter(
                self.trace_path, append=True, next_seq=_resume_events[-1]["seq"] + 1
            )
            self._resumed_from_disk = True
        else:
            self.trace = TraceWriter(self.trace_path)
            self._resumed_from_disk = False
        self.monitor = Monitor(self.run_dir, console=console)

    # ------------------------------------------------------------------ #
    # Public control surface
    # ------------------------------------------------------------------ #

    def pause(self):
        self._queue.put(("control", "pause"))

    def resume(self):
        self._queue.put(("control", "resume"))

    def stop(self):
        self._queue.put(("control", "stop"))

    # ------------------------------------------------------------------ #
    # The run
    # ------------------------------------------------------------------ #

    def run(self) -> RunResult:
        if self._ended:
            raise RuntimeError("a Runtime runs once; construct a new one")
        watcher = threading.Thread(target=self._watch_control, daemon=True)
        watcher.start()

        if self._resumed_from_disk:
            self._emit("resumed_from_disk", run_id=self.run_id, seed=self.seed)
        else:
            self._emit(
                "run_start",
                run_id=self.run_id,
                seed=self.seed,
                graph=self.graph.to_json(),
            )
        for w in self._startup_warnings:
            self._warn(w)

        if not self._gen_state:
            # The Generator's flip to TRUE is the run's first event; the
            # re-evaluation it triggers — of every gating expression that
            # references the Generator — seeds the initial ready set.
            self._gen_state = True
            self._emit("generator_fire")
            self._reeval(self.deps.get(GENERATOR_ID, set()), cause="generator")
        elif self._resumed_from_disk and getattr(self, "_recovery_reeval", None):
            self._reeval(self._recovery_reeval, cause="recovery")
        self._after_event()

        while not self._ended:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            except KeyboardInterrupt:
                self._do_stop("keyboard_interrupt")
                break
            kind = item[0]
            if kind == "done":
                _, nid, outputs, metrics, error = item
                if self._ended or self.states[nid].state != "running":
                    continue  # stale completion after stop
                self._commit(nid, outputs, metrics, error)
            elif kind == "interrupted":
                _, nid, metrics, ack_text, ack_error, steered = item
                self._handle_interrupted(nid, metrics, ack_text, ack_error, steered)
            elif kind == "warn":
                self._warn(item[1])
            elif kind == "control":
                cmd = item[1]
                if cmd == "pause":
                    self._do_pause()
                elif cmd == "resume":
                    self._do_resume()
                elif cmd == "stop":
                    self._do_stop("stopped")
            self._after_event()

        self.trace.close()
        return RunResult(
            run_id=self.run_id,
            run_dir=self.run_dir,
            trace_path=self.trace_path,
            seed=self.seed,
            reason=self._stop_reason or "quiescent",
            states=self.states,
        )

    # ------------------------------------------------------------------ #
    # Event plumbing
    # ------------------------------------------------------------------ #

    def _emit(self, event: str, **payload) -> int:
        seq = self.trace.write(event, **payload)
        record = {"seq": seq, "event": event, **payload}
        self.monitor.emit(record, snapshot=self._snapshot())
        return seq

    def _warn(self, message: str):
        self._emit("warning", message=message)

    def _after_event(self):
        if self._ended:
            return
        running = [ns for ns in self.states.values() if ns.state == "running"]
        if self._pausing and not running:
            self._pausing = False
            self._paused = True
            self._emit("paused")
        if not self._pausing and not self._paused:
            self._start_loop()
            busy = any(
                ns.state == "running" or ns.paused_open for ns in self.states.values()
            )
            ready = any(ns.state == "ready" for ns in self.states.values())
            if not busy and not ready:
                self._finish("quiescent")

    def _finish(self, reason: str):
        if not self._ended:
            self._stop_reason = reason
            self._ended = True
            self._emit("run_end", reason=reason)

    # ------------------------------------------------------------------ #
    # Re-evaluation, readiness, capture
    # ------------------------------------------------------------------ #

    def _reeval(self, node_ids: set[str], cause: str):
        """Re-evaluate gating expressions in reverse-rank order (highest rank
        first) and update the ready set accordingly."""
        order = sorted(
            (self.states[nid] for nid in node_ids), key=lambda s: -s.decl.rank
        )
        for ns in order:
            value, terms = self.evaluator.node_gate(ns.decl)
            self._emit("reeval", node=ns.decl.id, value=value, terms=terms, cause=cause)
            if ns.state == "idle" and value:
                ns.state = "ready"
                ns.captured_inputs = self._capture_inputs(ns.decl, terms)
                self._emit("ready", node=ns.decl.id, inputs=ns.captured_inputs)
            elif ns.state == "ready" and not value and not ns.rerun_pending:
                # rerun_pending nodes were already running when the process
                # died; an uninterrupted run would have completed that
                # invocation, so a later gate flip must not cancel the re-run.
                ns.state = "idle"
                ns.captured_inputs = {}
                self._emit("demoted", node=ns.decl.id)

    def _capture_inputs(self, node: Node, terms: dict[str, bool]) -> dict:
        """Snapshot the node's declared inputs from upstream declared outputs,
        across in-edges whose term is TRUE at this evaluation (a FALSE gate
        transfers nothing). Outputs and inputs pair by name; when several
        upstream nodes supply the same name, the latest commit wins."""
        captured: dict[str, Any] = {}
        provenance: dict[str, int] = {}
        for edge in self.graph.in_edges(node.id):
            if not terms.get(edge.id):
                continue
            for nid in edge.inputs:
                if nid == GENERATOR_ID:
                    continue
                src = self.states[nid]
                for name in node.inputs:
                    if name in src.decl.outputs and name in src.outputs:
                        if provenance.get(name, -2) < src.last_commit_seq:
                            provenance[name] = src.last_commit_seq
                            value = src.outputs[name]
                            try:
                                value = copy.deepcopy(value)
                            except Exception:
                                pass
                            captured[name] = value
        return captured

    def _redraw_for(self, committed_id: str):
        """Draws are redrawn at every commit of a gate's input nodes."""
        for edge in self.graph.out_edges(committed_id):
            for gate in edge.all_gates():
                index, value = self.draws.redraw(gate)
                if not gate.is_default:
                    self._emit("draw", gate=gate.gate_id, index=index, value=value)

    # ------------------------------------------------------------------ #
    # Starting nodes
    # ------------------------------------------------------------------ #

    def _sem_keys(self, node: Node) -> list[tuple[str, str]]:
        keys = [("type", node.type)]
        if node.type == "llm" and node.provider in self._prov_free:
            keys.append(("provider", node.provider))
        return keys

    def _sems_available(self, node: Node) -> bool:
        for kind, key in self._sem_keys(node):
            pool = self._sem_free if kind == "type" else self._prov_free
            if pool[key] < 1:
                return False
        return True

    def _sems_acquire(self, node: Node):
        for kind, key in self._sem_keys(node):
            pool = self._sem_free if kind == "type" else self._prov_free
            pool[key] -= 1

    def _sems_release(self, node: Node):
        for kind, key in self._sem_keys(node):
            pool = self._sem_free if kind == "type" else self._prov_free
            pool[key] += 1

    def _start_loop(self):
        """Start the lowest-rank startable node, rescan, repeat."""
        while True:
            candidates = sorted(
                (
                    ns
                    for ns in self.states.values()
                    if ns.state == "ready" or (ns.paused_open and ns.state == "paused")
                ),
                key=lambda s: s.decl.rank,
            )
            started = False
            for ns in candidates:
                if self._sems_available(ns.decl):
                    self._start(ns)
                    started = True
                    break
            if not started:
                return

    def _start(self, ns: NodeState):
        node = ns.decl
        self._sems_acquire(node)
        resumed = ns.paused_open
        ns.interrupt = False
        ns.rerun_pending = False
        ns.state = "running"
        if resumed:
            invocation = ns.open_invocation
        else:
            ns.invocations += 1
            invocation = ns.invocations
            ns.open_invocation = invocation
            if node.type == "llm":
                # Providers that assign their own session id (codex) get it
                # parsed from output; fabricating one here would poison the
                # pause/resume path if the invocation is interrupted early.
                adapter = get_adapter(node.provider)
                ns.session_id = (
                    None
                    if getattr(adapter, "provider_assigns_session", False)
                    else runners.session_uuid(self.run_id, node.id, invocation)
                )
        self._emit("start", node=node.id, invocation=invocation, resumed=resumed)
        threading.Thread(
            target=self._worker, args=(ns, resumed), daemon=True,
            name=f"gw-{node.id}#{invocation}",
        ).start()

    # ------------------------------------------------------------------ #
    # Workers (the only code that runs off the scheduler thread)
    # ------------------------------------------------------------------ #

    def _worker(self, ns: NodeState, resumed: bool):
        node = ns.decl
        warn = lambda m: self._queue.put(("warn", m))  # noqa: E731
        inputs = runners.Inputs(node.inputs, ns.captured_inputs)

        def register_proc(proc):
            with self._proc_lock:
                ns.proc = proc

        try:
            if node.type == "script":
                outputs, metrics, error = runners.run_script(node, inputs, warn)
            elif node.type == "human":
                outputs, metrics, error = runners.run_human(
                    node, inputs, run_id=self.run_id, run_dir=self.run_dir,
                    invocation=ns.open_invocation, register_proc=register_proc,
                    warn=warn,
                )
            elif node.type == "llm":
                status, outputs, metrics, error, ack_text = runners.run_llm(
                    node, inputs, run_id=self.run_id, run_dir=self.run_dir,
                    invocation=ns.open_invocation, register_proc=register_proc,
                    interrupted=lambda: ns.interrupt, warn=warn,
                    resume_session=ns.session_id if resumed else None,
                    resume_message=(
                        self._resume_message(node) if resumed else None
                    ),
                    pause_text=ns.pause_text if resumed else None,
                )
                if status == "interrupted_acked":
                    # The halt message was steered into the live session; the
                    # model ended its following turn and the session is done.
                    self._queue.put(
                        ("interrupted", node.id, metrics, ack_text, None, True)
                    )
                    return
                if status == "interrupted":
                    # Steering unavailable (or the write missed): the session
                    # was interrupted; deliver the halt message via the
                    # session-bound resume form instead.
                    sid = metrics.get("session_id") or ns.session_id
                    if sid:
                        ack_text, ack_error = runners.send_llm_message(
                            node, sid, self._pause_message(node),
                            run_dir=self.run_dir, invocation=ns.open_invocation,
                            tag="-pause", register_proc=register_proc,
                        )
                    else:
                        ack_text, ack_error = None, "no session id retained"
                    metrics["session_id"] = sid
                    self._queue.put(
                        ("interrupted", node.id, metrics, ack_text, ack_error, False)
                    )
                    return
            else:  # pragma: no cover
                outputs, metrics, error = {}, {}, f"unknown node type {node.type!r}"
        except Exception as exc:  # defensive: runner bugs must not hang the run
            outputs, metrics, error = {}, {}, f"runner crashed: {type(exc).__name__}: {exc}"
        self._queue.put(("done", node.id, outputs, metrics, error))

    def _pause_message(self, node) -> str:
        return node.pause_message or self.graph.pause_message or DEFAULT_PAUSE_MESSAGE

    def _resume_message(self, node) -> str:
        return node.resume_message or self.graph.resume_message or DEFAULT_RESUME_MESSAGE

    # ------------------------------------------------------------------ #
    # Commit — the heart of the schedule
    # ------------------------------------------------------------------ #

    def _commit(self, nid: str, outputs: dict, metrics: dict, error: str | None):
        ns = self.states[nid]
        node = ns.decl
        self._sems_release(node)
        with self._proc_lock:
            ns.proc = None
        ns.state = "idle"
        ns.paused_open = False
        ns.outputs = dict(outputs)
        if metrics.get("session_id"):
            ns.session_id = metrics["session_id"]
        ns.metrics.update(metrics)
        # The output expression evaluates; that evaluation is the commit and
        # the resulting boolean is the node's output state.
        ns.output_state = self.evaluator.output_state_value(node, ns.outputs)
        ns.commits += 1
        seq = self._emit(
            "commit",
            node=nid,
            invocation=ns.open_invocation,
            outputs=ns.outputs,
            output_state=ns.output_state,
            metrics=ns.metrics,
            error=error,
        )
        ns.last_commit_seq = seq
        self._total_commits += 1
        self._redraw_for(nid)
        self._reeval(self.deps.get(nid, set()), cause=f"commit:{nid}#{ns.open_invocation}")
        if self.max_commits is not None and self._total_commits >= self.max_commits:
            self._do_stop("commit_limit")

    # ------------------------------------------------------------------ #
    # Pause / Resume / Stop
    # ------------------------------------------------------------------ #

    def _do_pause(self):
        if self._pausing or self._paused or self._ended:
            return
        self._pausing = True
        self._emit("pause_requested")
        # Send the halt message to each running LLM node. Where the provider
        # exposes a steering channel (claude's stream-json stdin), the
        # message is written into the live session and reaches the agent
        # after its current tool call — no interrupt; the model ends its
        # following turn and the session terminates. Where no channel exists
        # (codex exec, headless grok), fall back to interrupting the turn
        # process — the session itself survives and receives the halt via
        # the session-bound resume form. Script and human invocations run to
        # completion either way.
        for ns in self.states.values():
            if ns.state == "running" and ns.decl.type == "llm":
                ns.interrupt = True
                with self._proc_lock:
                    handle = ns.proc
                if handle is None:
                    continue
                if not handle.steer(self._pause_message(ns.decl)):
                    handle.kill()

    def _handle_interrupted(self, nid: str, metrics: dict, ack_text, ack_error,
                            steered: bool):
        ns = self.states[nid]
        self._sems_release(ns.decl)
        with self._proc_lock:
            ns.proc = None
        ns.state = "paused"
        ns.paused_open = True
        ns.pause_text = ack_text or None
        if metrics.get("session_id"):
            ns.session_id = metrics["session_id"]
        ns.metrics.update(metrics)
        self._emit("node_interrupted", node=nid, invocation=ns.open_invocation,
                   session_id=ns.session_id, steered=steered)
        if ack_error:
            self._warn(f"pause message to {nid} failed: {ack_error}")
        else:
            self._emit("node_pause_acked", node=nid, steered=steered,
                       ack=(ack_text or "")[:2000])

    def _do_resume(self):
        if self._ended or not (self._paused or self._pausing):
            return
        self._pausing = False
        self._paused = False
        self._emit("resume_requested")
        self._emit("resumed")
        # paused-open nodes re-enter the schedule via the start loop, which
        # sends each session its resume message.

    def _do_stop(self, reason: str):
        if self._ended:
            return
        for ns in self.states.values():
            ns.interrupt = True
            with self._proc_lock:
                proc = ns.proc
            if proc is None:
                continue
            if hasattr(proc, "plan"):  # LiveInvocation
                proc.kill()
            elif proc.poll() is None:  # raw Popen (human shell)
                kill_tree(proc.pid)
        self._finish(reason)

    # ------------------------------------------------------------------ #
    # Control file
    # ------------------------------------------------------------------ #

    def _watch_control(self):
        import json as _json

        path = self.run_dir / "control.json"

        def mtime():
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return None

        # A control file already present at startup (e.g. left over before a
        # resume) is stale: only writes observed during this run count.
        last = mtime()
        while not self._ended:
            time.sleep(0.25)
            current = mtime()
            if current is not None and current != last:
                last = current
                try:
                    cmd = _json.loads(path.read_text(encoding="utf-8")).get("command")
                    if cmd in ("pause", "resume", "stop"):
                        self._queue.put(("control", cmd))
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    # Status snapshot (GUI interchange)
    # ------------------------------------------------------------------ #

    def _snapshot(self) -> dict:
        if self._ended:
            run_state = f"ended:{self._stop_reason or 'quiescent'}"
        elif self._paused:
            run_state = "paused"
        elif self._pausing:
            run_state = "pausing"
        else:
            run_state = "running"
        nodes = {}
        for nid, ns in self.states.items():
            nodes[nid] = {
                "state": ns.state,
                "output_state": (
                    "UNSET" if ns.output_state is UNDEFINED else bool(ns.output_state)
                ),
                "invocations": ns.invocations,
                "commits": ns.commits,
                "metrics": {
                    k: v for k, v in ns.metrics.items() if k != "usage"
                },
                "outputs": {k: _preview(v) for k, v in ns.outputs.items()},
            }
        return {
            "run_id": self.run_id,
            "graph": self.graph.name,
            "seed": self.seed,
            "state": run_state,
            "generator": self._gen_state,
            "next_seq": self.trace.next_seq,
            "nodes": nodes,
        }

    # ------------------------------------------------------------------ #
    # Replay (fault tolerance / resume-from-disk)
    # ------------------------------------------------------------------ #

    def _replay(self, events: list[dict]):
        if events[0].get("event") != "run_start":
            raise TraceReplayError("trace does not begin with run_start")
        recorded_draws: dict[str, tuple[int, float]] = {}
        self._recovery_reeval: set[str] = set()
        for ev in events:
            etype = ev.get("event")
            nid = ev.get("node")
            if etype == "run_end":
                raise TraceReplayError(f"run already ended ({ev.get('reason')})")
            if etype == "generator_fire":
                self._gen_state = True
                # If the trace tore before any ready event persisted, the
                # recovery re-evaluation re-seeds the initial ready set.
                self._recovery_reeval = set(self.deps.get(GENERATOR_ID, set()))
            elif etype == "ready":
                ns = self.states[nid]
                ns.state = "ready"
                ns.captured_inputs = json_restore(ev.get("inputs") or {})
            elif etype == "demoted":
                ns = self.states[nid]
                ns.state = "idle"
                ns.captured_inputs = {}
            elif etype == "start":
                ns = self.states[nid]
                ns.state = "running"
                ns.invocations = max(ns.invocations, int(ev["invocation"]))
                ns.open_invocation = int(ev["invocation"])
            elif etype == "commit":
                ns = self.states[nid]
                ns.state = "idle"
                ns.paused_open = False
                ns.outputs = json_restore(ev.get("outputs") or {})
                restored = ev.get("output_state")
                ns.output_state = UNDEFINED if isinstance(restored, dict) else restored
                ns.commits += 1
                ns.last_commit_seq = ev["seq"]
                ns.metrics.update(json_restore(ev.get("metrics") or {}))
                if ns.metrics.get("session_id"):
                    ns.session_id = ns.metrics["session_id"]
                for edge in self.graph.out_edges(nid):
                    for gate in edge.all_gates():
                        self.draws.redraw(gate)
                # A crash can tear the commit→re-evaluation pipeline: the
                # commit record lands but its dependent ready/demoted events
                # don't. Re-running the last commit's re-evaluation on resume
                # is idempotent when the pipeline was written whole.
                self._recovery_reeval = set(self.deps.get(nid, set()))
            elif etype == "draw":
                recorded_draws[ev["gate"]] = (int(ev["index"]), float(ev["value"]))
            elif etype == "node_interrupted":
                ns = self.states[nid]
                ns.state = "paused"
                ns.paused_open = True
                if ev.get("session_id"):
                    ns.session_id = ev["session_id"]
            elif etype == "paused":
                self._paused = True
                self._pausing = False
            elif etype == "pause_requested":
                self._pausing = True
            elif etype in ("resumed", "resume_requested"):
                self._paused = False
                self._pausing = False
        # Verify the replayed streams reproduce the recorded draws — a
        # mismatch means the graph or seed changed since the trace was cut.
        for gate_id, (index, value) in recorded_draws.items():
            if gate_id not in self.draws.index:
                raise TraceReplayError(f"trace references unknown gate {gate_id!r}")
            if self.draws.index[gate_id] < index:
                raise TraceReplayError(
                    f"gate {gate_id!r}: trace records draw #{index} but replay "
                    f"reached #{self.draws.index[gate_id]}"
                )
            if self.draws.index[gate_id] == index and abs(
                self.draws.value[gate_id] - value
            ) > 1e-12:
                raise TraceReplayError(
                    f"gate {gate_id!r}: replayed draw differs from trace — "
                    "graph definition or seed changed since this trace was cut"
                )
        # Invocations that were running when the process died are re-run:
        # back to ready with their captured inputs, immune to demotion.
        for ns in self.states.values():
            if ns.state == "running":
                ns.state = "ready"
                ns.rerun_pending = True
        if self._pausing and not any(
            ns.state == "running" for ns in self.states.values()
        ):
            self._pausing = False
            self._paused = True


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name)[:40] or "graph"


def _preview(value, limit: int = 800):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"… [{len(value)} chars]"
    if value is UNDEFINED:
        return "UNDEFINED"
    return value


# ---------------------------------------------------------------------- #
# Entry points
# ---------------------------------------------------------------------- #


def run(
    graph: Graph,
    seed: int | None = None,
    namespace: dict | None = None,
    run_dir=None,
    validate_graph: bool = True,
    console: bool = True,
    max_commits: int | None = None,
) -> RunResult:
    """Start a run of the graph. Called from the graph definition file —
    executing the file starts the run. The evaluation namespace defaults to
    the caller's module globals (the graph definition's own namespace)."""
    if namespace is None:
        import sys

        namespace = sys._getframe(1).f_globals
    rt = Runtime(
        graph,
        seed=seed,
        namespace=namespace,
        run_dir=run_dir,
        validate_graph=validate_graph,
        console=console,
        max_commits=max_commits,
    )
    return rt.run()


def resume_run(
    graph: Graph,
    trace_path,
    namespace: dict | None = None,
    console: bool = True,
    max_commits: int | None = None,
) -> RunResult:
    """Resume a run from its disk trace (crash recovery or a paused run in a
    dead process). The graph definition and seed must be unchanged; replayed
    draw streams are verified against the recorded ones.

    Caveat: outputs are restored from their JSON trace form — values that
    were not JSON-serializable come back as repr strings."""
    if namespace is None:
        import sys

        namespace = sys._getframe(1).f_globals
    events = read_events(trace_path)
    head = events[0]
    if head.get("event") != "run_start":
        raise TraceReplayError("trace does not begin with run_start")
    rt = Runtime(
        graph,
        seed=head.get("seed"),
        namespace=namespace,
        run_dir=Path(trace_path).parent,
        validate_graph=True,
        console=console,
        max_commits=max_commits,
        _resume_events=events,
        _run_id=head.get("run_id"),
    )
    return rt.run()
