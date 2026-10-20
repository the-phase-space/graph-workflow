Graph Workflow

Build workflows that combine Python scripts, AI agents, and human decisions.
Each step is a node, with conditions that determine when it runs. Workflows
can branch, run steps in parallel, and loop through revisions.

The Windows Monitor lets you create and edit graphs, start runs, inspect
results, and pause, resume, or stop execution.

Executables are built locally and are not included in a clone. Follow
guide/building.txt first, then open monitor\bin\graph-workflow-monitor.exe.
Create a graph or open an existing Python definition, then run it. You can
also run a definition directly: python mygraph.py

Requires Python 3.11 or later. AI steps also need the relevant command-line
tool installed and authenticated.

Before asking any LLM to construct a graph definition, point it to
guide/authoring.md and have it read the guide. That file is the authoring
guide for LLMs; it contains the rules, API details, and examples they need.

Logs and results are saved in a run folder. Keep run folders outside
Dropbox and other synced directories.
