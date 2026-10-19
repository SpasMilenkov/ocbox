---
name: sandbox-network
description: Use before any download, package install, git fetch, API call or other network request, and whenever a request fails with a 403, proxy, tunnel or connection error. Explains what network access exists in this sandbox and what to do when a host is blocked.
---

# Network access in this sandbox

You have no direct route to the internet. Every HTTPS request goes through an egress proxy that only lets listed hosts through. Plain HTTP, other ports and DNS lookups are not available to you.

Mode this session: {{network_mode}}

## Hosts you can reach

{{allowed_hosts}}

## Registries

{{registries}}

## When a request fails

- A 403, "proxy tunnel request failed", or a similar proxy error means the host is blocked by policy. It is not a temporary fault. Retrying, switching tools or trying a mirror will not help.
- Do not try to get around the proxy: no other proxies, tunnels, raw sockets, or relaying through an allowed host.
- Stop that part of the task. Tell the user exactly which host was blocked and what you needed it for. If a registry is CLOSED above, the user can restart the session with the option named there.
- Only call a failure temporary if a retry of the same request actually succeeded, and say that it did.

## Working offline

- `dotnet build` and `dotnet test` can run without the network when the packages are already in the NuGet cache in your home directory. Only a restore that needs a new package fails.
- `pnpm install` needs the npm registry. The `node_modules` directories start empty in this sandbox, so a first install needs it open.
- There are no credentials for git remotes here. Commit locally; the user pushes from their own machine after reviewing the changes.
