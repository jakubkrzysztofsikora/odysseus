# Per-run sandbox image (P2.1). This is NOT the app image (../Dockerfile) — it is
# the throwaway container that a single bash/python tool call executes inside.
#
# Hardening (enforced at BUILD time here; runtime flags applied by sandbox_runner.py):
#   * DHI (Docker Hardened Image) base — minimal, CVE-scanned, no shell/pkg-mgr
#     surface beyond what the tool needs. The tag below is a placeholder; CI pins
#     the digest of the approved internal DHI mirror (Circit registry).
#   * runs as a dedicated non-root UID (10001), never UID 0.
#   * no setuid/setgid binaries shipped; no package manager left in the final layer.
#
# Runtime hardening (sandbox_runner.py passes these to `docker run`, NOT bakeable):
#   --user 10001:10001  --read-only  --tmpfs /sandbox/scratch
#   --cap-drop ALL  --security-opt no-new-privileges  --network <egress-controlled>
#   (env is stripped — see runner; no ambient host creds reach the container)

# DHI base. CI overrides with the pinned internal digest:
#   --build-arg SANDBOX_BASE=circitregistry.azurecr.io/dhi/python:3.12-nonroot@sha256:...
ARG SANDBOX_BASE=docker.io/library/python:3.12-slim
FROM ${SANDBOX_BASE}

# Non-root sandbox user. UID is fixed so host bind-mount ownership is predictable
# and the runtime --user flag has a real account to map to.
ARG SANDBOX_UID=10001
ARG SANDBOX_GID=10001
RUN groupadd --gid ${SANDBOX_GID} sandbox \
    && useradd --uid ${SANDBOX_UID} --gid ${SANDBOX_GID} --no-create-home --shell /usr/sbin/nologin sandbox

# Scratch + workspace mountpoints. /sandbox/work is the read-write bind for the
# run's workspace; /sandbox/scratch is a tmpfs the runner mounts. Pre-create so a
# --read-only rootfs still has writable, owned mountpoints.
RUN mkdir -p /sandbox/work /sandbox/scratch \
    && chown -R ${SANDBOX_UID}:${SANDBOX_GID} /sandbox

# Strip the package manager and any setuid bits from the final image so a
# compromised tool run cannot self-upgrade or escalate. Best-effort: the DHI
# base may already omit apt; guard with `|| true`.
RUN (apt-get purge -y --auto-remove apt 2>/dev/null || true) \
    && find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

USER ${SANDBOX_UID}:${SANDBOX_GID}
WORKDIR /sandbox/work

# No ENTRYPOINT/long-running process: the runner invokes `docker run ... <argv>`
# per tool call and the container exits when the command completes or is SIGKILLed.
