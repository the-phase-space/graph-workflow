Graph Workflow is a Python library that runs directed graphs of LLMs, Python
scripts, and (if you want) human-in-the-loop nodes. It allows arbitrary code
execution, unlike the workflow tool in, say, Claude Code.

Workflows can branch, run in parallel, and loop.

Build the two Windows apps before using them; executables are not included
in the repository. You can open this repo in Claude or Codex and ask:

    Build the Monitor and human-input app without installers. Follow the
    repository's instructions and put the executables in their bin folders.

You may want to put shortcuts to the executables somewhere more convenient,
of course.

For a manual build, install Python 3.11+ (available as "python"), Node.js/npm,
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

Read guide/authoring.md before writing a graph definition. This tool was
created with a highly non-idiot-proof ethos. It's possible to quickly waste
a lot of tokens with it. The ceiling of what you can build is also simply
your imagination (and budget).

To watch a graph run in real time, load the resulting .py file in the Monitor
and run it. AI steps require their provider's command-line tool to be installed
and signed in. Human steps open an input window when they need you. Keep run
folders outside cloud-synced storage if you can.

Currently, the runtime supports up to 2 accounts for Claude and OpenAI, with
their home directories needing to be called `.claudeb` and `.codexb`. But your
local assistant can fix this easily if you have multiple accounts with
different home directories.
