---
name: project-workflow
description: Use when building, testing or validating this project, and before reporting that work is done. Lists the commands that make up the project's merge gates and which of them can run in this sandbox.
---

# Project workflow

This file is an example. The renderer ignores the examples directory. To use it, copy it into skills/ and edit it:

    cp -r sandbox-docs/examples/project-workflow sandbox-docs/skills/

## Backend

- Build: `dotnet build Backend/AlexandriaBackend.slnx`. The first restore needs the NuGet registry open (see the sandbox-network skill).
- Unit tests: `dotnet test Backend/Alexandria.Tests.Unit`.
- Integration tests need a container runtime and cannot run here. Say so in your answer instead of skipping them silently.

## Frontend

Add the frontend commands here, and note which registry each one needs open.

## Before you say you are done

State which of the commands above you ran, and which you could not run and why.
