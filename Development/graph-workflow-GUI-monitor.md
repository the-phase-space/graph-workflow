## Monitor GUI

**Application role**

The Monitor GUI is the project's desktop interface for both graph authoring and graph execution. When no graph is executing, it is a graph editor and validation environment; while a graph is executing, it is a live visualization and operational control surface. Monitoring is therefore one responsibility of the application rather than its complete identity.

**Launch model and session ownership**

Executing a graph definition automatically launches or attaches the Monitor GUI for that run. The Monitor GUI is also independently launchable as a Windows `.exe`. Both entry paths converge on one shared GUI/runtime session rather than recursively launching duplicate application or runtime instances.

**Backend authority and technology boundary**

Python is the authoritative backend for authoring semantics, validation, execution, monitoring, and run control. JSON is the structured representation and interchange layer at the application boundary; the React frontend is not an independent source of graph semantics. Frontend and backend communicate through a shared machine-readable contract for graph properties, capabilities, validation results, runtime events, and control requests.

Tauri + React is the provisional desktop stack, not yet a fixed technology commitment.

**Graph representation and state separation**

Graph-definition state, GUI layout state, and per-run runtime state are distinct forms of state. The saved graph must remain compatible with the executable Python graph definition established in the Core Model, while exposing its GUI-editable material through the structured Python/JSON contract. Arbitrary handwritten Python outside that structured contract is not presumed to be losslessly reversible into editable GUI elements.

Loading an unstructured (handwritten) definition carves its module-level helper code — everything the structured regeneration would not re-emit — into the contract's helpers section, so converting to the structured format preserves the definitions its prompts, script functions, and expressions reference. Saving is fail-safe: the regenerated definition is proven to execute from a temporary file and only then atomically replaces the target; a save that would produce a broken definition fails without touching the file on disk. Expression names that resolve to neither a node, an edge, a helper, nor a builtin surface as validation warnings.

GUI-authored state can be saved, including with `Ctrl+S`.

**Build mode and authoring completeness**

Build mode is available when a graph is not executing. It provides interactive tools for composing the graph and exposes every setting controllable at the CLI layer, including boolean expressions, linked helper scripts, prompts, declared inputs and outputs, edges and gates, models, effort levels, tool permissions, and all other CLI-controllable settings.

Models are selected through friendly display aliases (e.g. `Fable`, `Sonnet`, `Sol`, `Terra`, `Grok 4.5`) that map to the per-provider model identifiers of the Provider CLI Invocation Reference; the saved definition and the CLI flag always carry the model identifier, never the alias. A `Custom` option accepts an arbitrary identifier passed verbatim. A node prompt backed by a helper callable is directly editable: typed text overrides the callable with a literal prompt, and clearing the field restores the callable.

The left sidebar includes an explicit action that runs the project's input/output validator through the Python backend.

**Execution mode and immutability**

Execution mode is used while a graph is executing. In accordance with the Core Model, the topology is frozen and edge and gate properties remain static for the duration of the run; execution mode may inspect them but does not mutate the executing graph's edge semantics.

**Window composition and themes**

The graph name appears in the window title rather than in a dedicated top bar. Nearly the entire window is devoted to the graph canvas, supplemented by overlays, a minimizable left sidebar, and a docked right inspector panel for element properties. Both light and dark modes are required.

**Canvas navigation and graph layout**

The default viewport shows the whole graph at whatever zoom level is required. The graph's primary visual flow is bottom-to-top. Cyclic graphs retain that dominant orientation while necessarily routing return edges against it.

The canvas supports mouse-drag panning and scroll-wheel zooming. A fit-to-view control at the top right restores the full-graph view. Nodes are not user-movable.

**Selection and property inspection**

Nodes and edges are both selectable. Selecting either opens the inspector: a right-docked sidebar exposing all properties of the selected graph element. The inspector opens at one third of the workspace width by default and is freely resizable by dragging its left edge; the chosen width persists across sessions. Clicking empty canvas minimizes it, as does its collapse control — a sidebar glyph, not a close `×`. Short fields share rows (provider, model, and effort sit on one line) and expression editors are tall enough for typical expressions, scrolling for longer ones.

**Node shape, identity, and collapsed content**

All nodes are rounded-corner rectangles. The collapsed node face does not display the words `LLM`, `Script`, or `Human`; category or provider identity is communicated by its base fill color:

- Claude LLM node: translucent orange, with sufficient saturation and opacity not to read as brown.
- Codex LLM node: translucent blue.
- Grok LLM node: translucent purple.
- Script node: translucent gray.
- Human node: translucent beige.

The collapsed face displays at least the node's name, token usage (for LLM nodes), and a count-up timer showing how long it has been in the running state.

**Node runtime and outcome visualization**

Node visuals communicate several orthogonal dimensions rather than one mutually exclusive status value: provider/type, current scheduling state, invocation outcome or history, and—if later adopted—expression truth values. The established runtime and outcome treatments are:

- `running`: yellow external glow.
- `ready`: blue external glow.
- "Error" (as detected in trace parsing by the runtime): red external glow.
- "Completed" (`idle` & state was previously `running`): green external glow. 

**Edge and gate visualization**

Edges are visually thick and carry one or more arrows indicating direction. A 1:1 edge is rendered as a single routed path between its nodes. A divergent edge uses a shared trunk and branches, with direction arrows both before and after the divergence.

A convergent or divergent edge remains one logical, selectable edge even when its rendering consists of multiple segments and a junction. This preserves the first-class edge identity established in the Core Model.

Non-default gates are reflected in arrow color:

- All-default (passthrough) edge: black in light mode and white in dark mode, including its direction arrow.
- Non-default gate: golden-yellow direction arrows.

**Node activity and input/output inspection**

A selected node's inspector contains a scrollable activity-log pane that updates in real time. It displays every message and, for Claude, every available thought summary. A `Show tool uses` checkbox controls the inclusion of tool-use events.

The inspector clearly lists every declared input and output and marks its current status. A received input or written output has a green outline.

**Graph statistics sidebar**

A minimizable left sidebar displays graph-level statistics, including elapsed execution time, total token usage, and node lifecycle status counts.

**Orderly pause and resume**

The sidebar contains a prominent green-ish `Pause` button with a pause symbol. Pause is an orderly operation: upon activation, it reads `Pausing ...` while `running` nodes stop at convenient points, then becomes `Resume` once the graph is paused. Achieving pause freezes graph progress without force-terminating in-flight invocations.

Its other precise lifecycle and checkpoint semantics are deferred to the node-execution and run-lifecycle specifications.

**Immediate stop**

A red `Stop` button, slightly smaller than and positioned below `Pause`, performs an immediate, disorderly stop. It prevents any further nodes from beginning execution and force-terminates the complete process tree belonging to every `running` node rather than relying solely on termination of each recorded parent PID.