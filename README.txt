Graph Workflow

Run workflows that combine AI agents, Python scripts, and human decisions.
Describe your workflow to an LLM assistant; it writes the graph definition.
The desktop Monitor lets you run the graph, watch progress, inspect results,
and pause or stop execution. Workflows can branch, run in parallel, and loop.

Build the two Windows apps before using them; executables are not included
in the repository. You can open this repo in Claude or Codex and ask:

    Build the Monitor and human-input app without installers. Follow the
    repository's instructions and put the executables in their bin folders.

For a manual build, install Python 3.11+ (available as "python"), Node.js/npm,
Rust with the MSVC toolchain, Microsoft C++ Build Tools, and WebView2.
Tauri's prerequisite guide covers the build tools:
https://v2.tauri.app/start/prerequisites/

Use a checkout outside Dropbox or other synced folders. In PowerShell,
starting at the repository root:

    cd monitor
    npm ci
    npm run build
    npm run tauri -- build --no-bundle
    cd ..\human-shell
    & ..\monitor\node_modules\.bin\tauri.cmd build --no-bundle
    cd ..

Copy each executable from its reported build output to these repository
paths, creating the bin folders if needed:

    monitor\bin\graph-workflow-monitor.exe
    human-shell\bin\human-shell.exe

Open graph-workflow-monitor.exe. To create a workflow, tell your assistant:

    Read guide/authoring.md before writing a graph definition. Then create
    a workflow that [describe what you want].

Load the resulting .py file in the Monitor and run it. AI steps require
their provider's command-line tool to be installed and signed in. Human
steps open an input window when they need you. Keep run folders outside
cloud-synced storage.
