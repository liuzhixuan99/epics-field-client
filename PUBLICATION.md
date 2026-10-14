# Public release boundaries

Publish tracked source files from this repository only. Use `git archive` for source packages; do not recursively zip a development workspace.

Excluded content includes runtime reports, saved configuration, screenshots, logs, virtual environments, package caches, local audit files and credentials. These are excluded from Git and must not be force-added.

The application collects actual information from the operator's machine. Do not commit runtime reports or screenshots containing hostnames, addresses, user paths or device data. Keep operational data separate from public source packages.

Before publishing changes:

1. Review `git status` and `git ls-files`, including new files.
2. Review commit author and committer metadata, all reachable history, tags and repository configuration for unintended personal information.
3. Check source and packages for credentials, internal addresses, hostnames, user paths and private documents.
4. Inspect the actual ZIP, source distribution and wheel contents. Do not include bytecode or generated diagnostics.
5. Run tests and confirm actual environment collection, report access isolation and read-only device boundaries.

Portable binaries are Release assets, not Git source files. Retain component license texts and inventories. Do not bundle unused GNU Readline/curses. For native libraries requiring corresponding source, include the matching source, distribution patches and build specification with the binary release; verify provenance rather than assuming a compiler version. See `THIRD_PARTY_NOTICES.txt` and `third-party-licenses/Native-source-provenance.txt` for this release.

This repository uses a neutral local commit identity for its initial public snapshot. Contributors should choose an appropriate public Git identity before adding commits. No automatic upload or GitHub publication is built into the application.
