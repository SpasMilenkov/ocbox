# ocbox sandbox image: .NET SDK + Node + pnpm + Python + OpenCode.
# Built by `ocbox build` (it passes the build args and stages the verified
# OpenCode tarball into the build context). Don't `podman build` this by hand.

ARG DOTNET_SDK_TAG
FROM mcr.microsoft.com/dotnet/sdk:${DOTNET_SDK_TAG}

ARG NODE_DIST_PATH
ARG PNPM_VERSION
ARG APT_EXTRA=""
ARG OPENCODE_SHA256

ENV DEBIAN_FRONTEND=noninteractive

#  OS packages (git/rg/jq are used constantly by agents)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq less make procps ripgrep socat unzip xz-utils zip \
      python3 python3-pip python3-venv python-is-python3 ${APT_EXTRA} \
 && rm -rf /var/lib/apt/lists/*

#  Node: official tarball, verified against nodejs.org's SHASUMS256.txt -
RUN set -eux; \
    case "$(uname -m)" in x86_64) narch=x64;; aarch64) narch=arm64;; *) echo "unsupported arch"; exit 1;; esac; \
    base="https://nodejs.org/dist/${NODE_DIST_PATH}"; \
    cd /tmp; \
    curl -fsSLO "$base/SHASUMS256.txt"; \
    file="$(grep -o "node-v[0-9.]*-linux-${narch}\.tar\.xz" SHASUMS256.txt | head -1)"; \
    curl -fsSLO "$base/$file"; \
    grep " ${file}\$" SHASUMS256.txt | sha256sum -c -; \
    tar -xJf "$file" -C /usr/local --strip-components=1 --no-same-owner; \
    rm -f "$file" SHASUMS256.txt; \
    node --version

#  pnpm (pinned major from ocbox.toml; install scripts disabled)
RUN npm install -g --ignore-scripts "pnpm@${PNPM_VERSION}" && pnpm --version

#  OpenCode: binary fetched + integrity-checked by `ocbox build` (from npm, no npm run)
# Re-verified here, installed root-owned in /usr/local/bin so the agent cannot replace it.
ARG OPENCODE_SHA256
COPY opencode /usr/local/bin/opencode
RUN set -eux; \
    echo "${OPENCODE_SHA256}  /usr/local/bin/opencode" | sha256sum -c -; \
    chown root:root /usr/local/bin/opencode; \
    chmod 755 /usr/local/bin/opencode; \
    opencode --version

#  Entrypoint: localhost -> service bridge forwards (see ocbox-entry.sh)
COPY ocbox-entry.sh /usr/local/bin/ocbox-entry
RUN chown root:root /usr/local/bin/ocbox-entry && chmod 755 /usr/local/bin/ocbox-entry

#  Runtime defaults --
# The real HOME is a bind mount (state/home); this directory is only a fallback.
RUN mkdir -p /home/agent /work && chmod 1777 /home/agent /work

ENV HOME=/home/agent \
    OPENCODE_DISABLE_AUTOUPDATE=true \
    DOTNET_NOLOGO=1 \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    DOTNET_CLI_USE_MSBUILD_SERVER=0 \
    MSBUILDDISABLENODEREUSE=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/ocbox-entry"]
CMD ["opencode"]
