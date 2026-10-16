FROM agent-workspace:base

RUN microdnf install -y curl tar gzip coreutils \
    && microdnf clean all

ARG TARGETARCH
ARG COPILOT_VERSION=1.0.97
ENV COPILOT_AUTO_UPDATE=false

RUN set -eu; \
    case "$TARGETARCH" in \
        amd64) copilot_arch=x64 ;; \
        arm64) copilot_arch=arm64 ;; \
        *) echo "Unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    archive="copilot-linux-${copilot_arch}.tar.gz"; \
    release="https://github.com/github/copilot-cli/releases/download/v${COPILOT_VERSION}"; \
    curl -fsSL "$release/$archive" -o "/tmp/$archive"; \
    curl -fsSL "$release/SHA256SUMS.txt" -o /tmp/SHA256SUMS.txt; \
    (cd /tmp && grep -F " $archive" SHA256SUMS.txt | sha256sum -c -); \
    tar -xzf "/tmp/$archive" -C /usr/local/bin copilot; \
    rm -f "/tmp/$archive" /tmp/SHA256SUMS.txt; \
    copilot --version