# Separate from the control-plane image; runtime dependencies remain offline.
FROM python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
RUN rm -f /etc/apt/sources.list.d/* \
    && printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20250901T000000Z trixie main\n' > /etc/apt/sources.list \
    && printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99snapshot \
    && apt-get update \
    && apt-get install --yes --no-install-recommends \
        git=1:2.47.3-0+deb13u1 \
        ca-certificates=20250419 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
