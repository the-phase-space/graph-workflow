The document nails down the control plane — when nodes fire — in great detail, but almost everything about what happens *during* and *between* runs is still open. Here's what I'd consider genuinely underspecified for a prototype, roughly in order of how load-bearing it is:

1. **The node execution contract — what "running" actually is.**

    The schema never says what's inside a node. For agent nodes: where does the prompt come from, how are the declared inputs injected into it (template? files? stdin?), and — the hardest one — how do "explicitly typed" outputs (decision 18) get extracted from a `claude -p` / `codex exec` text stream? Structured-output JSON, a write-to-file convention, and post-hoc parsing are very different designs. Also absent: per-node model/effort (implied by the monitor's badges), tool restrictions (headline capability #2, no schema field), system prompt, and working directory. For script nodes: is a script an inline Python callable, an external file, a shell command — and what's its input/output signature? For human nodes: the actual interaction channel (console? the monitor GUI?) is unspecified.

2. **The data plane.**

    Decision 26 says outputs and inputs "pair across edges," but not the mechanism: matched by name? An explicit per-edge mapping (`source.outX → target.inY`)? Does a data-bearing edge carry *all* of the source's outputs or a declared subset, and where is that declared? Also open: the type system and serialization (by value vs. by reference for large artifacts like transcripts), what re-activation does to previously delivered input values (overwrite? persist stale?), and what happens to outputs produced *after* a priority output has already triggered the one-per-activation output evaluation.

3. **How expressions are authored and evaluated.**

    Activation expressions, output expressions, and gate expressions are all central, and all currently exist only as notation. In the Python API, are they strings that get parsed, lambdas, an operator-overloaded DSL? What namespace does a gate condition evaluate in — `context_size >= 69420` implies queryable live runtime metrics on nodes, whose source and update cadence are unspecified. Related: an identity scheme for nodes/edges/gates is required by three separate decisions (RNG stream derivation, the pending line's ordering, the monitor) and explicitly not established.

4. **Failure semantics.**

    Not an edge case — there is currently no concept of failure at all. A CLI invocation that errors, times out, or completes without producing its declared outputs leaves the node... `active` forever, holding its semaphore slot? Is there a `failed` state (decision 7 leaves the state set open-ended), a retry policy, and what does the output state become? Every real run will hit this.

5. **Run lifecycle and the exit condition.**

    Decision 6 says termination isn't a schema guarantee, but `python workflow.py` still needs an operational rule for when the process exits — presumably quiescence detection (nothing active, nothing pending, no trigger possible), which needs defining. Also: Ctrl+C behavior, whether anything persists to disk (run directory, journal, artifacts), and whether the deterministic seed implies replay/resume is a goal or just reproducibility.

6. **Monitor architecture.**

    Headline feature #5 has zero schema: the transport between the Python engine and the React Flow frontend (local server + websocket?), the event schema, how per-node transcripts/token counts are captured from the CLIs (presumably `--output-format stream-json`, but unstated), whether the monitor is mandatory or optional, and who launches it. This is a large fraction of prototype effort.

7. **Execution substrate.**

    "Process-grade isolation per node" is a stated capability, and CONCURRENCY is bounded by `nThreads` — but threads vs. processes vs. an asyncio loop managing subprocesses is undecided, along with how trigger evaluation and scheduler updates are serialized when concurrent nodes finish simultaneously. Smaller, same bucket: ollama invocation (CLI vs. HTTP API, per-node model) is named as a provider but never specified.

The document also flags two gaps itself — where `hasChanged` bookkeeping lives (decision 20) and how converged edges interact with the trigger structure (decision 24) — both real, though narrower than the items above.

If you want a rough priority for the design conversation: items 1–3 shape the authoring API and would be painful to retrofit; 4–5 shape the engine; 6–7 can be decided later without destabilizing anything.
