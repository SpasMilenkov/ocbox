---
name: sandbox-tools
description: Use when choosing which command to run, before trying to install software, when a command is not found, or when a process is killed without an error message. Lists what is installed in this sandbox, what is not, and the resource limits.
---

# Tools and limits

## Installed

{{tools_installed}}

Anything not listed may be missing. Check with `command -v NAME` before assuming a tool exists.

## Not available

{{tools_missing}}

You cannot install system packages. Do not try `sudo`, `apt` or similar. If a tool is missing, tell the user which one and why you need it.

## Resource limits

- Memory: {{mem_limit}}, shared by everything you run, including `/tmp`.
- CPUs: {{cpu_limit}}
- Processes and threads: {{pid_limit}}

A process that disappears with no error, or a build that dies suddenly, probably hit one of these limits. Report it to the user instead of retrying in a loop.
