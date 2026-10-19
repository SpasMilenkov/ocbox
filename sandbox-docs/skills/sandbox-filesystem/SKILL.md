---
name: sandbox-filesystem
description: Use before creating, restoring or searching for secret or config files, before editing git hooks, git config or agent config, and when a path looks unexpectedly empty or a write fails with a permission or read-only error. Explains what is hidden, read-only or separate in this sandbox.
---

# Files in this sandbox

The project is mounted at `/work`. You can read and write most of it. Your home directory (`/home/agent`) is scratch space for caches and tool state. `/tmp` is memory-backed, limited to {{tmp_size}}, and counts against your memory limit.

## Hidden on purpose ({{hidden_count}})

These paths appear empty. Secrets, credentials, certificates, database dumps and similar data live there on the user's machine. The emptiness is intentional.

{{hidden_paths}}

- Do not recreate them, fill them with guessed values, or search elsewhere for the real ones.
- If a task needs one of them, say which one and ask the user.

## Read-only

{{readonly_paths}}

Git hooks, git config and the agent's own config cannot be changed from here, so nothing planted there can run on the user's machine. If you think one needs to change, tell the user what and why.

## Build output lives elsewhere

{{isolated_count}} directories (`node_modules`, `bin`, `obj`) are separate volumes that start empty and are not part of the user's tree. Run the install or restore step before you build or test. Their contents never reach the user's machine, so a successful build here leaves nothing behind there.

Examples:

{{isolated_examples}}

## Git and review

- `git` works inside the project, and commits are fine. Pushing is not possible. The user pushes after review.
- Unless the user skipped it, a snapshot was taken before this session, so everything can be rolled back.
- When the session ends, files that run on the user's machine when built or committed are flagged for review: scripts, `package.json`, project and props files, compose files, Dockerfiles, CI and editor config, and similar. You may edit them when the task needs it, but say so in your answer.
