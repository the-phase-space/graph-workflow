Graph Workflow is a Python library that runs directed graphs of programmatic 
LLM CLI sessions (Including Claude and Codex), Python scripts, and (if you want)
human-in-the-loop nodes.

It allows arbitrary code execution, unlike the workflow tool in Claude Code.

The Graph's topology can branch, loop, form any node-edge configuration you like. 
This topology remains static once execution begins. The rules governing how 
information propagates through nodes and edges are left fully within the control
of the user. Each gate is simply a boolean expression.

Build the two Windows apps before using them; executables are not included
in the repository. You can open this repo in Claude or Codex and ask:

    Build the Monitor and human-input app without installers. Follow the
    repository's instructions and put the executables in their bin folders.

You may want to put shortcuts to the executables somewhere more convenient,
of course.

For a manual build, you'll need Python 3.11+ (available as "python"), Node.js/npm,
Rust with the MSVC toolchain, Microsoft C++ Build Tools, and WebView2.
Tauri's prerequisite guide covers the build tools:
https://v2.tauri.app/start/prerequisites/

In PowerShell, starting at the repository root:

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

Read (have your LLM assistant read) guide/authoring.md before writing a graph 
definition. This tool was created with a user-trusting, non-idiot-proof ethos. 
It's possible to build graphs that loop perpetually, that fan out more than 
your subscription(s) can handle, etc. 
But that also means that your only constraints are:
1) your imagination 
2) your budget

To watch a graph run in real time, load the resulting .py file in the Monitor
and run it. AI steps require their provider's command-line tool to be installed
and signed in. Human steps open an input shell when they need you. Keep run
folders outside cloud-synced storage if you can.

Currently, the runtime supports up to 2 accounts each for Claude and OpenAI.
The default accounts use `~/.claude` and `~/.codex`; the second accounts use
`~/.claudeb` and `~/.codexb`. Your local assistant can adapt this if you have
more accounts or use different configuration directories.
