# ocbox

### Made to be used by my for my specific workflows, but you can ask Claude to patch it up to fit yours if you so wish. This is more of a repo for me to keep track of changes and sync between devices more than  anything.


Run the [OpenCode](https://opencode.ai) agent in a locked-down, **rootless Podman** sandbox
instead of directly on your machine. Built for the case where you use hosted models but do not
fully trust the vendor's infrastructure or release pipeline.

```
 your host                     ocbox-internal network (NO internet route)
┌────────────────────┐        ┌───────────────────────────────────────────┐
│ your project       │ bind   │  agent container (OpenCode + the toolchain │
│ (secrets masked)   ├───────►│  you configure in [tools])                │
│ ocbox/backups  ✗   │        │  read-only rootfs, no capabilities,       │
│ your $HOME     ✗   │        │  fake $HOME, memory/CPU/pid limits        │
└────────────────────┘        └──────────────┬────────────────────────────┘
                                             │ HTTPS only, via proxy
                              ┌──────────────▼────────────────┐
                              │ egress proxy (allowlist,      │──► internet
                              │ public IPs only, logs hosts)  │    (allowed hosts only)
                              └───────────────────────────────┘
```

## What it protects against, and what it does not

**Protects against** a compromised or malicious OpenCode binary, update, dependency or model
response doing things on *your* machine:

| Threat | Mitigation |
|---|---|
| Reading your home dir, SSH keys, other repos, browser data | Only the project dir is mounted; `$HOME` is a separate fake directory |
| Reading project secrets (`.env`, certs, DB dumps, generated configs) | Masked with empty files/dirs (`[hide]` in `ocbox.toml`) |
| Exfiltrating data to arbitrary servers | No direct route out; all HTTPS goes through an allowlist proxy; DNS is not available to the agent |
| Reaching your LAN / cloud metadata endpoints | Proxy refuses any host that resolves to a non-public address |
| Silent self-update to a malicious build | Exact pinned version, npm integrity pinned in `ocbox.lock`, autoupdate disabled, binary root-owned |
| Planting things that run on your host later (git hooks, `core.fsmonitor`, poisoned configs, build output, `node_modules`) | `.git/hooks`, `.git/config` and the project's `opencode.jsonc` are read-only; `node_modules`/`bin`/`obj` live in separate volumes; changed "dangerous" files are flagged after each session |
| Replacing a protected path by moving its parent away (for example `.git`) | Parents of read-only paths are mount points too (they cannot be renamed); protected paths are fingerprinted and checked after every session |
| Data leaving through DNS lookups | The agent network is created with `--disable-dns` and has no route out; `check` verifies that no name resolves |
| Agent trashes the project | Snapshot before every session, one-command restore |
| Container escape via excess privileges | Rootless, `--cap-drop=all`, `no-new-privileges`, read-only root filesystem, limits |

**Does NOT protect against:**

* **Whatever the agent can read is sent to the model provider.** With hosted models, that means
  the provider's servers. The egress allowlist cannot help: that server is, by necessity, an
  allowed destination. Anything not hidden in `[hide]` should be considered shared. Compose
  files, application config and shell scripts often contain passwords: see
  [Before you point it at a real project](#before-you-point-it-at-a-real-project).
* A kernel / container-runtime escape (rare, but a container is not a VM). Keep the host
  updated. For more isolation see [Going further](#going-further).
* Malicious *code the agent writes into your source files* that you then run. The
  post-session report flags risky files, but reviewing `git diff` is your job.
* Dependency supply-chain attacks. Installs run inside the sandbox, which limits the damage,
  but what you later build on the host still contains those dependencies.
* **Services you bridge into the sandbox.** The agent gets full access to them, and if they run
  with normal internet access (for example in Docker) they are a way around the egress
  allowlist. See [Services](#services-databases-brokers-object-stores).
* **Host services on the network gateway.** The host side of the agent network has an address
  of its own. `check` lists every TCP port the host listens on for all interfaces (via `ss`),
  adds your bridged ports and a few common ones, and probes them all from inside the sandbox
  against the gateway and the host address. UDP, and ports that `ss` cannot see, are not
  covered. If the probe ever reports reachable ports, block traffic from the agent network's
  bridge interface in your host firewall.
* **Terminal output.** The agent's terminal UI is relayed to your terminal, and ocbox prints
  file names and host names taken from the session without stripping control characters. Use a
  terminal or multiplexer that ignores clipboard-write and similar escape sequences.
* **A perfect change report.** The post-session summary compares file size and modification
  time, not content, and skips the regenerable directories in `[backup].exclude_dirs`. It is a
  convenience. Review the real diff, and look at untracked files too.

## What the npm install does and does not buy you

Installing from npm instead of a release asset is **not** a security improvement: it is the same
vendor-published artifact, and no checksum can tell you the vendor's pipeline was honest. What
ocbox adds is tamper evidence *after* the first fetch (integrity pinning, no moving tags, no
install scripts, no silent updates) and containment of whatever the binary does. For 2.0.32 it
was observed that the package was published by GitHub Actions through npm trusted publishing and
that the registry shows no provenance attestation for the platform packages, so where the binary
was built could not be verified. Treat the OpenCode binary itself as **untrusted code**; the
sandbox (masked secrets, no direct internet, allowlist proxy, read-only root) is what limits the
damage if it turns out to be hostile.

## Requirements

* Linux with **rootless Podman** (the commands below are for Debian/Ubuntu-family systems)
* **Python 3.11.4+** (Ubuntu 24.04 ships 3.12; 22.04 ships 3.10 and is too old)
* `git` on the host (used to decide which build directories are safe to isolate)
* Disk: the image is a few GB, depending on what you add in `[tools].apt_extra`

```bash
sudo apt install podman uidmap passt git
podman info --format '{{.Host.NetworkBackend}}'    # should print: netavark
grep "$USER" /etc/subuid /etc/subgid                 # rootless needs entries here
```

No other Python packages. `ocbox.py` only uses the standard library.

## Quick start

```bash
cd ocbox
chmod +x ocbox.py

# 1. Edit ocbox.toml: set [tools] to match your project, review [hide].paths.

# 2. Build the image. With dotnet_sdk = "auto" the SDK version is read from the file
#    named in [tools].global_json inside the project; otherwise set an explicit tag.
./ocbox.py build ~/code/myproject

# 3. Verify the sandbox actually does what is promised (do this once, and after upgrades).
./ocbox.py check

# 4. First session in learn mode: see which hosts OpenCode really contacts.
#    Learn mode allows every public HTTPS host, so use a throwaway copy of the project.
./ocbox.py run ~/code/myproject-copy --learn
#    Quit when done. The report lists the hosts. Put the OpenCode ones into [allow].base.

# 5. Normal use: strict allowlist, plus registries only when needed.
./ocbox.py run ~/code/myproject
./ocbox.py run ~/code/myproject --allow nuget,npm
```

Free models: start OpenCode, run `/models` and pick one of the free ones. No API key needed.
Paid providers: `/connect` inside OpenCode, or use `--env NAME` / `[run].pass_env`.
Use a **dedicated key with a spending cap**, never your main one.

## Before you point it at a real project

ocbox decides what to hide by file name and path, not by content. Do this once per project:

```bash
cd ~/code/myproject
grep -rIlE '(password|secret|token|apikey|api_key|connectionstring)' \
  --exclude-dir={node_modules,.git,bin,obj,dist} . | head -50
```

Anything that shows real credentials and is not already covered by `[hide]` should either be
added to `[hide].paths` or have its secret replaced by a throwaway dev value. Typical suspects:
compose files with environment passwords, application settings files, dev scripts, and
monitoring or proxy provisioning configs. The defaults hide anything matching the `[hide].globs`
(`.env`, `*.pem`, `*.key`, and similar) plus the paths in `[hide].paths`. The session start
prints exactly what was hidden. The shipped `[hide].paths` are examples from the original
project: replace them with your own.

If your project already contains an `opencode.jsonc`, it is mounted **read-only**. Read it: if
it defines MCP servers, plugins or custom providers, the hosts they need must be allowlisted.

## How it maps onto your project

| Path | In the sandbox |
|---|---|
| Your source, tests, docs and other working files | read-write: this is the agent's workspace |
| Whatever `[hide]` lists (secrets, certificates, database dumps, generated configs) plus any file matching `[hide].globs` | **hidden** (appear empty) |
| `.git/hooks`, `.git/config`, `opencode.jsonc` (the `[hide].readonly` list) | **read-only**; parent directories such as `.git` are pinned so they cannot be renamed |
| `*/node_modules` for every directory with a `package.json` | separate Podman volume (starts empty: install dependencies inside) |
| `*/bin`, `*/obj` for every directory with a `.csproj`/`.fsproj`/`.vbproj` | separate Podman volumes |
| Build files, scripts, CI and compose files, package manifests (`[review].patterns`) | writable, but **flagged for review** if changed |

The agent can use `git` inside the sandbox (commits work), but cannot change the hooks or git
config. Pushing to a remote is deliberately impossible: do it from the host after reviewing.

## Commands

| Command | What it does |
|---|---|
| `ocbox.py build [PROJECT]` | Download and verify OpenCode, build the image |
| `ocbox.py check` | Start a real session with test probes and print PASS/FAIL |
| `ocbox.py docs [PROJECT] [--allow ...]` | Render and show the sandbox notes the agent will see, without starting anything |
| `ocbox.py run [PROJECT]` | Snapshot, start proxy + agent, report changes afterwards |
| `ocbox.py run PROJECT --allow nuget,npm` | Add egress profiles for this session |
| `ocbox.py run PROJECT --learn` | Allow all public HTTPS and log hosts (supervised use only) |
| `ocbox.py run PROJECT --attach NAME[:ALIAS]` | Connect a running Podman container (see Services) |
| `ocbox.py run PROJECT --no-backup` | Skip the pre-session snapshot |
| `ocbox.py run PROJECT -- bash` | Get a shell in the sandbox instead of OpenCode |
| `ocbox.py backups [PROJECT]` | List snapshots |
| `ocbox.py restore [STAMP]` | Roll the project back to a snapshot (asks first) |
| `ocbox.py restore STAMP --to DIR` | Extract a snapshot elsewhere, touching nothing |
| `ocbox.py wipe --home / --volumes / --all` | Delete OpenCode login+caches / build volumes |
| `ocbox.py cleanup [--networks]` | Remove leftovers after a crash |
| `ocbox.py -n <command>` | Dry run: print the podman commands instead of running them |

`-n` is the best way to learn what the script does while you read the Podman docs.

## Snapshots and restore

Every `run` first writes `backups/<project>-<timestamp>.tar.gz` plus a manifest, on the host,
outside the project and never mounted into a container. Skipped: regenerable directories
(`node_modules`, `bin`, `obj`, `dist`, ...), `[backup].exclude_paths`, and files over
`max_file_mb` (listed as a warning). The `.git` directory is included, so restoring also
rolls back commits the agent made. The last 20 snapshots are kept.

```bash
./ocbox.py backups
./ocbox.py restore                     # newest: shows what will be deleted/overwritten, asks
./ocbox.py restore 20261018-1530       # a specific one (substring of the name)
./ocbox.py restore 20261018-1530 --to /tmp/old   # just look at it
```

A restore itself takes a `-pre-restore` snapshot first, so it can be undone. Snapshots contain
a copy of your secrets (they include hidden files). They are mode 0600 in a 0700 directory and
git-ignored: do not copy them around.

After each session you get a summary of added/modified/deleted files, with a
**REVIEW** list of anything that runs on your machine (hooks, scripts, package manifests,
project files, compose files, ...). Read it before you build or start anything on the host.

## Services (databases, brokers, object stores)

The agent cannot start containers (that needs a container socket, which is root on your host),
and it has no route to your host. If your dev services run elsewhere (Docker, Portainer, a
system package) and publish ports on the host, ocbox uses a **service bridge**: a tiny container
on the agent network that relays *only the ports you list* to the same port on the host.

```toml
[services]
host      = "host.containers.internal"
bridge_ip = "10.201.77.3"
[services.ports]
db      = [5432]
broker  = [5672]
objects = [9000]        # host-published ports
```

Inside the sandbox each service is reachable two ways, so a dev-mode configuration pointing at
`localhost:5432` works unchanged, and so do names (`db:5432`). Start your stack the way you
normally do, then `./ocbox.py run PROJECT`. `./ocbox.py check` reports PASS/WARN per service.

* **Published ports are what matter, not the container runtime's internal names.** List the port
  as seen from the host, not the port inside the container.
* **The agent gets full access** to these services with whatever credentials it can find or
  you give it. It can read, change and delete data. Point it at a **throwaway database** (or take
  a dump before a session: ocbox snapshots the project files, *not* service data). Credential
  files that are hidden by `[hide]` stay unknown to the agent unless you provide dev-only ones.
* **A bridged service can be a way out.** If it runs with normal internet access, anything that
  lets the agent execute code or make outbound connections through it (a superuser database
  role, a broker with an admin user and shovel/federation plugins, an admin API token) bypasses
  the egress allowlist. Give the agent a least-privilege account, bridge only the ports it
  needs, and leave admin and management ports off the list. Running the services under Podman on
  an internal network (see `--attach` below) avoids the problem.
* **`host.containers.internal` is the most likely thing to need adjusting.** The bridge must
  reach your host's published ports. If `check` shows WARN while the services are up, set
  `[services].host` to your machine's LAN address (`ip -4 route get 1.1.1.1`, the `src` value).
  Container runtimes usually publish on `0.0.0.0` by default; ports published as
  `127.0.0.1:PORT:PORT` cannot be bridged. A host firewall can also block it.
* The bridge can reach exactly one host and the listed ports. It is not a route to your LAN.
* Ports must be 1024 or higher.
* If you run services under Podman instead, `--attach NAME[:ALIAS]` / `[network].attach`
  connects those containers directly.
* Testcontainers-style tests need a container runtime and will not work inside the sandbox.
  Have the agent write them; run them yourself on the host.

## Tools in the image

`[tools].apt_extra` installs extra Debian packages into the image, for example media tools,
document converters or fonts that your project shells out to. Add packages there and rerun
`./ocbox.py build`. Tools run headless inside the read-only sandbox, and anything that needs a
profile directory writes to the fake `$HOME`. The Debian release in the image follows the .NET
SDK image tag, so package versions can differ slightly from your host's.

## Sandbox notes for the agent

Agents guess when they cannot see their own environment: a blocked host looks like a
"transient proxy error", an empty `.env` looks like a bug. ocbox therefore writes a short
set of notes about the sandbox and attaches them to the agent, read-only.

* Templates live in `sandbox-docs/`: one `skills/<name>/SKILL.md` per topic (network,
  services, filesystem, tools) plus `rules.md`. They are plain markdown with `{{placeholders}}`
  that `ocbox.py` fills from the real session: allowed hosts, which `--allow` profiles are
  open, bridged services, hidden and read-only paths, limits. They cannot go stale.
* `sandbox_docs.py` does the rendering. `ocbox.py` only calls it and adds the mounts.
  The rendered copy goes to `state/session/` on every `run` and is mounted read-only at
  `/etc/ocbox`, and again where the agent looks for skills and for its global rules file.
* The rules file is the "always read" part: a few critical facts and a pointer to
  `/etc/ocbox/README.md`. Skills are loaded on demand and a small model may skip them, so
  the facts that prevent the common mistakes are in the rules file as well.
* `ocbox.py docs PROJECT` renders and prints them so you can review the text.
* Add your own: `sandbox-docs/skills/<name>/SKILL.md` (frontmatter `name` must match the
  directory, one-line `description`, markdown files only). `sandbox-docs/examples/` has a
  `project-workflow` example for build and test commands. Your own global rules go in
  `sandbox-docs/rules.extra.md` and are appended to the rules file.
* Project-derived text (file names) is sanitised before it enters the notes, so a file named
  with newlines and markdown cannot inject instructions into a later session.
* The notes are advisory. They reduce guessing; the mounts and the proxy are the protection.

Which agent it is for is the `[agent]` table in `ocbox.toml`. All keys are optional and the
defaults are for OpenCode. Paths are relative to the agent's home (`/home/agent`):

```toml
[agent]
name         = "opencode"
rules_path   = ".config/opencode/AGENTS.md"
skills_paths = [".config/opencode/skills"]   # may be empty for agents without skills
docs_mount   = "/etc/ocbox"                  # under /etc, /opt, /srv or /mnt
```

## Configuration

Everything is in `ocbox.toml` and commented. The values that ship with it are examples from the
project it was first written for: adjust them to yours. The important parts:

* `[opencode]`: OpenCode **2.x, installed from npm without running npm**. `ocbox build` downloads
  the platform package tarball (`@opencode/cli-linux-x64`, or `-baseline` on CPUs without AVX2,
  or `-linux-arm64`) straight from `registry.npmjs.org`, checks its sha512 integrity against both
  the registry's metadata and the value pinned in `ocbox.lock`, refuses a tarball containing
  anything but `package.json` and the binary, and extracts that one file. The `@opencode/cli`
  launcher package and its `postinstall` script are never fetched or run. The version must be exact
  (for example `2.0.32`), never `latest`/`beta`/`dev`: the `dev` tag publishes a new build every
  few hours. The Linux integrity values for the shipped version are pre-seeded in `ocbox.lock`:
  verify them yourself from another machine before trusting them.
  The old 1.x GitHub-asset path was removed (1.x is being discontinued).
* `[tools]`: .NET SDK (`auto` = from `global.json`), Node, pnpm, extra apt packages. `pnpm` accepts
  `11.22.0` or `pnpm@11.22.0`. Match these to your project's own version files.
* `[limits]`: memory, CPUs, pids, `/tmp` size. `/tmp` is RAM-backed and counts against the memory
  limit.
* `[allow]`: egress profiles. `base` is always on. The hosts listed are educated guesses:
  use `--learn` to confirm them.
* `[hide]`, `[backup]`, `[review]`: the lists discussed above.
* `[services]`: the host services to bridge into the sandbox (see Services). Leave
  `[services.ports]` empty when the agent does not need them.
* `[network].subnet`/`proxy_ip`: change if the range collides with something on your machine.

## Upgrading OpenCode

1. Pick an exact stable version from `npm view @opencode/cli versions` (ignore `0.0.0-*` builds),
   ideally one that has been out a couple of weeks, and read its release notes.
2. Bump `[opencode].version`.
3. `./ocbox.py build`. The first time, it records the registry's integrity value for that version
   in `ocbox.lock`. A published npm version can never legitimately change, so every later build
   must match. To pin it *before* building, add the key yourself, e.g.
   `"npm:@opencode/cli-linux-x64@<version>": "sha512-..."` (take the value from
   `npm view @opencode/cli-linux-x64@<version> dist.integrity` on another machine or network).
4. `./ocbox.py check`, then commit `ocbox.toml` and `ocbox.lock`.

Autoupdate is off in three places (environment variable, config, read-only root-owned binary).

## Version-controlling this directory

Commit: `ocbox.py`, `sandbox_docs.py`, `sandbox-docs/`, `proxy.py`, `bridge.py`, `ocbox-entry.sh`,
`Containerfile`, `ocbox.toml`, `ocbox.lock`, `README.md`, `.gitignore`. The `.gitignore` should
exclude `state/` (OpenCode login, caches, downloads), `backups/` (copies of your secrets),
`.build/` and `__pycache__/`. Don't put this directory inside the project it protects: `ocbox`
refuses to run if they overlap.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `check` fails "no direct internet" | The network is not actually `--internal`. `./ocbox.py cleanup --networks`, then retry |
| `check` fails "proxy allows listed host" | Host machine cannot reach the allowed host, or DNS on your host is odd. Test `curl https://models.dev` |
| Network creation fails (subnet overlaps) | Change `[network].subnet` and `proxy_ip` |
| Fixed IP / alias errors | `podman info` should show `netavark`; if it says `cni`, upgrade Podman or switch the backend |
| OpenCode hangs at startup | It is probably waiting on a blocked host. Run with `--learn` once and read the report |
| `permission denied` writing in `node_modules`/`bin`/`obj` | First-use ownership of a new volume. `./ocbox.py wipe --volumes` and retry |
| Tool installs fail ("connection refused/403") | Add the registry profile: `--allow nuget,npm,pypi` |
| Node's `fetch` ignores the proxy | Built-in `fetch` only honours proxy variables on recent Node (24, and newer 22.x); `NODE_USE_ENV_PROXY=1` is already set. If your Node is older, those calls fail closed. Other tools use the proxy variables normally |
| OOM kills / strange build crashes | Raise `[limits].memory`; check `pids` |
| "illegal instruction" starting OpenCode | Older CPU: set `[opencode].variant = "baseline"` and rebuild |
| `a session seems to be running` | Crashed earlier: `./ocbox.py cleanup` |
| Browser-based OAuth login to a provider fails | Callback ports are not published. Use API keys instead |

## Not supported in this setup (by design)

Running Docker/Podman or `docker compose` from inside the sandbox; browser-based end-to-end
tests (browsers are not installed, and matching them to your lockfile is project specific: add
them via `apt_extra` or a custom step if you need them); pushing to git remotes;
`opencode serve` / the web UI (no published ports; this was the vulnerable surface in earlier
releases); anything that needs your real home directory, SSH agent or GPG keys.
You can unlock individual things later, consciously, one flag at a time.

## Going further

* **gVisor (`runsc`)**: set it as the runtime for the agent container for a user-space kernel
  between the agent and your host. Closest thing to VM isolation without a VM. Expect slower
  file and syscall performance, which matters most for builds and package installs.
* **Dedicated user account** (or spare machine) for the sandbox if you ever feed it anything you
  care about. A breakout then lands in an account with no access to your real home directory.
* **A small VM** instead of bare rootless Podman, if you have the memory to spare: the agent's
  blast radius becomes the VM, which you can snapshot and throw away.
* **Copy-on-write project**: Podman can mount the project with an overlay (`:O`) so nothing
  touches the real tree until you accept it. Not implemented here; the snapshot/restore flow is
  the simpler equivalent. Working on a clone and importing the result as a reviewed patch is
  the stricter variant of the same idea.
