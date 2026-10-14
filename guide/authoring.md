# Composing graph-workflow definitions

Addressed to the model writing a definition. Everything here is stated once; derive consequences from the mechanisms rather than expecting enumerated cases. When this file and the source disagree, the source wins — the runtime is small and readable: `graph_workflow/model.py` (declarations), `engine.py` (scheduling), `expressions.py` (gates), `runners.py` (node execution), `validation.py` (rejections), `providers/` (the CLIs). The definition at the end of this file illustrates the core constructs and was verified by execution.

## Where your priors mislead

This runtime resembles DAG/pipeline systems just enough to import wrong assumptions:

- Cycles are legal and ordinary. A back-edge plus a gate is the loop construct; nodes re-fire, and `invocations` counts.
- Edges gate; they do not carry data. An edge contributes one boolean term to each destination's gating expression; data transfer is a side effect of a TRUE term at arming, paired by name.
- Everything is level-triggered. Terms are pure functions of current output states — not events, not queues — re-evaluated only when a node they depend on commits (dependencies are defined under Execution).
- Rank is a scheduling priority, not topology.
- Failure is a value. Exceptions and timeouts are recorded on the commit; missing required outputs make the default output expression FALSE. Nothing raises across the graph; the run continues to quiescence.
- Gating expressions are ordinary Python strings; there is no expression DSL and no gate function library.
- LLM nodes are headless CLI sessions (`claude`, `codex`, `grok`, `openrouter` through `dsh`, plus `claudeb`/`codexb` — the same CLIs on a second login) with permissions bypassed.

## Shape

A definition is one executable Python file: topology declared as data, a run of itself started under the `__main__` guard.

```python
from graph_workflow import Edge, Graph, ScriptNode, Semaphores, run

g = Graph("my-graph", semaphores=Semaphores(script=4, llm=2, human=1))

A = ScriptNode("A", rank=0, fn=lambda i: {"x": 21}, outputs={"x": int})
B = ScriptNode("B", rank=1, fn=lambda i: {"y": i.x * 2},
               inputs={"x": int}, outputs={"y": int})
g.add_nodes(A, B)
g.add_edges(
    Edge("gen_A", inputs=[g.generator], outputs=[A]),
    Edge("e_AB", inputs=[A], outputs=[B]),
)

if __name__ == "__main__":
    result = run(g)
    print("REASON:", result.reason)
```

The guard is required — the validator and the Monitor GUI import definitions with `run()` intercepted — so module level must stay free of side effects beyond declarations. `Graph(..., run_dir=...)` is the graph's declaration of where its runs live: an absolute path, taken as given, never inside a cloud-synced folder. Omit it, as here, and the runtime creates a hashed folder in `%TEMP%`. `seed=` (run seed; omitted = random per run) and `monitor=` (`True`/`False`/exe path; omitted = launch the Monitor GUI only when installed and the console is interactive) are declared the same way.

## Execution

1. At run start the Generator (`g.generator`, id `GEN` — not a node: no logic, no data, nothing may be upstream of it) flips its output state TRUE. Gates referencing it re-evaluate; that seeds the ready set.
2. A node whose gate evaluates TRUE while idle becomes ready and captures its inputs: for each declared input name, a deep-copied value from the same-named output of the latest-committed supplier, across in-edges whose term was TRUE at that evaluation. The snapshot stays frozen while the node waits. Validation guarantees only that a supplier is *possible*, not that one has committed.
3. The scheduler starts the lowest-rank ready node whose semaphores have free slots. Semaphores queue ready nodes; they never cancel them.
4. When an invocation ends, the node's output expression is evaluated over its declared outputs. That evaluation is the commit; its boolean is the node's output state — the value every downstream term reads.
5. After a commit, dependent nodes re-evaluate their gates (stochastic gates on the committing node's out-edges redraw first). Every incoming supplier is a dependency, as is each node ID or `GEN` appearing as a Python name in the node gate or applicable in-edge gates. Explicit references are found by syntax scan (`ast.Name`), not by tracking reads during evaluation. Ready nodes whose gates went FALSE are demoted to idle.
6. The run ends when nothing is ready, running, or paused mid-invocation: `reason: quiescent` — the clean ending even when branches never fired. Other reasons: `stopped`, `commit_limit`, `keyboard_interrupt`.

Two consequences of level-triggering are worth spelling out. A gate over anything but node state — the clock, a file, a mutated global — is legal Python but is never re-checked between commits; encode such conditions as node outputs. And a cycle's gate needs an arm-once first term plus, on the re-arm term, both an alternation guard and a bound:

```python
gate=("(e_seed and D.invocations == 0)"
      " or (e_back and C.invocations == D.invocations"
      " and D.invocations < MAX_ROUNDS)")
```

Without the alternation guard ("my peer has answered my latest commit") the still-TRUE back-edge term re-arms D off its own commit. `max_commits=` in `run()` is the backstop.

Gating decisions and draws are functions of seed and commit history alone, but wall-clock completion order of concurrently running nodes varies; `Semaphores(script=1, ...)` when completion order must be deterministic.

## Declarations

```python
Graph(name, semaphores=None, pause_message=None, resume_message=None,
      run_dir=None, seed=None, monitor=None)
Semaphores(script=4, llm=2, human=1, providers=None)
```

`providers` (e.g. `{"claude": 1, "codex": 1}`) subdivides the llm pool — an LLM node needs a free slot in both, and llm slots are concurrent CLI sessions, i.e. real spend. Each login is its own key here: `{"claude": 1, "claudeb": 1}` is two concurrent sessions on two accounts. `pause_message`/`resume_message` override what a paused LLM session is told (defaults: `DEFAULT_PAUSE_MESSAGE` / `DEFAULT_RESUME_MESSAGE`; per-node values override the graph's). `add_nodes`/`add_edges` are variadic and reject duplicates.

Common to all nodes:

```python
Node(id, rank, inputs=None, outputs=None, gate=None, output_expression=None)
```

- `id` — a non-keyword Python identifier, never `GEN`, unique across nodes and edges together (they share the expression namespace). Every registered node's ID names its runtime handle in expressions, even when the object is held in a collection or differently named variable. Binding `A = ScriptNode("A", ...)` is a convention; a Python global named `A` is unnecessary for `A.commits` to resolve inside a gate.
- `rank` — unique int; number in flow order. Lowest ready rank starts first.
- `inputs` / `outputs` — `{name: type}`; types are declarative only, not enforced. Choose output names globally before drawing any edge — names are the data plane, and when two suppliers share a name, latest commit wins. Output names may not collide with the handle metrics listed under Expressions.
- `gate` — defaults to the AND of all in-edge terms; a node with no in-edges never arms.
- `output_expression` — defaults to `isDefined(...)` ANDed over every declared output.

### ScriptNode

```python
ScriptNode(id, rank, fn, **common)
```

`fn(inputs)` on a worker thread; anything goes — files, network, subprocesses, model calls. `inputs` offers attribute access (`i.x`, declared names only), mapping access (`i["x"]`, `i.get("x", default)`), and declared but unsupplied names read as `UNDEFINED` (falsy). Return a dict over declared output names, the bare value when exactly one output is declared, or `None` for none. Undeclared keys warn and are dropped; an exception ends the invocation with the error recorded and, under the default output expression, a FALSE commit.

### LLMNode

```python
LLMNode(id, rank, provider, model, prompt,
        effort=None, settings=None, cwd=None, extra_args=None,
        timeout_s=None, parse=None,
        pause_message=None, resume_message=None, persist_from=None, **common)
```

One invocation = one headless CLI turn, in a fresh session by default. `persist_from="A"` instead sends this node's rendered prompt into A's latest session, using the provider's resume mechanism (`--resume ID`, or Codex's thread resume). OpenRouter's current DSH CLI has no resume operation: fresh invocations work, but an attempt to resume an OpenRouter session fails as an ordinary node invocation error. The session ID is bound when this invocation starts and recorded in the trace for recovery. No manual UUID wiring is needed:

```python
g = Graph("shared-context")
A = LLMNode("A", rank=0, provider="claude", model="sonnet",
            prompt="Draft a plan.", outputs={"response": str})
B = LLMNode("B", rank=1, provider="claude", model="sonnet",
            persist_from="A", prompt="Review the plan and improve it.",
            outputs={"response": str})
g.add_nodes(A, B)
g.add_edges(Edge("start", g.generator, A), Edge("review", A, B))
```

The source may be several edges upstream, including across script or human nodes. Persistence can chain: C with `persist_from="B"` continues B's session. It shares the live session history; it does not fork a snapshot of A's reply. Each new invocation selects its source's latest session again. A node without `persist_from` still starts fresh on every invocation.

For a loop where each agent keeps its own context, use self-reference: `persist_from="A"` on A and `persist_from="B"` on B. Each starts fresh when it has no session yet, then resumes its own session on later iterations. A directed cycle must lead back to each self-referencing node; a zero-edge path does not count. For example, these gates alternate three turns per agent:

```python
g = Graph("separate-contexts")

def propose(inputs):
    if not isDefined(inputs.response):
        return "Propose an initial plan."
    return f"Revise the plan using this critique:\n{inputs.response}"

A = LLMNode("A", rank=0, provider="claude", model="sonnet",
            persist_from="A", prompt=propose,
            gate="(start or back) and A.invocations == B.commits and A.invocations < 3",
            inputs={"response": str}, outputs={"response": str})
B = LLMNode("B", rank=1, provider="claude", model="sonnet",
            persist_from="B", prompt="Review this revision: $response",
            gate="forward and B.invocations < A.commits",
            inputs={"response": str}, outputs={"response": str})
g.add_nodes(A, B)
g.add_edges(Edge("start", g.generator, A), Edge("forward", A, B),
            Edge("back", B, A))
```

Each agent remembers its own turns; ordinary graph inputs deliver the other agent's latest reply. A's prompt handles the missing input on its first turn.

Validation checks only that the source node exists and a directed path leads from it to this node. It ignores gates, provider/login compatibility, and concurrent session use; cycles are allowed. Authors must arrange for an LLM source to have a session available, use a provider/login that can resume it, and order turns as needed. A missing session on another node means an invocation error; only self-reference bootstraps a fresh context. This declaration adds no edges or scheduling constraints. For providers that support session resume, Pause/Resume completes the same invocation in its bound session with `resume_message`; ordinary persistence uses `prompt`. The Monitor's LLM node dialog exposes the same field as **Persist from**.

The existing `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and `XAI_API_KEY` stripping applies to every provider. Claude, Codex, and Grok must be installed and logged in; OpenRouter uses DSH's credential store as described below. The CLIs run with permissions bypassed — `cwd` (default: the launching process's) is the agent's working directory and its blast radius.

`prompt` is a `string.Template` string — `$name` substitutes the captured input, non-strings JSON-encoded, `$$` a literal `$`, and a name never captured survives as literal `$name` text, silently — or a callable `prompt(inputs) -> str`; use the callable and branch on `isDefined(...)` whenever an input is optional. If `response` is declared it receives the raw reply text; by default, every other declared output is extracted from the last JSON object in the reply (fenced blocks first), so the prompt must end by demanding it — "End your reply with exactly one JSON object, no prose after it, no code fences:" followed by a sketch carrying exactly the declared names. A declared key missing from the reply stays undefined, and the default output expression then commits FALSE. `parse(text, raw) -> dict` replaces the JSON extraction; its keys map onto declared outputs, undeclared keys warn and drop. Bind per-node output names with a parser factory:

```python
def capture(output_name):
    def parse_reply(text, raw):
        return {output_name: text.strip()}
    return parse_reply


parse_a = capture("answer_a")
parse_b = capture("answer_b")
```

The first node uses `parse=parse_a, outputs={"answer_a": str}`; the second uses `parse=parse_b, outputs={"answer_b": str}`. Each factory call binds its own output name, which must match the returned key and declared output. When generating nodes in a loop, call the factory per node; closures reading a changing loop variable would share its final binding.

`effort` maps to the CLI's reasoning-effort control; `settings` to `claude --settings`; `timeout_s` ends the invocation with an error. Never set `timeout_s` on LLM nodes unless the user has directly imposed a literal time limit, because a timeout can kill an invocation just short of completion and waste everything it has spent.

Outputs are parsed from the reply that ends the invocation, and background work the agent leaves running is killed (Claude allows at most ten idle minutes; `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` in the launching environment lifts this). Prompts for long-running work must demand foreground commands within the tool's time limit and no reply before the final JSON.

`extra_args` supplies provider arguments, one list element per argument: normally separate elements for a flag and its value. Keep a value containing spaces in one element, without shell quotes. `["--tools", ""]` supplies an empty value; `["--tools"]` omits it; `["--tools", '""']` supplies literal quote characters. Use the first form for Claude's empty-set recipe below.

GW passes argv directly to `subprocess.Popen` without enabling a shell or adding quoting. Python's `shutil.which` selects the executable; on Windows, this may be a `.cmd` wrapper whose argument parsing GW neither bypasses nor validates. The list contract does not guarantee arbitrary wrapper behavior.

For Claude and Grok, `extra_args` follows GW's generated CLI options and precedes `--session-id ID` or `--resume ID`; no special terminator is needed for the empty Claude value. Codex defaults to `[exe, "app-server", *extra_args]`: use server startup options such as `--disable FEATURE` or `-c key=value`. Model and effort are sent later by RPC; `extra_args` is not an RPC parameter map. The legacy `GW_CODEX_EXEC=1` path takes `codex exec` arguments instead.

OpenRouter invokes native `dsh -p --provider openrouter --model ID [--effort EFFORT]`, followed by `extra_args`, `--session-id ID`, and `-- -`. GW supplies the rendered prompt through UTF-8 stdin and closes it; multiline text, quotes, long prompts, and a literal `-` stay intact. `settings` remains Claude-only.

Tool availability is separate from a prompt instruction to refrain from using tools. GW has no dedicated tool-availability parameter; use provider controls through `extra_args`. GW independently requests permission bypass: Claude `--dangerously-skip-permissions`, Grok `--yolo`, Codex `approvalPolicy="never", sandbox="danger-full-access"` on thread start and resume, and DSH's full-access `-p` default for OpenRouter. These policies do not remove tools.

Claude, Grok, and Codex controls below are supported by installed CLI help and adapter wiring (2026-09-10); Codex's feature listing also confirmed `shell_tool=false`. The OpenRouter row uses DSH's tool controls. Claude's first `system`/`init` line in `sessions/<node>-<inv>.jsonl` lists the tools a session actually had, which is the inventory to trust:

| Provider | `extra_args` | Scope |
|---|---|---|
| `claude` / `claudeb` | `["--tools", ""]` | Disables built-in tools. `["--tools", "Read"]` selects named built-ins. Neither establishes exclusion of MCP tools. |
| | `["--tools", "", "--strict-mcp-config"]` | `tools: []`, `mcp_servers: []` (verified) |
| `grok` | `["--disable-web-search"]` | Disables web search/fetch. `--tools <TOOLS>` and `--disallowed-tools <TOOLS>` select/remove comma-separated built-ins; empty `--tools` has no documented no-tools meaning. |
| `codex` / `codexb` | `["--disable", "shell_tool"]` | Turns off the default shell tool feature in app-server mode; other tools are outside this control. |
| `openrouter` | `["--agent", "reviewer", "--allow-tool", "write", "--deny-tool", "web_fetch"]` | Loads `~/.dsh/agents` by frontmatter name. Repeatable allowances extend its tools and clear matching frontmatter denials; CLI denials win. `--tools-mode native` removes the Code Mode transport. |

No complete no-tools recipe is established here for Grok or Codex. Grok's hyphenated `--disallowed-tools` removes built-ins; `--disallowedTools` is a permission-rule alias.

| provider | CLI | login (config dir) | live steering on Pause |
|---|---|---|---|
| `claude` | `claude -p`, stream-json | `~/.claude` | yes, mid-turn |
| `claudeb` | as `claude` | `~/.claudeb` | as `claude` |
| `codex` | `codex app-server` JSON-RPC | `~/.codex` | yes (`GW_CODEX_EXEC=1` = legacy `codex exec`, no steering) |
| `codexb` | as `codex` | `~/.codexb` | as `codex` |
| `grok` | `grok --prompt-file` | `~/.grok` | no — interrupt, halt delivered on session resume |
| `openrouter` | `dsh -p --provider openrouter` | `~/.dsh` | no; DSH also lacks session resume, so an interrupted turn cannot continue |

`claudeb`/`codexb` are `claude`/`codex` on the CLI's second config dir, i.e. a second login: same CLI, same models and efforts (the `claude` and `codex` rows below), a separate rate limit, and a separate key for `Semaphores(providers=...)`. Treat the choice as picking which account's limit a node spends. The config-dir variables (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GROK_HOME`) are stripped from every node's environment alongside the API keys and set from the provider name alone, so `claude` means the `~/.claude` login even when the definition was launched from a `claudeb` session, and an MCP server registered on one login is reachable only from nodes on that provider. A missing config dir is an error of the same kind as a CLI missing from PATH.

OpenRouter requires `dsh.exe` on Windows (`dsh` elsewhere). Its adapter clears inherited `DSH_HOME` so the normal `~/.dsh` configuration applies. Credentials resolve inside DSH from `~/.dsh/.credentials.yaml`, with `OPENROUTER_API_KEY` honored as DSH's override; GW does not read the key. Stdout is the final assistant text, including for `parse(text, raw)` where `raw` is that stdout string. Nonzero exits retain DSH's stderr diagnostic as the node error. The node's session id names the DSH transcript; `dsh export ID` decodes it. DSH supplies no usage envelope on stdout, so after each invocation GW reads usage back from that transcript: `usage` is `{inputTokens, outputTokens, steps, costUsd}` summed over the session's model steps, and `context_size` is the last step's input + output tokens. If the export fails, both retain their empty/zero defaults. All OpenRouter models use the existing `LLMNode` class.

| Provider | Model | CLI slug | Accepted efforts |
|---|---|---|---|
| `claude` | Haiku | `haiku` | None; omit `effort` |
| `claude` | Sonnet | `sonnet` | `low`, `medium`, `high`, `xhigh`, `max`, `ultracode` |
| `claude` | Opus | `opus` | `low`, `medium`, `high`, `xhigh`, `max`, `ultracode` |
| `claude` | Fable | `fable` | `low`, `medium`, `high`, `xhigh`, `max`, `ultracode` |
| `grok` | Grok 4.6 | None; omit `model` | `low`, `medium`, `high`, `xhigh` |
| `codex` | Luna | `gpt-6-luna` | `low`, `medium`, `high`, `xhigh`, `max` |
| `codex` | Terra | `gpt-5.6-terra` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |
| `codex` | Sol | `gpt-6.1-sol` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |
| `codex` | Astra | `gpt-6-astra` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |
| `openrouter` | Muse Spark 1.3 | `meta/muse-spark-1.3-contributor` (alias `muse-spark-1.3`) | `low`, `medium`, `high`, `xhigh`, `max` |
| `openrouter` | Muse Wonder | `meta/muse-wonder` (alias `muse-wonder`) | `low`, `medium`, `high`, `xhigh`, `max` |

Muse Spark 1.3 and Muse Wonder are the supplied OpenRouter models. The adapter expands the short aliases to the listed ids; other explicit ids pass through to DSH. Use `provider="openrouter"`, not the delegate wrapper's `dsh` provider name:

```python
A = LLMNode("A", rank=0, provider="openrouter", model="muse-spark-1.3",
            effort="high", prompt="Summarize the requested work.",
            outputs={"response": str})
```

The table, not `claude --help`, is the accepted set: `ultracode` is absent from the help text yet accepted by `--effort`, verified by execution, and required whenever a Claude node is meant to achieve its task by running a dynamic multi-agent workflow.

### HumanNode

```python
HumanNode(id, rank, prompt, shell_cmd=None, timeout_s=None, **common)
```

Writes `human/<id>-<inv>.request.json` in the run dir, launches the human-input shell (`shell_cmd` overrides the exe), and blocks until the response file supplies the declared outputs; `timeout_s=None` waits forever. String answers for non-`str` declared types are `json.loads`-coerced only when that parses — declare booleans as `bool` and phrase the prompt to demand `true`/`false`, since `"no"` stays a truthy string. The node commits TRUE for yes and no alike (both outputs defined); the decision belongs in a downstream gate on the value. `GW_HUMAN_AUTO` answers human nodes without the shell.

Never include a human node in the graph without the user's consent. Proposing their inclusion is no burden or hassle at all if you believe the graph would benefit.
- The human node's content must specify the exact allowable selections, either as buttons or as string-literals, in its embedded instructions.

### Edge

```python
Edge(id, inputs, outputs, input_gates=None, output_gates=None, universal_gate=None)
Gate(expression="True", p_invocation=None)
```

First-class N:M; endpoints are node objects or id strings; `g.generator` may appear only in `inputs`. Gates are `Gate` objects, bare expression strings, or `None` (identity); `input_gates`/`output_gates` map node id to gate. The edge contributes exactly one term to each output node's gate:

    term(edge, o) = universal_gate AND output_gate(o)
                    AND, over every input i: input_gate(i) AND output_state(i)

With default edge and node gates, an N:M edge has the same Boolean arming condition as N×M separate 1:1 edges. Capture is still per edge: an input that never committed, or committed FALSE, makes the term FALSE, and a FALSE-term edge transfers no data at capture. Placement is scope: `universal_gate` covers the whole edge (and owns one shared random draw); `output_gates` are per-destination (independent draws — per-branch probabilities go here); `input_gates` are ANDed in like the rest — a FALSE input gate blocks the whole term, it does not deliver the other inputs. There is no per-edge OR: give the node separate edges and write `gate="e_a or e_b"`.

Universal and output gates evaluate before input output-states are checked, including at run start, so guard every read of a node output with `isDefined(...)`.

Edge-gate expressions additionally see `sampled_value` — the gate's own seeded draw in [0,1) — and `p_invocation` if declared (must be in [0,1]); the stochastic idiom is `Gate("sampled_value <= p_invocation", p_invocation=0.35)`. Each gate owns an RNG stream seeded by `(run seed, gate id)`: draw #0 at run start, a redraw at every commit of the edge's input nodes, never at evaluation. Same seed + same definition = same draws, exactly; each redraw of a non-default gate is a `draw` trace event.

For fan-in that completes even when a supplier commits FALSE, assume registered A and B each run once, declare `a` and `b`, and leave rank 20 unused:

```python
def collect(i):
    return {"results": {"a": i.get("a", None), "b": i.get("b", None)}}


C = ScriptNode(
    "C", rank=20, fn=collect,
    inputs={"a": str, "b": str}, outputs={"results": dict},
    gate="A.commits >= 1 and B.commits >= 1",
)
g.add_nodes(C)
g.add_edges(
    Edge("e_a", inputs=[A], outputs=[C]),
    Edge("e_b", inputs=[B], outputs=[C]),
)
```

The node gate waits for both completions; each edge supplies data only when its own term is TRUE. If A commits TRUE and B FALSE, C captures `a` and sees `b` as `UNDEFINED`; `i.get` replaces only `UNDEFINED` with the chosen fallback. The default node gate would block C. With one grouped `[A, B] -> C` edge and the custom gate above, C would run but capture neither value: B makes that edge's sole term FALSE.

C's own commit does not re-evaluate its gate (the gate mentions A and B, not C), so single-run suppliers need no `C.invocations == 0` guard; suppliers that re-fire need the bound and alternation guard from Execution.

## Expressions

Node gates, edge gates, and output expressions are Python strings eval'd on demand in one namespace, later entries shadowing: the definition module's globals (module helpers and constants are therefore usable by name), node handles for every node id plus `GEN`, `isDefined`/`UNDEFINED`, then context extras — in a node gate, each in-edge's term bound by edge id (available nowhere else); in an edge gate, `sampled_value`/`p_invocation`; in an output expression, the declared outputs by name. Any exception inside an expression is caught, logged as a `warning` trace event, and the expression counts as False — the run behaves, but a clean trace has no warnings, which is what the `isDefined` guards are for.

A handle exposes declared outputs (latest committed value, `UNDEFINED` before the first commit) plus metrics — the reserved names outputs may not use: `id`, `rank`, `state` (`"idle"`/`"ready"`/`"running"`; paused reads as running), `output_state`, `invocations`/`commits` (starts vs commits — a mid-flight invocation has incremented only the former), `context_size`, `duration_s`, `usage`, `session_id`, `last_commit_seq`.

`commits` increments for both TRUE and FALSE commits, including error completions. Thus `A.commits >= 1` means A has completed at least once. `output_state` is the latest output-expression result, not an error flag: partial outputs may commit FALSE, and a custom output expression can commit TRUE even after an error. Values on a FALSE node remain readable through its handle but do not transfer through a FALSE-term edge.

Handle injection is confined to expression evaluation: it creates no module variables and does not change a helper's Python globals. A module global sharing a node ID is shadowed; validation warns unless it holds that exact registered node object. An absent global causes no shadow warning.

The gate `"A.commits >= 1 and B.commits >= 1"` and this helper form expose the same dependencies:

```python
def finished(*nodes):
    return all(node.commits >= 1 for node in nodes)


gate="finished(A, B)"
```

In `gate="finished_group()"`, node references hidden inside the helper add no dependencies; neither do nodes hidden in a module-level collection. Incoming suppliers remain dependencies. Pass needed handles as explicit arguments to supply them to the helper and expose them to the syntax scan. Output expressions are not scanned for gate dependencies.

## Running

```python
run(graph, seed=None, namespace=None, run_dir=None,
    validate_graph=True, console=True, max_commits=None, monitor=None) -> RunResult
resume_run(graph, trace_path) -> RunResult
```

`seed`, `run_dir`, and `monitor` default to the graph's declarations. `run_dir` falls back to `GW_RUN_DIR` (a launcher's steer for undeclared graphs), then a hashed folder in `%TEMP%`. `namespace` defaults to the caller's module globals — which is how the expression namespace comes to be the definition module; override only when embedding. `RunResult`: `run_id`, `run_dir`, `trace_path`, `seed`, `reason`, `states`, `output_state(nid)`, `outputs(nid)`.

A live run polls `control.json` in the run dir (~4×/s; `{"command": "pause"}` / `resume` / `stop`; a file already present at startup is stale and ignored — in-process, `Runtime` has `pause()`/`resume()`/`stop()`). Pause lets scripts and humans finish and halts each running LLM session politely, holding its invocation open; Resume completes that same invocation; Stop ends the run (`reason: stopped`). `resume_run` replays a crashed or abandoned run from its trace: the definition and seed must be unchanged (replayed draws are checked; mismatch raises `TraceReplayError`), restored outputs come back in their JSON trace forms, and invocations in flight at the crash re-run with their captured inputs.

The run directory: `trace.jsonl` (authoritative, append per event), `status.json` (live snapshot), `control.json` (yours), `prompts/<node>-<inv>.txt` (every rendered LLM prompt), `sessions/<node>-<inv>.jsonl` (provider stream tee), `human/` (request/response files). Trace events, completely: `run_start`, `generator_fire`, `reeval` (node, value, per-edge terms, cause), `ready` (captured inputs), `demoted`, `start`, `commit` (outputs, output_state, error), `draw`, `warning`, `pause_requested`, `node_interrupted`, `node_pause_acked`, `paused`, `resume_requested`, `resumed`, `resumed_from_disk`, `run_end` (reason); every event carries `seq` and `ts`. The trace is the diagnostic instrument: `reeval` explains arming, `ready` explains data, `commit` explains everything downstream.

| env var | effect |
|---|---|
| `GW_RUN_DIR` | run dir for a graph that declares none (the Monitor sets it to watch such a run) |
| `GW_HUMAN_AUTO` | JSON file `{"<node id>" or "*": {output: value}}`; answers human nodes, no shell |
| `GW_HUMAN_SHELL` / `GW_HUMAN_SHELL_AUTO` | shell exe override / drive the real shell headlessly |
| `GW_CODEX_EXEC` | `1` = legacy `codex exec` instead of app-server |

## Verification

`python -m graph_workflow.gui validate <file>` executes the file with `run()` intercepted and prints `{"ok": true, "errors": [], "warnings": []}` when clean. Errors are structural: duplicate ranks, node/edge id collisions, output names on the reserved-metrics list, unknown endpoints, anything upstream of the Generator, a Generator-fed-only node declaring inputs, an input no upstream output could supply, a `persist_from` reference without a directed path from its source, expression syntax errors, `p_invocation` out of range, unknown provider or missing model/prompt. Warnings are curatorial: no in-edges, unresolvable names in expressions, semaphores for unknown providers, shadowed module names. What validation cannot see — gate logic, `$name` typos inside prompts, unbounded cycles, a model ignoring the JSON contract — surfaces only in a run. So every definition gets both before handover:

```powershell
python -m graph_workflow.gui validate mygraph.py
python mygraph.py                                                  # REASON: quiescent
```

Then read the trace (`result.run_dir` names the folder): last event `run_end` with reason `quiescent`, the expected commits with `output_state: true` and `error: null`, no `warning` events, no literal `$name` in `prompts/*.txt`. Set `GW_HUMAN_AUTO` to get through human nodes unattended.

## Policy

- Definitions carry no narrative comments. A single-line docstring naming the graph's shape is the ceiling; a coupling that would otherwise need a comment becomes structure instead — a shared module constant, referenced by name from every gate string that depends on it.
- Topology is static; there is no dynamic node spawning. Collections: iterate inside one script node, unroll a known N into parallel nodes, or ride the payload around a bounded cycle.
- Every quiescence path is designed: loop bounds, branch escape hatches whose constants match the bounds they answer, `max_commits` as backstop.
- Keep script fns and prompt callables as named module-level defs. The Monitor also preserves callable expressions in direct module-level node constructors, including `parse=capture("answer")`, partials, and inline lambdas. A callable created inside a loop or function needs its own surviving module-level binding for GUI saves; otherwise the Monitor names the node and callable it cannot save. In a GUI-saved file, hand-edit only the `# --- gw:helpers ---` section; `# --- gw:graph ---` is regenerated wholesale on every GUI save, and `# --- gw:gui ---` is display metadata the runtime ignores.
- Monitor deletion removes Boolean gate predicates that depend on the deleted node, including terms for edges emptied by deletion. It preserves remaining `and`/`or`/`not` structure and restores the native default when no predicates remain. A predicate mixing deleted and surviving runtime bindings, or creating a variable binding, requires an explicit edit first. Deletion and gate cleanup undo together. Declared data inputs and their consuming code still need review when a supplier is removed.

## A full definition

Every remaining construct, once: bounded LLM loop with alternation guard, convergent edge, stochastic gate, custom output expression, branch with escape hatch, human approval with the decision in a downstream gate.

```python
"""Bounded draft/critique loop, stochastic audit, conditional publish, human approval."""

import os

from graph_workflow import (
    Edge, Gate, Graph, HumanNode, LLMNode, ScriptNode, Semaphores, isDefined, run,
)

MAX_DRAFTS = 3
PASS_SCORE = 7
RUN_DIR = os.path.join(os.environ["LOCALAPPDATA"], "Temp", "gw-pitch-loop")


def draft_prompt(i):
    lines = [f"Write a 4-6 sentence product pitch for {i.topic}, aimed at {i.audience}."]
    if isDefined(i.notes):
        lines.append(f"This is a revision. Address this reviewer feedback: {i.notes}")
    lines.append(
        "End your reply with exactly one JSON object, no prose after it, "
        'no code fences: {"draft": "<the pitch>"}'
    )
    return "\n".join(lines)


def assemble(i):
    return {"final_md": f"{i.draft}\n\n---\nscore {i.score}/10 - {i.notes}"}


g = Graph("pitch-loop", semaphores=Semaphores(script=4, llm=2, human=1), run_dir=RUN_DIR)

S1 = ScriptNode(
    "S1", rank=0,
    fn=lambda i: {"topic": "a pocket spectrometer", "audience": "field geologists"},
    outputs={"topic": str, "audience": str},
)

L1 = LLMNode(
    "L1", rank=1,
    provider="claude", model="opus", effort="medium",
    prompt=draft_prompt,
    inputs={"topic": str, "audience": str, "notes": str},
    outputs={"response": str, "draft": str},
    cwd=RUN_DIR,
    gate=(
        "(e_topic and L1.invocations == 0)"
        " or (e_back and L2.invocations == L1.invocations"
        " and L1.invocations < MAX_DRAFTS"
        " and isDefined(L2.score) and L2.score < PASS_SCORE)"
    ),
)

L2 = LLMNode(
    "L2", rank=2,
    provider="claude", model="fable", effort="high",
    prompt=(
        "You are a strict pitch reviewer. The product: $topic. The draft:\n\n"
        "$draft\n\nScore it 0-10 for specificity and punch, and give one "
        "actionable sentence of feedback. "
        "End your reply with exactly one JSON object, no prose after it, "
        'no code fences: {"score": <integer 0-10>, "notes": "<one sentence>"}'
    ),
    inputs={"topic": str, "draft": str},
    outputs={"response": str, "score": int, "notes": str},
    cwd=RUN_DIR,
)

S2 = ScriptNode(
    "S2", rank=3,
    fn=lambda i: i.score < 5,
    inputs={"score": int},
    outputs={"flag": bool},
    output_expression="isDefined(flag) and not flag",
)

S3 = ScriptNode(
    "S3", rank=4,
    fn=assemble,
    inputs={"draft": str, "score": int, "notes": str},
    outputs={"final_md": str},
)

H1 = HumanNode(
    "H1", rank=5,
    prompt=(
        "A pitch is ready to publish.\n\n$final_md\n\n"
        "Approve publication? Set `approved` true or false; `remarks` optional."
    ),
    inputs={"final_md": str},
    outputs={"approved": bool, "remarks": str},
)

S4 = ScriptNode(
    "S4", rank=6,
    fn=lambda i: {"receipt": f"published with remarks: {i.get('remarks', '')}"},
    inputs={"remarks": str},
    outputs={"receipt": str},
    gate="e_go and H1.approved",
)

g.add_nodes(S1, L1, L2, S2, S3, H1, S4)
g.add_edges(
    Edge("gen_S1", inputs=[g.generator], outputs=[S1]),
    Edge("e_topic", inputs=[S1], outputs=[L1]),
    Edge("e_review", inputs=[S1, L1], outputs=[L2]),
    Edge("e_back", inputs=[L2], outputs=[L1]),
    Edge("e_audit", inputs=[L2], outputs=[S2],
         output_gates={"S2": Gate("sampled_value <= p_invocation",
                                  p_invocation=0.35)}),
    Edge("e_final", inputs=[L1], outputs=[S3]),
    Edge("e_pub", inputs=[L2], outputs=[S3],
         universal_gate="isDefined(L2.score) and"
                        " (L2.score >= PASS_SCORE or L1.invocations >= MAX_DRAFTS)"),
    Edge("e_ship", inputs=[S3], outputs=[H1]),
    Edge("e_go", inputs=[H1], outputs=[S4]),
)

if __name__ == "__main__":
    result = run(g, max_commits=50)
    print("REASON:", result.reason)
    for nid in ("S1", "L1", "L2", "S2", "S3", "H1", "S4"):
        st = result.states[nid]
        print(nid, st.output_state, st.invocations)
```

At any seed it quiesces: the loop exits by score or by exhausting `MAX_DRAFTS` (the escape hatch on `e_pub` keeps S3 reachable either way), S2 fires per its seeded draws, S4 only on approval. Verified with `seed=7` declared and `GW_HUMAN_AUTO` = `{"H1": {"approved": true, "remarks": "ship it"}}`: 34 trace events, all six commits TRUE, S2 idle (its draw, 0.559, exceeds 0.35), zero warnings, `run_end` reason `quiescent`.
