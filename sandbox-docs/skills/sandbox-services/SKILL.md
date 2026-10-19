---
name: sandbox-services
description: Use before touching Postgres, RabbitMQ, Garage, S3 or any other database, queue or storage service, and when a connection to localhost fails or is refused. Explains which services are connected and what is off limits.
---

# Host services

Services connected to this sandbox:

{{services}}

They run outside the sandbox, and the connection is a plain TCP relay to the user's machine. They hold real development data, not a copy.

## Rules

- Do not drop, truncate or overwrite data, and do not run destructive migrations, unless the user explicitly asked for that in this session.
- Prefer reading. For writes, use the project's own migrations and tooling.
- Credentials for these services may be hidden from you on purpose. Do not search for them, guess them, or recreate hidden files. If you need one, ask the user.
- You cannot start, stop or inspect containers here. There is no Docker or Podman. Tests that need Testcontainers will not run. Write them if asked, but say they were not run.

## When a connection fails

- "Connection refused" or a timeout usually means the service is not running on the user's machine, or is not published on that port. It is not something to fix from inside the sandbox.
- Report which service and port failed, and stop retrying.
