"""
ocbox sandbox docs: renders the agent-neutral notes that explain the sandbox to the agent.

ocbox.py calls prepare() once per session. It reads templates from sandbox-docs/, fills in
the placeholders from the real session configuration (allowed hosts, bridged services,
hidden paths, limits, ...), writes the result to a session directory and returns the podman
mount arguments that attach it read-only to the agent container.

Layout of the templates directory:

    skills/<name>/SKILL.md   one directory per skill (markdown only, frontmatter required)
    rules.md                 short always-on text for the agent's global rules file
    rules.extra.md           optional, appended to rules.md (your own global rules)
    examples/                ignored by the renderer; copy things out of here to use them

Layout of the rendered output, which is what the agent sees:

    README.md                generated index of the skills
    rules.md                 rendered rules text (also mounted as the agent's rules file)
    skills/<name>/SKILL.md   rendered skills (also mounted where the agent looks for skills)

Everything is mounted read-only, so the agent cannot rewrite its own instructions.
Standard library only. This module never talks to podman.
"""
import re
import shutil
from pathlib import Path, PurePosixPath

# Everything that differs between agents lives here, and can be overridden in the
# [agent] table of ocbox.toml. Paths are relative to the agent's home (/home/agent).
DEFAULT_AGENT = {
    "name": "opencode",
    "rules_path": ".config/opencode/AGENTS.md",
    "skills_paths": [".config/opencode/skills"],
    "docs_mount": "/etc/ocbox",
}

ALLOWED_MOUNT_ROOTS = {"etc", "opt", "srv", "mnt"}
PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
SKILL_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._/@+~ -]")
LIST_LIMIT = 40

REGISTRY_LABELS = {
    "nuget": "NuGet (dotnet restore)",
    "npm": "npm registry (pnpm install)",
    "pypi": "PyPI (pip install)",
    "github": "GitHub (git over HTTPS, release downloads)",
}

TOOLS_MISSING = [
    "Docker, Podman and any other container runtime (so no Testcontainers)",
    "`sudo`, root, and system package installs (`apt` cannot run here)",
    "Browsers and Playwright",
    "SSH keys, GPG keys and your real home directory",
]


class DocsError(Exception):
    """Anything wrong with the templates or the [agent] configuration."""


# agent settings

def _check_relative(value, label):
    if not isinstance(value, str) or not value.strip():
        raise DocsError(f"[agent].{label} must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or re.search(r"[:,\s]", value):
        raise DocsError(f"[agent].{label} = {value!r} must be a plain path relative to the "
                        "agent's home directory (no '..', ':', ',' or spaces)")
    return str(path)


def agent_settings(cfg):
    """Merge [agent] from ocbox.toml over the defaults and validate it."""
    raw = cfg.get("agent", {})
    unknown = set(raw) - set(DEFAULT_AGENT)
    if unknown:
        raise DocsError(f"[agent]: unknown key(s): {', '.join(sorted(unknown))}")
    agent = {**DEFAULT_AGENT, **raw}

    if not isinstance(agent["name"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", agent["name"]):
        raise DocsError("[agent].name must be lowercase letters, digits, '-', '_' or '.'")
    agent["rules_path"] = _check_relative(agent["rules_path"], "rules_path")
    paths = agent["skills_paths"]
    if not isinstance(paths, list):
        raise DocsError("[agent].skills_paths must be a list (it may be empty)")
    agent["skills_paths"] = [_check_relative(p, "skills_paths") for p in paths]

    mount = agent["docs_mount"]
    parts = PurePosixPath(mount).parts if isinstance(mount, str) else ()
    if (len(parts) < 3 or parts[0] != "/" or parts[1] not in ALLOWED_MOUNT_ROOTS
            or ".." in parts or re.search(r"[:,\s]", mount)):
        raise DocsError(f"[agent].docs_mount = {mount!r} must be an absolute path like "
                        f"/etc/ocbox (first directory one of: {', '.join(sorted(ALLOWED_MOUNT_ROOTS))})")
    agent["docs_mount"] = str(PurePosixPath(mount))
    return agent


# placeholder values

def _clean(text):
    """Make project-derived text safe to put into trusted instructions.

    File names come from the project, which the agent can write to. Without this, a
    file named with newlines and markdown could smuggle text into every later session.
    """
    return UNSAFE_CHARS.sub("?", str(text))


def _code(text):
    return f"`{_clean(text)}`"


def _bullets(items, empty, limit=LIST_LIMIT):
    items = list(items)
    if not items:
        return f"- {empty}"
    lines = [f"- {item}" for item in items[:limit]]
    if len(items) > limit:
        lines.append(f"- ... and {len(items) - limit} more")
    return "\n".join(lines)


def build_context(cfg, agent, *, domains, profiles, learn, hidden, isolated, readonly):
    """Everything the templates may refer to, as {placeholder: text}."""
    limits = cfg.get("limits", {})
    tools = cfg.get("tools", {})
    named = cfg.get("services", {}).get("ports", {})

    if learn:
        network_mode = ("learn mode. Every public HTTPS host is reachable and logged, "
                        "because the user is supervising this session.")
        allowed_hosts = "- every public HTTPS host (learn mode)"
    else:
        network_mode = "strict allowlist. Only the hosts listed below are reachable."
        allowed_hosts = _bullets((_code(d) for d in domains), "no hosts at all")
        if any(d.startswith(".") for d in domains):
            allowed_hosts += "\n\nA leading dot means the domain and all of its subdomains."

    registry_lines = []
    for name in cfg.get("allow", {}):
        if name == "base":
            continue
        label = REGISTRY_LABELS.get(name, _clean(name))
        if learn or name in profiles:
            registry_lines.append(f"{label}: OPEN")
        else:
            registry_lines.append(f"{label}: CLOSED (the user can restart the session "
                                  f"with `--allow {_clean(name)}`)")

    service_lines = [
        f"{_code(name)}: " + ", ".join(f"localhost:{port}" for port in ports)
        + f" (also reachable by the host name {_code(name)})"
        for name, ports in named.items()
    ]
    if named:
        services_rule = ("- The services on localhost (" + ", ".join(_clean(n) for n in named)
                         + ") are real development services. Do not drop, truncate or "
                         "overwrite their data unless the user explicitly asks.")
    else:
        services_rule = "- No host services are connected in this session."

    sdk = tools.get("dotnet_sdk", "auto")
    dotnet = (".NET SDK (the version the project's global.json asks for)" if sdk == "auto"
              else f".NET SDK {_clean(sdk)}")
    installed = [
        dotnet,
        f"Node.js {_clean(tools.get('node', 'unknown version'))}",
        f"pnpm {_clean(str(tools.get('pnpm', 'unknown version')).removeprefix('pnpm@'))}",
        "git",
        "Python 3",
        "ripgrep (`rg`)",
        f"the agent itself ({agent['name']})",
    ]
    extra = tools.get("apt_extra", [])
    if extra:
        installed.append("system packages: " + ", ".join(_clean(p) for p in extra))

    return {
        "agent_name": agent["name"],
        "docs_dir": agent["docs_mount"],
        "network_mode": network_mode,
        "allowed_hosts": allowed_hosts,
        "registries": _bullets(registry_lines, "no registries are configured"),
        "services": _bullets(service_lines, "No host services are connected in this session."),
        "services_rule": services_rule,
        "hidden_count": str(len(hidden)),
        "hidden_paths": _bullets(
            (f"{_code(rel)} ({'directory' if is_dir else 'file'})" for rel, is_dir in hidden),
            "nothing is hidden"),
        "readonly_paths": _bullets((_code(r) for r in readonly), "no paths are read-only"),
        "isolated_count": str(len(isolated)),
        "isolated_examples": _bullets((_code(r) for r in isolated), "none", limit=12),
        "tools_installed": _bullets(installed, "unknown"),
        "tools_missing": _bullets(TOOLS_MISSING, "unknown"),
        "mem_limit": _clean(limits.get("memory", "unknown")),
        "cpu_limit": _clean(limits.get("cpus", "unknown")),
        "pid_limit": _clean(limits.get("pids", "unknown")),
        "tmp_size": _clean(limits.get("tmp_size", "unknown")),
    }


# rendering

def render_text(text, ctx, source):
    """Replace {{name}} placeholders in one pass. Unknown names are an error, so a typo
    in a template is caught here and not discovered by the agent."""
    unknown = sorted({m.group(1) for m in PLACEHOLDER.finditer(text)} - set(ctx))
    if unknown:
        raise DocsError(f"{source}: unknown placeholder(s): {', '.join(unknown)}. "
                        f"Available: {', '.join(sorted(ctx))}")
    return PLACEHOLDER.sub(lambda m: ctx[m.group(1)], text)


def parse_frontmatter(text, source):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise DocsError(f"{source}: SKILL.md must start with a '---' frontmatter block")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise DocsError(f"{source}: the frontmatter block is not closed with '---'") from None
    fields = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields[key.strip()] = value.strip()
    return fields


def check_skill(dir_name, text, source):
    """Validate the frontmatter of a rendered SKILL.md and return its description."""
    fields = parse_frontmatter(text, source)
    name = fields.get("name", "")
    description = fields.get("description", "")
    if name != dir_name:
        raise DocsError(f"{source}: frontmatter name {name!r} must equal the directory name {dir_name!r}")
    if not SKILL_NAME.fullmatch(name) or len(name) > 64:
        raise DocsError(f"{source}: skill name {name!r} must be lowercase letters, digits and "
                        "single hyphens, at most 64 characters")
    if not description or len(description) > 1024:
        raise DocsError(f"{source}: description must be one line of 1 to 1024 characters")
    return description


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")


def _render_skill(src, dest, ctx):
    if not (src / "SKILL.md").is_file():
        raise DocsError(f"{src}: missing SKILL.md")
    description = None
    for path in sorted(src.rglob("*")):
        if path.is_symlink():
            raise DocsError(f"{path}: symlinks are not allowed in templates")
        if path.is_dir():
            continue
        if path.suffix != ".md":
            raise DocsError(f"{path}: only .md files are allowed inside a skill")
        rel = path.relative_to(src)
        text = render_text(path.read_text(encoding="utf-8"), ctx, str(path))
        if rel == Path("SKILL.md"):
            description = check_skill(src.name, text, str(path))
        _write(dest / rel, text)
    return description


def _index(agent, ctx, described):
    docs = agent["docs_mount"]
    lines = [
        "# Notes about this sandbox",
        "",
        "You are running inside an ocbox sandbox. It limits what you can reach and change. "
        "These notes were written for you by the tool that started the sandbox. They are "
        "read-only and are regenerated at the start of every session, so they describe "
        "this session exactly.",
        "",
        f"Network mode this session: {ctx['network_mode']}",
        "",
        "Each skill below is a plain markdown file. Read the one that matches your task "
        f"before you start. The short version of the rules is in {docs}/rules.md.",
        "",
        "Skills:",
    ]
    lines += [f"- `{docs}/skills/{name}/SKILL.md`: {description}" for name, description in described]
    return "\n".join(lines)


def _fix_modes(out_dir):
    out_dir.chmod(0o755)
    for path in out_dir.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)


def render_docs(templates_dir, out_dir, ctx, agent):
    """Render the templates into out_dir (wiped first). Returns [(skill name, description)]."""
    templates_dir, out_dir = Path(templates_dir), Path(out_dir)
    skills_src, rules_src = templates_dir / "skills", templates_dir / "rules.md"
    if not skills_src.is_dir() or not rules_src.is_file():
        raise DocsError(f"{templates_dir} must contain a skills/ directory and a rules.md file")
    if re.search(r"[:,]", str(out_dir)):
        raise DocsError(f"{out_dir}: podman cannot mount a path containing ':' or ','")

    shutil.rmtree(out_dir, ignore_errors=True)
    (out_dir / "skills").mkdir(parents=True)
    described = []
    for skill_dir in sorted(skills_src.iterdir()):
        if skill_dir.name.startswith(".") or not skill_dir.is_dir():
            continue
        if skill_dir.is_symlink():
            raise DocsError(f"{skill_dir}: symlinks are not allowed in templates")
        description = _render_skill(skill_dir, out_dir / "skills" / skill_dir.name, ctx)
        described.append((skill_dir.name, description))
    if not described:
        raise DocsError(f"{skills_src} contains no skills")

    rules = render_text(rules_src.read_text(encoding="utf-8"), ctx, str(rules_src))
    extra = templates_dir / "rules.extra.md"
    if extra.is_file():
        rules = rules.rstrip("\n") + "\n\n" + render_text(extra.read_text(encoding="utf-8"), ctx, str(extra))
    _write(out_dir / "rules.md", rules)
    _write(out_dir / "README.md", _index(agent, ctx, described))
    _fix_modes(out_dir)
    return described


# entry points used by ocbox.py

def prepare(cfg, templates_dir, out_dir, *, domains, profiles, learn, hidden, isolated, readonly):
    """Render everything for one session. Returns (agent settings, [(skill, description)])."""
    agent = agent_settings(cfg)
    ctx = build_context(cfg, agent, domains=domains, profiles=profiles, learn=learn,
                        hidden=hidden, isolated=isolated, readonly=readonly)
    return agent, render_docs(templates_dir, out_dir, ctx, agent)


def mounts(agent, out_dir):
    """podman arguments that attach the rendered notes, read-only, to the agent container.

    One canonical copy at docs_mount (the path the rules text points to), plus the same
    files where this particular agent looks for skills and for its global rules.
    """
    out = Path(out_dir)
    if re.search(r"[:,]", str(out)):
        raise DocsError(f"{out}: podman cannot mount a path containing ':' or ','")
    args = ["-v", f"{out}:{agent['docs_mount']}:ro"]
    for rel in agent["skills_paths"]:
        args += ["-v", f"{out / 'skills'}:/home/agent/{rel}:ro"]
    args += ["-v", f"{out / 'rules.md'}:/home/agent/{agent['rules_path']}:ro"]
    return args
