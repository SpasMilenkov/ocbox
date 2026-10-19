#!/usr/bin/env python3
"""
ocbox - run the OpenCode agent inside a locked-down, rootless Podman sandbox.

    ocbox.py build [PROJECT]                  build the image
    ocbox.py run [PROJECT] [--allow ...]      snapshot, then start a sandboxed session
    ocbox.py check                            self-test the sandbox (run this first!)
    ocbox.py docs [PROJECT] [--allow ...]     render and show the notes the agent will see
    ocbox.py backups [PROJECT]                list snapshots
    ocbox.py restore [STAMP] [--to DIR]       restore a snapshot
    ocbox.py wipe / cleanup                   housekeeping

Standard library only. Needs Python 3.11.4+ and Podman. Read README.md first.
Configuration lives in ocbox.toml.
"""
import argparse
import base64
import fnmatch
import hashlib
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

if sys.version_info < (3, 11) or not hasattr(tarfile, "data_filter"):
    sys.exit("ocbox needs Python 3.11.4 or newer (tomllib + safe tar extraction).")
import tomllib  # noqa: E402  (imported after the version check on purpose)

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "ocbox.toml"
LOCK_FILE = ROOT / "ocbox.lock"
STATE = ROOT / "state"
HOME_DIR = STATE / "home"          # the agent's fake $HOME (OpenCode auth + caches)
EMPTY_DIR = STATE / "empty"        # bind-mounted read-only over hidden directories
BACKUP_DIR = ROOT / "backups"      # NEVER mounted into a container
BUILD_DIR = ROOT / ".build"
DOCS_TEMPLATES = ROOT / "sandbox-docs"      # templates for the notes the agent sees
SESSION_DIR = STATE / "session"             # rendered notes, mounted read-only into the agent
CHECK_SESSION_DIR = STATE / "session-check" # same, for `check`

sys.path.insert(0, str(ROOT))
try:
    import sandbox_docs  # noqa: E402  (renders the agent's notes, see sandbox_docs.py)
except ImportError as exc:
    sys.exit(f"ocbox: cannot import sandbox_docs.py from {ROOT} ({exc})")

IMAGE = "ocbox:latest"
INTERNAL_NET = "ocbox-internal"    # agent lives here: no route to the internet
EGRESS_NET = "ocbox-egress-net"    # normal network, only the proxy joins it
PROXY_NAME = "ocbox-egress"
BRIDGE_NAME = "ocbox-bridge"
AGENT_NAME = "ocbox-agent"
PROXY_PORT = 3128

DRY_RUN = False


# small helpers

def die(msg):
    print(f"ocbox: error: {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg):
    print(f"[ocbox] {msg}", flush=True)


def warn(msg):
    print(f"[ocbox] WARNING: {msg}", file=sys.stderr, flush=True)


def podman(*args, check=True, capture=False, query=False):
    """Run `podman ARGS`.

    capture=True : capture stdout/stderr and return them on the result.
    query=True   : the exit code IS the answer (e.g. `network exists`), never fatal.
    In --dry-run mode nothing is executed; the command is printed instead.
    """
    cmd = ["podman", *[str(a) for a in args]]
    if DRY_RUN:
        print("  + " + shlex.join(cmd))
        return subprocess.CompletedProcess(cmd, 1 if query else 0, "", "")
    if query:
        capture = True
    try:
        result = subprocess.run(cmd, capture_output=capture, text=True)
    except FileNotFoundError:
        die("podman is not installed (sudo apt install podman uidmap)")
    if check and not query and result.returncode != 0:
        detail = (result.stderr or "").strip()
        die(f"command failed ({result.returncode}): {shlex.join(cmd)}\n{detail}")
    return result


def load_config():
    if not CONFIG_FILE.exists():
        die(f"{CONFIG_FILE} not found")
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


def read_jsonc(path):
    """Read JSON that may contain // or /* */ comments and trailing commas."""
    text = path.read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(^|\s)//.*$", r"\1", text, flags=re.M)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return json.loads(text)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def slug_of(project):
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", project.name).strip("-").lower()[:30] or "project"


def rel_join(base, name):
    return f"{base}/{name}" if base else name


def matches_any(rel, patterns):
    """Match a project-relative path against fnmatch patterns.
    Patterns without '/' are matched against the file name only."""
    name = rel.rsplit("/", 1)[-1]
    for p in patterns:
        target = rel if "/" in p else name
        if fnmatch.fnmatch(target, p):
            return True
    return False


# project resolution + safety checks

def resolve_project(cfg, arg, required=True):
    value = arg or cfg["project"].get("path", "")
    if not value:
        if required:
            die("no project given. Pass a path, or set [project].path in ocbox.toml")
        return None
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() if arg else ROOT) / p
    return p.resolve()


def validate_project(project):
    home = Path.home().resolve()
    if not project.is_dir():
        die(f"{project} is not a directory")
    if project == Path("/") or project == home or project in home.parents:
        die(f"refusing to mount {project}: it is your home directory (or above it)")
    for name in (".ssh", ".gnupg", ".aws", ".kube", ".docker", ".config", ".mozilla"):
        if (project / name).exists():
            die(f"refusing to mount {project}: it contains '{name}', "
                "which looks like a home directory, not a project")
    if project == ROOT or project in ROOT.parents or ROOT in project.parents:
        die("ocbox's own directory (and its backups) must not overlap the project")


# build

def dotnet_sdk_tag(cfg, project):
    want = cfg["tools"]["dotnet_sdk"]
    if want != "auto":
        return want
    if project is None:
        die("tools.dotnet_sdk = 'auto' needs a project: ocbox.py build PROJECT "
            "(or set an explicit SDK tag in ocbox.toml)")
    gj = project / cfg["tools"]["global_json"]
    if not gj.exists():
        die(f"{gj} not found; set tools.global_json or an explicit tools.dotnet_sdk")
    version = read_jsonc(gj).get("sdk", {}).get("version")
    if not version:
        die(f"{gj} has no sdk.version; set an explicit tools.dotnet_sdk")
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if m and int(m.group(3)) < 100:
        # "10.0.0" is a floor in global.json, not a real SDK build (real ones are
        # 10.0.100, 10.0.101, ...), so no image tag with that name exists.
        tag = f"{m.group(1)}.{m.group(2)}"
        info(f"global.json sdk.version {version} is not a published SDK build; using image tag '{tag}'")
        return tag
    return version


def node_dist_path(version):
    # "22" -> newest 22.x ; "22.11.0" -> exactly that
    return f"v{version}" if version.count(".") >= 2 else f"latest-v{version}.x"


def read_lock():
    return json.loads(LOCK_FILE.read_text()) if LOCK_FILE.exists() else {}


def write_lock(data):
    LOCK_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


NPM_REGISTRY = "https://registry.npmjs.org"
EXPECTED_MEMBERS = {"package/package.json", "package/bin/opencode"}


def cpu_has_avx2():
    try:
        return bool(re.search(r"(^|\s)avx2(\s|$)", Path("/proc/cpuinfo").read_text(), re.M))
    except OSError:
        return False


def opencode_package(cfg):
    """Name of the npm platform package that holds the Linux binary."""
    oc = cfg["opencode"]
    base = oc["package"]
    if not re.fullmatch(r"@opencode/cli", base):
        die(f"[opencode].package must be '@opencode/cli' (got '{base}')")
    machine = platform.machine()
    if machine in ("x86_64", "AMD64"):
        variant = oc.get("variant", "auto")
        if variant == "auto":
            variant = "standard" if cpu_has_avx2() else "baseline"
        if variant not in ("standard", "baseline"):
            die("[opencode].variant must be auto, standard or baseline")
        return f"{base}-linux-x64" + ("-baseline" if variant == "baseline" else "")
    if machine in ("aarch64", "arm64"):
        return f"{base}-linux-arm64"
    die(f"unsupported CPU architecture: {machine}")


def npm_integrity(path):
    """npm-style integrity string (sha512, base64) of a file."""
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha512-" + base64.b64encode(h.digest()).decode()


def fetch_opencode(cfg):
    """Get the pinned OpenCode binary from npm WITHOUT running npm or any install script.

    We download the platform package tarball (e.g. @opencode/cli-linux-x64) ourselves,
    check its sha512 integrity against (a) what the registry advertises and (b) the
    value pinned in ocbox.lock, refuse any tarball with unexpected contents, and extract
    the single binary. Nothing from the package is ever executed on your host.
    Returns (path_to_binary, sha256_of_binary).
    """
    version = cfg["opencode"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        die("[opencode].version must be an exact release like 2.0.32 (not a tag or range)")
    pkg = opencode_package(cfg)
    short = pkg.split("/", 1)[1]
    key = f"npm:{pkg}@{version}"
    out_dir = STATE / "downloads" / version
    tgz = out_dir / f"{short}.tgz"
    binary = out_dir / f"{short}.opencode"
    if DRY_RUN and not binary.exists():
        info(f"(dry run) would fetch {pkg}@{version} from npm and verify it")
        return None, "0" * 64

    meta_url = f"{NPM_REGISTRY}/{pkg.replace('/', '%2f')}/{version}"
    try:
        with urllib.request.urlopen(meta_url, timeout=60) as resp:
            meta = json.load(resp)
    except urllib.error.HTTPError as exc:
        die(f"npm has no {pkg}@{version} (HTTP {exc.code}): {meta_url}")
    advertised = meta["dist"]["integrity"]
    tarball_url = meta["dist"]["tarball"]
    if not tarball_url.startswith(NPM_REGISTRY + "/"):
        die(f"unexpected tarball location from the registry: {tarball_url}")
    hooks = {k for k in (meta.get("scripts") or {}) if k in ("preinstall", "install", "postinstall")}
    if hooks or meta.get("dependencies"):
        warn(f"{pkg}@{version} now declares install scripts/dependencies ({hooks or 'deps'}); "
             "ocbox never runs them, but the package layout changed: review before trusting it")

    lock = read_lock()
    if key in lock and lock[key] != advertised:
        die(f"the registry now advertises a DIFFERENT integrity for {pkg}@{version}!\n"
            f"  ocbox.lock: {lock[key]}\n  registry:   {advertised}\n"
            "A published npm version must never change. Do not continue.")

    out_dir.mkdir(parents=True, exist_ok=True)
    if not tgz.exists():
        info(f"downloading {tarball_url}")
        tmp = tgz.with_name(tgz.name + ".part")
        with urllib.request.urlopen(tarball_url, timeout=300) as resp, open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out)
        tmp.rename(tgz)
    actual = npm_integrity(tgz)
    expected = lock.get(key, advertised)
    if actual != expected:
        tgz.unlink()
        die(f"INTEGRITY MISMATCH for {pkg}@{version}!\n  expected: {expected}\n  got:      {actual}\n"
            "The download was deleted. Do not continue unless you understand why.")
    if key not in lock:
        lock[key] = actual
        write_lock(lock)
        info(f"first use of {pkg}@{version}: recorded {actual[:24]}... in ocbox.lock")

    if not binary.exists():
        with tarfile.open(tgz) as tar:
            names = {m.name for m in tar.getmembers()}
            if names != EXPECTED_MEMBERS:
                die(f"unexpected files in {pkg}@{version}: {sorted(names ^ EXPECTED_MEMBERS)}")
            member = tar.getmember("package/bin/opencode")
            if not member.isreg():
                die("package/bin/opencode is not a regular file")
            tmp = binary.with_name(binary.name + ".part")
            with tar.extractfile(member) as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out)
        os.chmod(tmp, 0o755)
        tmp.rename(binary)
    return binary, sha256_of(binary)


def cmd_build(cfg, args):
    project = resolve_project(cfg, args.project, required=False)
    tools = cfg["tools"]
    sdk_tag = dotnet_sdk_tag(cfg, project)
    node_version = str(tools["node"]).removeprefix("v")
    pnpm_version = str(tools["pnpm"]).removeprefix("pnpm@")  # accept "pnpm@11.22.0" too
    binary, digest = fetch_opencode(cfg)
    version = cfg["opencode"]["version"]

    shutil.rmtree(BUILD_DIR, ignore_errors=True)
    BUILD_DIR.mkdir(parents=True)
    shutil.copy(ROOT / "Containerfile", BUILD_DIR / "Containerfile")
    shutil.copy(ROOT / "ocbox-entry.sh", BUILD_DIR / "ocbox-entry.sh")
    if binary is not None:
        shutil.copy(binary, BUILD_DIR / "opencode")

    info(f"building {IMAGE}: dotnet sdk {sdk_tag}, node {node_version}, "
         f"pnpm {pnpm_version}, opencode {version}")
    podman(
        "build",
        "--build-arg", f"DOTNET_SDK_TAG={sdk_tag}",
        "--build-arg", f"NODE_DIST_PATH={node_dist_path(node_version)}",
        "--build-arg", f"PNPM_VERSION={pnpm_version}",
        "--build-arg", f"APT_EXTRA={' '.join(tools['apt_extra'])}",
        "--build-arg", f"OPENCODE_SHA256={digest}",
        "--label", f"ocbox.opencode={version}",
        "-t", IMAGE, "-t", f"ocbox:oc-{version}",
        "-f", BUILD_DIR / "Containerfile", BUILD_DIR,
    )
    info("done. Next: ocbox.py check")


# project scanning: what to hide, what to isolate

def walk(project, skip_dirs, skip_rels=()):
    """os.walk with pruning. Yields (relative_dir, dirnames, filenames);
    relative_dir uses '/' and is '' for the project root."""
    for dirpath, dirnames, filenames in os.walk(project):
        rel_dir = os.path.relpath(dirpath, project)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in skip_dirs and rel_join(rel_dir, d) not in skip_rels
        )
        yield rel_dir, dirnames, sorted(filenames)


def discover_hidden(project, cfg):
    """Return sorted [(relpath, is_dir)] of things to blank out inside the sandbox."""
    h = cfg["hide"]
    found = {}
    for rel in h["paths"]:
        p = project / rel
        if p.is_symlink():
            warn(f"hide.paths entry '{rel}' is a symlink; cannot mask it, skipping")
        elif p.is_dir():
            found[rel] = True
        elif p.exists():
            found[rel] = False
    hidden_dirs = {r for r, is_dir in found.items() if is_dir}
    for rel_dir, _dirs, files in walk(project, set(cfg["scan"]["skip_dirs"]), hidden_dirs):
        for name in files:
            if matches_any(name, h["globs"]) and not matches_any(name, h["except_globs"]):
                found.setdefault(rel_join(rel_dir, name), False)
    # drop entries nested inside an already-hidden directory
    result = []
    for rel, is_dir in sorted(found.items()):
        if any(rel.startswith(d + "/") for d in hidden_dirs):
            continue
        result.append((rel, is_dir))
    return result


def git_ignored_subset(project, rels):
    """Which of these (not necessarily existing) directories are git-ignored?"""
    if not rels:
        return set()
    if not (project / ".git").exists():
        return set(rels)
    probe = "/.ocbox-probe"
    try:
        r = subprocess.run(
            ["git", "-C", str(project), "check-ignore", "-z", "--stdin"],
            input="\0".join(rel + probe for rel in rels) + "\0",
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return set(rels)
    if r.returncode not in (0, 1):
        return set(rels)  # git could not tell us; assume ignorable build output
    ignored = {x[: -len(probe)] for x in r.stdout.split("\0") if x.endswith(probe)}
    return ignored & set(rels)


def discover_isolated(project, cfg, hidden):
    """node_modules / bin / obj directories that get their own Podman volume.

    Why: code installed or compiled inside the sandbox (node_modules, build output)
    would otherwise land in your real tree and run on the host the next time you
    start the dev server or build. Separate volumes keep the two worlds apart.
    """
    hidden_dirs = {r for r, is_dir in hidden if is_dir}
    candidates = []
    for rel_dir, _dirs, files in walk(project, set(cfg["scan"]["skip_dirs"]), hidden_dirs):
        if "package.json" in files:
            candidates.append(rel_join(rel_dir, "node_modules"))
        if any(f.endswith((".csproj", ".fsproj", ".vbproj")) for f in files):
            candidates += [rel_join(rel_dir, "bin"), rel_join(rel_dir, "obj")]
    ok = git_ignored_subset(project, candidates)
    result = []
    for rel in candidates:
        p = project / rel
        if rel not in ok:
            warn(f"'{rel}' is not git-ignored; not isolating it (agent shares it with the host)")
        elif p.is_symlink() or (p.exists() and not p.is_dir()):
            warn(f"'{rel}' is not a plain directory; not isolating it")
        else:
            result.append(rel)
    return result


def volume_name(slug, rel):
    digest = hashlib.sha1(rel.encode()).hexdigest()[:8]
    return f"ocbox-{slug}-{digest}-{rel.rsplit('/', 1)[-1]}"


def readonly_ancestors(readonly):
    """Directories above the read-only paths, shallowest first (for example '.git').

    A mount point cannot be renamed or deleted, but its parent can. Without this, the
    agent could move '.git' away and create a fresh, writable '.git' with its own hooks
    and config in the same place, and the host would run them later. Mounting the parent
    as well makes it immovable too.
    """
    found = set()
    for rel in readonly:
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            found.add("/".join(parts[:i]))
    return sorted(found, key=lambda r: (r.count("/"), r))


def mount_args(cfg, project, hidden, isolated, readonly):
    """All -v/--tmpfs arguments for the agent container."""
    for rel in [r for r, _ in hidden] + isolated + readonly:
        if ":" in rel:
            die(f"cannot mount/mask the path '{rel}': it contains ':' which podman's -v "
                "syntax cannot express. Rename it.")
    slug = slug_of(project)
    args = ["-v", f"{project}:/work"]
    for rel in readonly_ancestors(readonly):  # parents first, children are mounted after them
        args += ["-v", f"{project / rel}:/work/{rel}"]
    for rel in isolated:
        args += ["-v", f"{volume_name(slug, rel)}:/work/{rel}"]
    for rel, is_dir in hidden:
        if is_dir:
            # An empty read-only bind mount, NOT a tmpfs: runc copies the existing
            # contents of the mountpoint into a new tmpfs by default (tmpcopyup),
            # which would leak small hidden directories to the agent.
            args += ["-v", f"{EMPTY_DIR}:/work/{rel}:ro"]
        else:
            args += ["-v", f"/dev/null:/work/{rel}:ro"]
    for rel in readonly:
        args += ["-v", f"{project / rel}:/work/{rel}:ro"]
    args += ["-v", f"{HOME_DIR}:/home/agent"]
    return args


def existing_readonly(project, cfg):
    out = []
    for rel in cfg["hide"]["readonly"]:
        p = project / rel
        if p.is_symlink():
            warn(f"hide.readonly entry '{rel}' is a symlink; skipping")
        elif p.exists():
            out.append(rel)
    return out


# fake home for the agent

def seed_home():
    for sub in (".config/opencode", ".local/share/opencode", ".local/state/opencode",
                ".cache", ".nuget"):
        (HOME_DIR / sub).mkdir(parents=True, exist_ok=True)
    os.chmod(HOME_DIR, 0o700)
    EMPTY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(EMPTY_DIR, 0o755)
    conf = HOME_DIR / ".config/opencode/opencode.json"
    if not conf.exists():
        conf.write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "autoupdate": False,
            "share": "disabled",
        }, indent=2) + "\n")


def prepare_docs(cfg, out_dir, domains, profiles, learn, hidden, isolated, readonly):
    """Render the notes the agent gets (all logic lives in sandbox_docs.py).
    Returns (agent settings, [(skill, description)], podman mount arguments)."""
    try:
        agent, skills = sandbox_docs.prepare(
            cfg, DOCS_TEMPLATES, out_dir, domains=domains, profiles=profiles, learn=learn,
            hidden=hidden, isolated=isolated, readonly=readonly)
        return agent, skills, sandbox_docs.mounts(agent, out_dir)
    except sandbox_docs.DocsError as exc:
        die(f"sandbox notes: {exc}")


def seed_agent_home(agent):
    """Create the mount points for the notes inside the fake home, so that mounting
    over them never depends on the container runtime creating them."""
    for rel in agent["skills_paths"]:
        (HOME_DIR / rel).mkdir(parents=True, exist_ok=True)
    rules = HOME_DIR / agent["rules_path"]
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.touch(exist_ok=True)


# networking + egress proxy

def ensure_networks(cfg):
    n = cfg["network"]
    # --disable-dns: the network's DNS server forwards unknown names upstream, which would
    # be a covert channel for data (names are sent to whoever runs the domain).
    wanted = ((INTERNAL_NET, ["--internal", "--disable-dns", "--subnet", n["subnet"]]),
              (EGRESS_NET, []))
    for name, extra in wanted:
        if podman("network", "exists", name, query=True).returncode != 0:
            info(f"creating podman network {name}")
            podman("network", "create", *extra, name)
        elif name == INTERNAL_NET and not DRY_RUN:
            result = podman("network", "inspect", name, query=True)
            try:
                dns_enabled = json.loads(result.stdout)[0].get("dns_enabled", True)
            except (ValueError, IndexError, AttributeError):
                die(f"could not read the settings of the network {name}, so ocbox cannot tell "
                    f"whether DNS is off: {(result.stderr or result.stdout).strip()[:300]}")
            if dns_enabled is not False:
                die(f"the network {name} has DNS enabled (it was created by an older ocbox). "
                    "Recreate it: ocbox.py cleanup --networks, then retry")


def resolve_allow(cfg, extra_profiles):
    profiles = cfg["allow"]
    names = ["base"] + list(cfg["run"]["default_profiles"]) + list(extra_profiles)
    domains, seen = [], set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        if name not in profiles:
            die(f"unknown allow profile '{name}'. Available: {', '.join(sorted(profiles))}")
        domains += profiles[name]
    return sorted(set(domains)), list(dict.fromkeys(names))


def stage_script(name):
    """Copy a helper script into state/ with mode 0644.

    The proxy and bridge run as uid 65534, which is an unrelated subuid under rootless
    Podman, so on the host it counts as "other". A restrictive umask on the checkout
    would otherwise make the script unreadable inside the container.
    """
    STATE.mkdir(parents=True, exist_ok=True)
    dest = STATE / name
    shutil.copyfile(ROOT / name, dest)
    os.chmod(dest, 0o644)
    return dest


def start_proxy(cfg, domains, learn):
    n = cfg["network"]
    stop_proxy()
    STATE.mkdir(parents=True, exist_ok=True)
    allow_file = STATE / "allow.txt"
    allow_file.write_text("\n".join(domains) + "\n")
    os.chmod(allow_file, 0o644)
    script = stage_script("proxy.py")
    podman(
        "run", "-d", "--name", PROXY_NAME, "--pull=never",
        "--network", f"{INTERNAL_NET}:ip={n['proxy_ip']}",
        "--network", EGRESS_NET,
        "--read-only", "--cap-drop=all", "--security-opt=no-new-privileges",
        "--memory=256m", "--pids-limit=128", "--user", "65534:65534",
        "-e", f"MODE={'learn' if learn else 'strict'}", "-e", f"PORT={PROXY_PORT}",
        "-v", f"{script}:/proxy/proxy.py:ro",
        "-v", f"{allow_file}:/proxy/allow.txt:ro",
        "--entrypoint", "python3", IMAGE, "/proxy/proxy.py",
    )
    if DRY_RUN:
        return
    for _ in range(40):  # wait up to ~10 s for "proxy up"
        logs = podman("logs", PROXY_NAME, query=True).stdout or ""
        if "proxy up" in logs:
            return
        time.sleep(0.25)
    logs = stop_proxy()
    die("the egress proxy did not start. Container output:\n" + (logs.strip() or "(no output)"))


def stop_proxy():
    """Remove the proxy container and return its log text."""
    logs = ""
    if not DRY_RUN and podman("container", "exists", PROXY_NAME, query=True).returncode == 0:
        result = podman("logs", PROXY_NAME, query=True)
        logs = (result.stdout or "") + (result.stderr or "")
    podman("rm", "-f", "-t", "0", PROXY_NAME, query=True)
    return logs


def summarize_proxy(logs, learn):
    denied, learned = set(), set()
    for line in logs.splitlines():
        verb, _, rest = line.partition(" ")
        rest = rest.strip()
        if not rest:
            continue
        if verb == "DENY":
            denied.add(rest.split(" resolves")[0])
        elif verb == "LEARN":
            learned.add(rest)
    if learn and learned:
        print("\n[ocbox] hosts contacted during this session (candidates for [allow]):")
        for host in sorted(learned):
            print(f"    {host}")
    if denied:
        print("\n[ocbox] connections BLOCKED by the egress allowlist:")
        for host in sorted(denied):
            print(f"    {host}")
        print("        (allow one with: --allow <profile>  or add it to [allow] in ocbox.toml)")


def service_ports(cfg):
    """Validate [services.ports]; return ({name: [ports]}, sorted unique ports)."""
    named = cfg.get("services", {}).get("ports", {})
    owner = {}
    for name, plist in named.items():
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
            die(f"[services.ports]: '{name}' is not a valid host name")
        for port in plist:
            if not isinstance(port, int) or not 1024 <= port <= 65535:
                die(f"[services.ports] {name}: port {port!r} must be a number from 1024 to 65535")
            if port in owner:
                die(f"[services.ports]: port {port} is listed for both '{owner[port]}' and '{name}'")
            owner[port] = name
    return named, sorted(owner)


def start_bridge(cfg):
    """Start the service bridge (if any ports are configured)."""
    _, ports = service_ports(cfg)
    if not ports:
        return
    svc = cfg["services"]
    stop_bridge()
    script = stage_script("bridge.py")
    podman(
        "run", "-d", "--name", BRIDGE_NAME, "--pull=never",
        "--network", f"{INTERNAL_NET}:ip={svc['bridge_ip']}",
        "--network", EGRESS_NET,
        "--read-only", "--cap-drop=all", "--security-opt=no-new-privileges",
        "--memory=128m", "--pids-limit=128", "--user", "65534:65534",
        "-e", f"FORWARD_PORTS={','.join(str(p) for p in ports)}",
        "-e", f"TARGET_HOST={svc['host']}",
        "-v", f"{script}:/bridge/bridge.py:ro",
        "--entrypoint", "python3", IMAGE, "/bridge/bridge.py",
    )
    if DRY_RUN:
        return
    for _ in range(40):
        if "bridge up" in (podman("logs", BRIDGE_NAME, query=True).stdout or ""):
            return
        time.sleep(0.25)
    logs = stop_bridge()
    die("the service bridge did not start. Container output:\n" + (logs.strip() or "(no output)"))


def stop_bridge():
    """Remove the bridge container and return its log text."""
    logs = ""
    if not DRY_RUN and podman("container", "exists", BRIDGE_NAME, query=True).returncode == 0:
        result = podman("logs", BRIDGE_NAME, query=True)
        logs = (result.stdout or "") + (result.stderr or "")
    podman("rm", "-f", "-t", "0", BRIDGE_NAME, query=True)
    return logs


def summarize_bridge(logs, host):
    failed = sorted({m.group(1) for m in re.finditer(r"FAIL port=(\d+)", logs)}, key=int)
    if failed:
        print(f"\n[ocbox] the service bridge could not reach host port(s) {', '.join(failed)} "
              f"on {host}.")
        print("        Is the service running and published on 0.0.0.0 (not only 127.0.0.1)? "
              "See README > Services.")


def attach_services(specs):
    """Connect already-running containers to the agent network for this session."""
    attached = []
    for spec in specs:
        name, _, alias = spec.partition(":")
        cmd = ["network", "connect"]
        if alias:
            cmd += ["--alias", alias]
        result = podman(*cmd, INTERNAL_NET, name, check=False, capture=True)
        if result.returncode != 0:
            warn(f"could not attach '{name}': {(result.stderr or '').strip()} "
                 "(is it a running *podman* container?)")
        else:
            attached.append(name)
            info(f"attached {name}" + (f" as '{alias}'" if alias else ""))
    return attached


def detach_services(names):
    for name in names:
        podman("network", "disconnect", INTERNAL_NET, name, query=True)


# the agent container command line

def agent_args(cfg, project, hidden, isolated, readonly, command, tty=True,
               extra_env=(), attach_specs=(), docs_mounts=()):
    L, N, R = cfg["limits"], cfg["network"], cfg["run"]
    proxy = f"http://{N['proxy_ip']}:{PROXY_PORT}"
    aliases = [spec.partition(":")[2] or spec.partition(":")[0] for spec in attach_specs]
    named, fwd_ports = service_ports(cfg)
    bridge_ip = cfg.get("services", {}).get("bridge_ip", "")
    no_proxy = ",".join(["localhost", "127.0.0.1", *aliases, *named, *([bridge_ip] if bridge_ip else [])])
    a = ["run", "--rm", "--name", AGENT_NAME, "--pull=never", "--hostname", "ocbox"]
    if tty:
        a.append("-it")
    a += [
        "--network", INTERNAL_NET,
        "--userns=keep-id", "--user", f"{os.getuid()}:{os.getgid()}",
        "--cap-drop=all", "--security-opt=no-new-privileges", "--read-only",
        "--tmpfs", f"/tmp:rw,exec,nosuid,nodev,size={L['tmp_size']}",
        "--memory", L["memory"], "--memory-swap", L["memory"],
        "--cpus", L["cpus"], "--pids-limit", str(L["pids"]),
    ]
    env = {
        "HOME": "/home/agent",
        "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
        "http_proxy": proxy, "https_proxy": proxy,
        "NO_PROXY": no_proxy, "no_proxy": no_proxy,
        "NODE_USE_ENV_PROXY": "1",  # make Node's built-in fetch honour the proxy
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "GIT_AUTHOR_NAME": R["git_name"], "GIT_COMMITTER_NAME": R["git_name"],
        "GIT_AUTHOR_EMAIL": R["git_email"], "GIT_COMMITTER_EMAIL": R["git_email"],
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "/work",
    }
    if fwd_ports:
        env["OCBOX_FORWARD_PORTS"] = ",".join(str(p) for p in fwd_ports)
        env["OCBOX_BRIDGE_IP"] = bridge_ip
        for name in named:  # /etc/hosts entries, so names work without any DNS
            a += ["--add-host", f"{name}:{bridge_ip}"]
    for k, v in env.items():
        a += ["-e", f"{k}={v}"]
    for name in list(R["pass_env"]) + list(extra_env):
        if name in os.environ:
            a += ["-e", name]  # value comes from your shell, never written to disk
        else:
            warn(f"pass_env: ${name} is not set in your shell; skipping")
    a += mount_args(cfg, project, hidden, isolated, readonly)
    a += list(docs_mounts)  # after the home mount: these sit on paths inside it
    a += ["-w", "/work", IMAGE, *command]
    return a


# snapshots (backups) + change report

def scan_project(project, cfg):
    """Return ({relpath: [size, mtime_ns]}, [skipped_big_files])."""
    b = cfg["backup"]
    max_bytes = b["max_file_mb"] * 1024 * 1024
    files, too_big = {}, []
    for rel_dir, dirnames, filenames in walk(project, set(b["exclude_dirs"]),
                                             set(b["exclude_paths"])):
        names = list(filenames)
        # symlinks to directories show up in dirnames; keep them as symlink entries
        names += [d for d in dirnames if (project / rel_join(rel_dir, d)).is_symlink()]
        for name in names:
            rel = rel_join(rel_dir, name)
            try:
                st = (project / rel).lstat()
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                if st.st_size > max_bytes:
                    too_big.append(rel)
                    continue
            elif not stat.S_ISLNK(st.st_mode):
                continue
            files[rel] = [st.st_size, st.st_mtime_ns]
    return files, too_big


def backup_names(slug=None):
    if not BACKUP_DIR.exists():
        return []
    pat = re.compile(rf"^{re.escape(slug)}-\d{{8}}-\d{{6}}.*\.tar\.gz$" if slug
                     else r"^.+-\d{8}-\d{6}.*\.tar\.gz$")
    return sorted(p.name[: -len(".tar.gz")] for p in BACKUP_DIR.iterdir() if pat.match(p.name))


def snapshot(project, cfg, label=""):
    slug = slug_of(project)
    BACKUP_DIR.mkdir(exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    files, too_big = scan_project(project, cfg)
    name = f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}{label}"
    archive = BACKUP_DIR / f"{name}.tar.gz"
    info(f"snapshot: {len(files)} files -> backups/{archive.name}")
    with tarfile.open(archive, "w:gz") as tar:
        for rel in sorted(files):
            tar.add(project / rel, arcname=rel, recursive=False)
    os.chmod(archive, 0o600)
    manifest = {"project": str(project), "files": files}
    (BACKUP_DIR / f"{name}.manifest.json").write_text(json.dumps(manifest))
    if too_big:
        warn(f"{len(too_big)} file(s) over {cfg['backup']['max_file_mb']} MB were NOT backed up "
             f"(e.g. {too_big[0]})")
    # retention
    names = backup_names(slug)
    for old in names[: max(0, len(names) - cfg["backup"]["keep"])]:
        for suffix in (".tar.gz", ".manifest.json"):
            (BACKUP_DIR / f"{old}{suffix}").unlink(missing_ok=True)
    return name, files


def report_changes(project, cfg, before):
    after, _ = scan_project(project, cfg)
    added = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(k for k in after if k in before and after[k] != before[k])

    def visible(rel):  # hide git's internal bookkeeping, but keep hooks/config
        return not rel.startswith(".git/") or matches_any(rel, [".git/hooks/*", ".git/config"])

    git_internal = sum(1 for r in added + modified + deleted if not visible(r))
    shown = {"added": [r for r in added if visible(r)],
             "modified": [r for r in modified if visible(r)],
             "deleted": [r for r in deleted if visible(r)]}
    total = sum(len(v) for v in shown.values())
    print(f"\n[ocbox] changes since the snapshot: {len(shown['added'])} added, "
          f"{len(shown['modified'])} modified, {len(shown['deleted'])} deleted"
          + (f"  (+{git_internal} files inside .git: commits/index)" if git_internal else ""))
    for label, rels in shown.items():
        for rel in rels[:15]:
            print(f"    {label:9} {rel}")
        if len(rels) > 15:
            print(f"    {label:9} ... and {len(rels) - 15} more")
    always_review = [".env", ".env.*"]  # compose and dev servers load these on the host
    flagged = [r for r in shown["added"] + shown["modified"] + shown["deleted"]
               if matches_any(r, cfg["review"]["patterns"] + always_review)]
    if flagged:
        print("\n[ocbox] REVIEW BEFORE RUNNING ANYTHING ON YOUR HOST. These files execute or "
              "configure things on your machine:")
        for rel in flagged[:40]:
            print(f"    ! {rel}")
        if len(flagged) > 40:
            print(f"    ! ... and {len(flagged) - 40} more")
    elif total:
        print("[ocbox] nothing in the review list changed. Still skim `git diff` before building.")


def protected_rels(hidden, readonly):
    """Every path whose identity must survive a session: the hidden and read-only
    paths, and every directory above them."""
    rels = set()
    for rel in [r for r, _ in hidden] + list(readonly):
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            rels.add("/".join(parts[:i]))
    return sorted(rels)


def fingerprint(project, rels):
    out = {}
    for rel in rels:
        try:
            st = (project / rel).lstat()
            out[rel] = (st.st_dev, st.st_ino)
        except OSError:
            out[rel] = None
    return out


def report_tampering(project, before):
    """Compare protected paths with how they were before the session. A different inode
    means the path was moved or replaced. Returns the list of changed paths."""
    after = fingerprint(project, list(before))
    changed = [rel for rel, ident in before.items() if ident != after[rel]]
    if changed:
        print("\n[ocbox] SECURITY: these protected paths were MOVED OR REPLACED during the session:")
        for rel in changed[:30]:
            print(f"    !! {rel}")
        print("        Unless you changed them yourself, treat this as tampering. Do not run git,")
        print("        builds or scripts in this project until you have inspected them.")
        print("        `ocbox.py restore` rolls the project back to the snapshot.")
    return changed


def safe_extract(tar, dest):
    def keep(member, path):
        try:
            return tarfile.data_filter(member, path)
        except tarfile.FilterError as exc:
            warn(f"skipping unsafe archive member {member.name}: {exc}")
            return None
    tar.extractall(dest, filter=keep)


def cmd_backups(cfg, args):
    project = resolve_project(cfg, args.project, required=False)
    names = backup_names(slug_of(project) if project else None)
    if not names:
        print("no backups found")
        return
    for name in names:
        size = (BACKUP_DIR / f"{name}.tar.gz").stat().st_size / 1e6
        print(f"{name}   {size:8.1f} MB")


def cmd_restore(cfg, args):
    project = resolve_project(cfg, args.project, required=False)
    names = backup_names(slug_of(project) if project else None)
    if not names:
        die("no backups found")
    if args.stamp:
        matches = [n for n in names if args.stamp in n]
        if len(matches) != 1:
            die(f"'{args.stamp}' matches {len(matches)} backups; be more specific "
                "(see: ocbox.py backups)")
        name = matches[0]
    else:
        name = names[-1]
    archive = BACKUP_DIR / f"{name}.tar.gz"
    manifest = json.loads((BACKUP_DIR / f"{name}.manifest.json").read_text())

    if args.to:  # inspect-only extraction, touches nothing else
        dest = Path(args.to).expanduser().resolve()
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as tar:
            safe_extract(tar, dest)
        info(f"extracted {name} to {dest}")
        return

    target = Path(manifest["project"])
    if project and project != target:
        die(f"this backup belongs to {target}, not {project}")
    validate_project(target)
    current, _ = scan_project(target, cfg)
    snap = manifest["files"]
    extra = sorted(set(current) - set(snap))
    changed = sorted(k for k in snap if k in current and snap[k] != current[k])
    missing = sorted(set(snap) - set(current))
    print(f"Restoring {name} into {target}:")
    print(f"  {len(extra)} file(s) created since will be DELETED")
    print(f"  {len(changed)} changed file(s) will be overwritten")
    print(f"  {len(missing)} deleted file(s) will be brought back")
    for rel in (extra + changed)[:10]:
        print(f"    {rel}")
    if input("Type 'yes' to continue: ").strip() != "yes":
        die("aborted")
    snapshot(target, cfg, label="-pre-restore")  # so the restore itself is undoable
    for rel in extra:
        (target / rel).unlink(missing_ok=True)
    with tarfile.open(archive) as tar:
        safe_extract(tar, target)
    info("restored.")


# run

def cmd_run(cfg, args, command):
    project = resolve_project(cfg, args.project)
    validate_project(project)
    if not DRY_RUN and podman("image", "exists", IMAGE, query=True).returncode != 0:
        die("image not built yet: run `ocbox.py build PROJECT` first")
    if not DRY_RUN and podman("container", "exists", AGENT_NAME, query=True).returncode == 0:
        die("a session seems to be running already (or crashed): `ocbox.py cleanup`")

    extra_profiles = [p for chunk in args.allow for p in chunk.split(",") if p]
    domains, profiles = resolve_allow(cfg, extra_profiles)
    hidden = discover_hidden(project, cfg)
    isolated = discover_isolated(project, cfg, hidden)
    readonly = existing_readonly(project, cfg)
    seed_home()
    agent, skills, docs_mounts = prepare_docs(cfg, SESSION_DIR, domains, profiles, args.learn,
                                              hidden, isolated, readonly)
    seed_agent_home(agent)

    info(f"project: {project}")
    info(f"hidden from the agent ({len(hidden)}): "
         + (", ".join(r for r, _ in hidden[:8]) + (" ..." if len(hidden) > 8 else "")
            if hidden else "nothing found"))
    info(f"read-only for the agent: {', '.join(readonly) or 'nothing'}")
    info(f"separate volumes for build output: {len(isolated)} (node_modules/bin/obj)")
    info(f"sandbox notes for the agent: {', '.join(name for name, _ in skills)} "
         f"(at {agent['docs_mount']})")
    if args.learn:
        warn("LEARN MODE: all public HTTPS hosts are allowed and logged. "
             "Use it for short, supervised sessions only.")
    else:
        info(f"egress allowed to {len(domains)} host patterns: {', '.join(domains)}")

    before = {}
    if not args.no_backup and not DRY_RUN:
        _, before = snapshot(project, cfg)
    elif not args.no_backup:
        info("(dry run) would take a snapshot first")

    guard = fingerprint(project, protected_rels(hidden, readonly))
    named, fwd_ports = service_ports(cfg)
    if fwd_ports:
        info("host services bridged to localhost and by name: "
             + ", ".join(f"{n}:{'/'.join(map(str, p))}" for n, p in named.items()))
    ensure_networks(cfg)
    attached = []
    exit_code = 0
    try:
        start_proxy(cfg, domains, args.learn)
        start_bridge(cfg)
        specs = list(cfg["network"]["attach"]) + list(args.attach)
        attached = attach_services(specs)
        argv = agent_args(cfg, project, hidden, isolated, readonly,
                          command or ["opencode"], tty=sys.stdin.isatty(),
                          extra_env=args.env, attach_specs=specs, docs_mounts=docs_mounts)
        try:
            exit_code = podman(*argv, check=False).returncode
        except KeyboardInterrupt:
            exit_code = 130
    finally:
        # Cleanup must finish even if Ctrl-C is pressed again while it runs.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        info("stopping proxy and bridge...")
        logs = stop_proxy()
        bridge_logs = stop_bridge()
        podman("rm", "-f", "-t", "0", AGENT_NAME, query=True)
        detach_services(attached)
        if not DRY_RUN:
            session_log = STATE / "last-session-proxy.log"
            session_log.write_text(logs)
            os.chmod(session_log, 0o600)
        signal.signal(signal.SIGINT, signal.default_int_handler)
    summarize_proxy(logs, args.learn)
    summarize_bridge(bridge_logs, cfg.get("services", {}).get("host", ""))
    if not DRY_RUN:
        info(f"proxy log saved to {STATE / 'last-session-proxy.log'}")
    if report_tampering(project, guard) and exit_code == 0:
        exit_code = 3
    if before:
        report_changes(project, cfg, before)
        print("\n[ocbox] undo everything with: ocbox.py restore")
    sys.exit(exit_code)


# check: verify the sandbox really does what we claim

def host_listening_ports(gateway):
    """TCP ports the host listens on for every interface (or on the gateway address).

    Those are the ports a container could reach through the host's address on the agent
    network. Loopback-only listeners are skipped: the container cannot reach them.
    Returns None when `ss` is not available.
    """
    try:
        out = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True, timeout=10).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    ports = set()
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        addr, _, port = fields[3].rpartition(":")
        if port.isdigit() and addr.strip("[]") in ("*", "0.0.0.0", "::", gateway):
            ports.add(int(port))
    return sorted(ports)


CHECK_SCRIPT = r'''
import json, os, socket, subprocess
PROXY = ("{proxy_ip}", {proxy_port})
res = {{}}

def attempt(fn):
    try:
        return bool(fn())
    except Exception:
        return False

def connect(host):
    s = socket.create_connection(PROXY, timeout=10)
    s.sendall(("CONNECT %s:443 HTTP/1.1\r\nHost: %s\r\n\r\n" % (host, host)).encode())
    line = s.recv(200).split(b"\r\n")[0].decode()
    s.close()
    return line

def can_write(path, mode="w"):
    try:
        with open(path, mode) as f:
            f.write("x")
        return True
    except OSError:
        return False

res["no direct internet"] = not attempt(lambda: socket.create_connection(("1.1.1.1", 443), timeout=4))
res["proxy blocks unlisted host"] = attempt(lambda: "403" in connect("example.com"))
res["proxy allows listed host"] = attempt(lambda: "200" in connect("{allowed_host}"))
res[".env is hidden (empty)"] = attempt(lambda: open("/work/.env").read() == "")
res["hidden directory is empty"] = attempt(lambda: os.listdir("/work/init-output") == [])
res["hidden directory is read-only"] = not can_write("/work/init-output/ocbox-check.txt")
DOCS = "{docs_mount}"
SKILL_PATHS = {skill_paths}
RULES = "{rules_path}"
SKILL_NAMES = {skill_names}
res["sandbox notes present"] = attempt(lambda: os.path.isfile(DOCS + "/README.md") and all(
    os.path.isfile(DOCS + "/skills/" + n + "/SKILL.md") for n in SKILL_NAMES))
res["sandbox notes read-only"] = not can_write(DOCS + "/ocbox-check.txt") and \
    not can_write(DOCS + "/README.md", "a")
res["agent skills path serves the notes"] = attempt(lambda: all(
    os.path.isfile(p + "/" + n + "/SKILL.md") for p in SKILL_PATHS for n in SKILL_NAMES))
res["agent skills path read-only"] = not any(can_write(p + "/ocbox-check.txt") for p in SKILL_PATHS)
res["agent rules file points at the notes"] = attempt(lambda: DOCS in open(RULES).read())
res["agent rules file read-only"] = not can_write(RULES, "a")
def can_rename(a, b):
    try:
        os.rename(a, b)
    except OSError:
        return False
    os.rename(b, a)
    return True

GATEWAY = "{gateway}"
# A random name: podman copies the host's /etc/hosts into the container, so a real
# name can be answered from that file and say nothing about DNS.
PROBE_NAME = "ocbox-probe-" + os.urandom(6).hex() + ".example.net"
res["no DNS resolution"] = not attempt(lambda: socket.gethostbyname(PROBE_NAME))

def nameservers():
    try:
        found = [l.split()[1] for l in open("/etc/resolv.conf") if l.startswith("nameserver")]
    except OSError:
        found = []
    return [n for n in found if ":" not in n]

def udp_route(host):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 53))   # fails with "network unreachable" when there is no route
        return True
    finally:
        sock.close()

def host_ips():
    ips = set()
    try:
        for line in open("/etc/hosts"):
            f = line.split()
            if len(f) > 1 and "host.containers.internal" in f[1:] and ":" not in f[0]:
                ips.add(f[0])
    except OSError:
        pass
    return ips

res["no route for UDP (resolvers, internet)"] = not any(
    attempt(lambda h=h: udp_route(h)) for h in nameservers() + ["1.1.1.1", "9.9.9.9"])
HOST_PORTS = {host_ports}

def open_ports(host, ports):
    from concurrent.futures import ThreadPoolExecutor
    def probe(port):
        try:
            socket.create_connection((host, port), timeout=1.5).close()
            return port
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=32) as pool:
        return [p for p in pool.map(probe, ports) if p is not None]

reach_host = {{h: open_ports(h, HOST_PORTS) for h in sorted(host_ips())}}
res["host machine not reachable directly"] = not any(reach_host.values())

def routes():
    out = []
    try:
        conv = lambda h: socket.inet_ntoa(bytes.fromhex(h)[::-1])
        for line in open("/proc/net/route").read().splitlines()[1:]:
            f = line.split()
            out.append("%s dest=%s gw=%s mask=%s" % (f[0], conv(f[1]), conv(f[2]), conv(f[7])))
    except Exception as exc:
        out.append("unreadable: %s" % exc)
    return out

def resolvers():
    try:
        return [l.strip() for l in open("/etc/resolv.conf") if l.strip() and not l.startswith("#")]
    except OSError as exc:
        return ["unreadable: %s" % exc]

res["__net__"] = {{"resolv.conf": resolvers(), "routes": routes()}}
reach_gateway = open_ports(GATEWAY, HOST_PORTS)
res["nothing listens on the network gateway"] = not reach_gateway
res["__reach__"] = {{"gateway": reach_gateway, "host": reach_host}}
res[".git cannot be renamed or replaced"] = not can_rename("/work/.git", "/work/.git-moved")
res["hidden .env cannot be renamed"] = not can_rename("/work/.env", "/work/.env-moved")
res["root filesystem read-only"] = not can_write("/usr/local/ocbox-test")
res[".git/hooks read-only"] = not can_write("/work/.git/hooks/pre-commit")
res[".git/config read-only"] = not can_write("/work/.git/config", "a")
res["project dir writable"] = can_write("/work/ocbox-check.txt")
res["isolated node_modules writable"] = can_write("/work/node_modules/ocbox-check.txt")
res["isolated bin writable"] = can_write("/work/bin/ocbox-check.txt")
res["home writable"] = can_write("/home/agent/ocbox-check.txt")
res["no ssh keys visible"] = not os.path.exists(os.path.expanduser("~/.ssh"))
for tool in ["opencode --version", "dotnet --version", "node --version",
             "pnpm --version", "python3 --version", "git --version", "rg --version"]:
    res["tool: " + tool.split()[0]] = attempt(
        lambda t=tool: subprocess.run(t.split(), capture_output=True, timeout=60).returncode == 0)
svc = {{}}
for port in {service_ports}:
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.settimeout(2)
        try:
            svc[str(port)] = True if s.recv(1) else None   # b"" = upstream refused/closed
        except socket.timeout:
            svc[str(port)] = True                       # silent = upstream accepted us
        s.close()
    except OSError:
        svc[str(port)] = None
res["__services__"] = svc
print(json.dumps(res))
'''


def cmd_check(cfg, args):
    info("self-test: this starts a real proxy + container with the same flags as `run`")
    out = podman("--version", capture=True).stdout
    print(f"  {out.strip() if out else 'podman'}")
    if not DRY_RUN:
        backend = podman("info", "--format", "{{.Host.NetworkBackend}}", capture=True).stdout.strip()
        print(f"  network backend: {backend}" + ("" if backend == "netavark"
              else "   <-- expected 'netavark'; fixed IPs / aliases may not work"))
        if podman("image", "exists", IMAGE, query=True).returncode != 0:
            die("image not built yet: run `ocbox.py build PROJECT` first")

    proj = STATE / "check-project"
    shutil.rmtree(proj, ignore_errors=True)
    (proj / ".git" / "hooks").mkdir(parents=True)
    (proj / "sub").mkdir()
    (proj / ".env").write_text("SECRET=do-not-leak\n")
    # A small hidden directory with a secret in it. This is the case that a tmpfs
    # mount would have leaked (tmpcopyup copies small directories into the tmpfs).
    (proj / "init-output").mkdir()
    (proj / "init-output" / "creds.env").write_text("SECRET=do-not-leak\n")
    (proj / "package.json").write_text("{}\n")
    (proj / ".gitignore").write_text("node_modules/\nbin/\nobj/\n")
    (proj / "app.csproj").write_text("<Project />\n")
    subprocess.run(["git", "init", "-q", str(proj)], check=False)
    (proj / ".git" / "config").touch()

    base = cfg["allow"]["base"]
    allowed = next((d for d in base if not d.startswith(".")), base[0].lstrip("."))
    domains, profiles = resolve_allow(cfg, [])
    hidden = discover_hidden(proj, cfg)
    isolated = discover_isolated(proj, cfg, hidden)
    readonly = existing_readonly(proj, cfg)
    if "init-output" not in {r for r, is_dir in hidden if is_dir}:
        die("the check expects 'init-output' in [hide].paths; add it or adjust the check")
    seed_home()
    agent, skills, docs_mounts = prepare_docs(cfg, CHECK_SESSION_DIR, domains, profiles, False,
                                              hidden, isolated, readonly)
    seed_agent_home(agent)
    ensure_networks(cfg)
    named, fwd_ports = service_ports(cfg)
    gateway = str(ipaddress.ip_network(cfg["network"]["subnet"], strict=False)[1])
    discovered = host_listening_ports(gateway)
    if discovered is None:
        warn("`ss` is not available: probing only a fixed list of common host ports")
    # Bridged ports are always probed too: a runtime that publishes them without a
    # listening socket (iptables only) would not show up in `ss`.
    host_ports = sorted({22, 80, 443, 2375, *fwd_ports, *(discovered or [])})
    info(f"probing {len(host_ports)} host TCP ports from inside the sandbox")
    script = CHECK_SCRIPT.format(
        proxy_ip=cfg["network"]["proxy_ip"], proxy_port=PROXY_PORT,
        allowed_host=allowed, service_ports=fwd_ports, host_ports=host_ports,
        docs_mount=agent["docs_mount"],
        skill_paths=[f"/home/agent/{p}" for p in agent["skills_paths"]],
        rules_path=f"/home/agent/{agent['rules_path']}",
        skill_names=[name for name, _ in skills],
        gateway=gateway)
    try:
        start_proxy(cfg, domains, learn=False)
        start_bridge(cfg)
        argv = agent_args(cfg, proj, hidden, isolated, readonly,
                          ["python3", "-c", script], tty=False, docs_mounts=docs_mounts)
        result = podman(*argv, check=False, capture=True)
    finally:
        stop_proxy()
        stop_bridge()
        podman("rm", "-f", "-t", "0", AGENT_NAME, query=True)
    if DRY_RUN:
        return
    if result.returncode != 0:
        print(result.stdout, result.stderr)
        die("the test container failed to start; output above")
    try:
        res = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(result.stdout, result.stderr)
        die("could not parse the test container's output")
    owner_ok = (proj / "ocbox-check.txt").exists() and \
        (proj / "ocbox-check.txt").stat().st_uid == os.getuid()
    res["files created in project are owned by you"] = owner_ok
    # What the agent wrote must not have landed in the real hidden directory.
    res["nothing written into hidden directory on host"] = \
        sorted(p.name for p in (proj / "init-output").iterdir()) == ["creds.env"]
    # The notes must name every host the proxy really allows (host side comparison).
    network_note = CHECK_SESSION_DIR / "skills" / "sandbox-network" / "SKILL.md"
    allowed_now = [l.strip() for l in (STATE / "allow.txt").read_text().splitlines() if l.strip()]
    res["notes list every host the proxy allows"] = network_note.is_file() and \
        all(f"`{d}`" in network_note.read_text() for d in allowed_now)
    services = res.pop("__services__", {})
    net = res.pop("__net__", {})
    reach = res.pop("__reach__", {})
    failed = 0
    for name, ok in res.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        failed += 0 if ok else 1
    if net and not res.get("no DNS resolution", True):
        print("  -- DNS leak diagnostics, as seen from inside the sandbox:")
        for line in net.get("resolv.conf", []):
            print(f"       resolv.conf: {line}")
        for line in net.get("routes", []):
            print(f"       route: {line}")
        print("       (a nameserver on the network gateway means the network's DNS server is "
              "still on: ocbox.py cleanup --networks)")
    open_gateway = reach.get("gateway") or []
    open_host = {h: ports for h, ports in (reach.get("host") or {}).items() if ports}
    if open_gateway or open_host:
        print("  -- host ports the sandbox could reach (this list should be empty):")
        if open_gateway:
            print(f"       gateway: {', '.join(map(str, open_gateway))}")
        for h, ports in open_host.items():
            print(f"       {h}: {', '.join(map(str, ports))}")
    if services:
        print("  -- host services (WARN only: they may simply not be running)")
        by_port = {str(p): n for n, plist in named.items() for p in plist}
        for port, ok in services.items():
            label = f"{by_port.get(port, '?')} on localhost:{port}"
            print(f"  [{'PASS' if ok else 'WARN'}] {label}"
                  + ("" if ok else "  (not reachable: service down, or not published on 0.0.0.0?)"))
    shutil.rmtree(proj, ignore_errors=True)
    shutil.rmtree(CHECK_SESSION_DIR, ignore_errors=True)
    print()
    if failed:
        print(f"{failed} check(s) failed. See README.md > Troubleshooting. "
              "Do not use the sandbox for real work until they pass.")
        sys.exit(1)
    print("All checks passed.")


def cmd_docs(cfg, args):
    """Render the agent's notes without starting anything, and show them."""
    extra_profiles = [p for chunk in args.allow for p in chunk.split(",") if p]
    domains, profiles = resolve_allow(cfg, extra_profiles)
    project = resolve_project(cfg, args.project, required=False)
    if project:
        validate_project(project)
        hidden = discover_hidden(project, cfg)
        isolated = discover_isolated(project, cfg, hidden)
        readonly = existing_readonly(project, cfg)
    else:
        hidden, isolated, readonly = [], [], []
        warn("no project given: the notes show no hidden, read-only or isolated paths")
    agent, skills, _ = prepare_docs(cfg, SESSION_DIR, domains, profiles, args.learn,
                                    hidden, isolated, readonly)
    info(f"rendered {len(skills)} skill(s) for the agent '{agent['name']}' into {SESSION_DIR}")
    for name, description in skills:
        print(f"  {name}: {description}")
    print(f"\nIn the sandbox: notes at {agent['docs_mount']}, skills at "
          f"{', '.join('~/' + p for p in agent['skills_paths']) or '(not mounted)'}, "
          f"rules at ~/{agent['rules_path']}. The rules file reads:\n")
    print((SESSION_DIR / "rules.md").read_text())
    print(f"Read everything with:  cat {SESSION_DIR}/README.md {SESSION_DIR}/skills/*/SKILL.md")


# housekeeping

def cmd_wipe(cfg, args):
    if not (args.home or args.volumes or args.all):
        die("nothing to do: pass --home (OpenCode login + caches), "
            "--volumes (node_modules/bin/obj volumes) or --all")
    if args.home or args.all:
        shutil.rmtree(HOME_DIR, ignore_errors=True)
        info("removed state/home (OpenCode login/config and caches)")
    if args.volumes or args.all:
        out = podman("volume", "ls", "--format", "{{.Name}}", capture=True).stdout or ""
        vols = [v for v in out.split() if v.startswith("ocbox-")]
        for v in vols:
            podman("volume", "rm", "-f", v, query=True)
        info(f"removed {len(vols)} volume(s)")


def cmd_cleanup(cfg, args):
    podman("rm", "-f", "-t", "0", AGENT_NAME, PROXY_NAME, BRIDGE_NAME, query=True)
    info("removed leftover session containers")
    if args.networks:
        podman("network", "rm", INTERNAL_NET, EGRESS_NET, query=True)
        info("removed ocbox networks")


# main

def build_parser():
    p = argparse.ArgumentParser(prog="ocbox.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="print the podman commands instead of running them")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("build", help="build the sandbox image")
    s.add_argument("project", nargs="?")

    s = sub.add_parser("run", help="start a sandboxed OpenCode session")
    s.add_argument("project", nargs="?")
    s.add_argument("--allow", action="append", default=[], metavar="PROFILES",
                   help="extra egress profiles, e.g. nuget,npm (repeatable)")
    s.add_argument("--learn", action="store_true",
                   help="allow all HTTPS and log hosts (to discover what to allowlist)")
    s.add_argument("--no-backup", action="store_true", help="skip the pre-session snapshot")
    s.add_argument("--attach", action="append", default=[], metavar="CONTAINER[:ALIAS]",
                   help="also connect this running podman container for the session")
    s.add_argument("--env", action="append", default=[], metavar="NAME",
                   help="pass this host environment variable into the container")
    s.epilog = "Run something else instead of opencode:  ocbox.py run PROJECT -- bash"

    sub.add_parser("check", help="verify the sandbox works as promised")

    s = sub.add_parser("docs", help="render and show the notes the agent will see")
    s.add_argument("project", nargs="?")
    s.add_argument("--allow", action="append", default=[], metavar="PROFILES",
                   help="extra egress profiles, as for run")
    s.add_argument("--learn", action="store_true", help="render as for a learn mode session")

    s = sub.add_parser("backups", help="list snapshots")
    s.add_argument("project", nargs="?")

    s = sub.add_parser("restore", help="restore a snapshot (default: newest)")
    s.add_argument("stamp", nargs="?", help="part of the snapshot name, e.g. 20261018-1530")
    s.add_argument("--project", help="limit to this project")
    s.add_argument("--to", help="only extract into this directory (touches nothing else)")

    s = sub.add_parser("wipe", help="delete local state")
    s.add_argument("--home", action="store_true")
    s.add_argument("--volumes", action="store_true")
    s.add_argument("--all", action="store_true")

    s = sub.add_parser("cleanup", help="remove leftover session containers")
    s.add_argument("--networks", action="store_true", help="also remove ocbox networks")
    return p


def main():
    global DRY_RUN
    argv = sys.argv[1:]
    extra = []
    if "--" in argv:  # everything after -- is the command to run in the container
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    DRY_RUN = args.dry_run
    if os.getuid() == 0 and not DRY_RUN:
        die("do not run ocbox as root. The sandbox relies on rootless podman as a normal user.")
    cfg = load_config()
    try:
        if args.command == "build":
            cmd_build(cfg, args)
        elif args.command == "run":
            cmd_run(cfg, args, extra)
        elif args.command == "check":
            cmd_check(cfg, args)
        elif args.command == "docs":
            cmd_docs(cfg, args)
        elif args.command == "backups":
            cmd_backups(cfg, args)
        elif args.command == "restore":
            cmd_restore(cfg, args)
        elif args.command == "wipe":
            cmd_wipe(cfg, args)
        elif args.command == "cleanup":
            cmd_cleanup(cfg, args)
    except KeyError as exc:
        die(f"ocbox.toml is missing the key {exc}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
