# syntax=docker/dockerfile:1.7
#
# Buzz Asterisk 22.11.0, built from the official source tarball.
#
#   docker compose build            (from infra/asterisk/)
#
# Stage 1 builds a minimal module set (modules.list) into /staging.
# Stage 2 is a slim Debian runtime with only the shared libraries those
# binaries link against, running as the unprivileged "asterisk" user.

# Pin by digest after the first build: debian:trixie-slim@sha256:... (verify)
ARG DEBIAN_IMAGE=debian:trixie-slim

###############################################################################
FROM ${DEBIAN_IMAGE} AS build

ARG ASTERISK_VERSION=22.11.0
# From https://downloads.asterisk.org/pub/telephony/asterisk/releases/asterisk-22.11.0.sha256
# and confirmed against a fresh download on 19 Oct 2026.
ARG ASTERISK_SHA256=3bd5ee040509a3d3cd9b1ba9520c18e6ec0a7e7981ca68c457dcd36ba3c54d94
# Optional extra check: the detached signature asterisk-22.11.0.tar.gz.asc is
# made by key F2FC93DB7587BD1FB49E045A5D984BE337191CE7 (fingerprint read from
# the signature packet; owner not yet confirmed - verify). Set VERIFY_GPG=1 to
# fetch the key from keys.openpgp.org and check the signature as well.
ARG ASTERISK_GPG_FINGERPRINT=F2FC93DB7587BD1FB49E045A5D984BE337191CE7
ARG VERIFY_GPG=0

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl bzip2 patch pkg-config file \
      gnupg dirmngr \
      libedit-dev libjansson-dev libxml2-dev uuid-dev libsqlite3-dev \
      libssl-dev libcurl4-openssl-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src
RUN set -eux; \
    base="https://downloads.asterisk.org/pub/telephony/asterisk/releases"; \
    curl -fsSLo asterisk.tar.gz "${base}/asterisk-${ASTERISK_VERSION}.tar.gz"; \
    echo "${ASTERISK_SHA256}  asterisk.tar.gz" | sha256sum -c -; \
    if [ "${VERIFY_GPG}" = "1" ]; then \
      curl -fsSLo asterisk.tar.gz.asc "${base}/asterisk-${ASTERISK_VERSION}.tar.gz.asc"; \
      export GNUPGHOME="$(mktemp -d)"; \
      gpg --batch --keyserver hkps://keys.openpgp.org --recv-keys "${ASTERISK_GPG_FINGERPRINT}"; \
      gpg --batch --verify asterisk.tar.gz.asc asterisk.tar.gz; \
    fi; \
    mkdir asterisk; \
    tar -xzf asterisk.tar.gz --strip-components=1 -C asterisk

WORKDIR /usr/src/asterisk
COPY modules.list /usr/src/modules.list

# Bundled pjproject (2.17) is downloaded by configure and checked against the
# md5 file shipped inside the verified tarball (third-party/pjproject/).
RUN set -eux; \
    ./configure \
      --prefix=/usr --sysconfdir=/etc --localstatedir=/var \
      --libdir=/usr/lib \
      --with-pjproject-bundled \
      --with-jansson-bundled=no; \
    make menuselect.makeopts; \
    enable_args=""; \
    for m in $(grep -Ev '^[[:space:]]*(#|$)' /usr/src/modules.list); do \
      enable_args="${enable_args} --enable ${m}"; \
    done; \
    menuselect/menuselect \
      --disable-category MENUSELECT_ADDONS \
      --disable-category MENUSELECT_APPS \
      --disable-category MENUSELECT_BRIDGES \
      --disable-category MENUSELECT_CDR \
      --disable-category MENUSELECT_CEL \
      --disable-category MENUSELECT_CHANNELS \
      --disable-category MENUSELECT_CODECS \
      --disable-category MENUSELECT_FORMATS \
      --disable-category MENUSELECT_FUNCS \
      --disable-category MENUSELECT_PBX \
      --disable-category MENUSELECT_RES \
      --disable-category MENUSELECT_TESTS \
      --disable-category MENUSELECT_UTILS \
      --disable-category MENUSELECT_AGIS \
      --disable-category MENUSELECT_CORE_SOUNDS \
      --disable-category MENUSELECT_MOH \
      --disable-category MENUSELECT_EXTRA_SOUNDS \
      --disable BUILD_NATIVE \
      ${enable_args} \
      menuselect.makeopts; \
    menuselect/menuselect --check-deps menuselect.makeopts; \
    make -j"$(nproc)"; \
    make install DESTDIR=/staging; \
    rm -rf /staging/etc/asterisk/* /staging/usr/include; \
    find /staging -type f \( -name '*.so' -o -name '*.so.*' -o -path '*/sbin/*' \) \
      -exec strip --strip-unneeded {} + 2>/dev/null || true

# Work out the runtime packages from what the binaries actually link against.
RUN set -eux; \
    mkdir -p /staging/usr/share/buzz; \
    find /staging -type f \( -name '*.so' -o -name '*.so.*' -o -path '*/sbin/*' \) -print0 \
      | xargs -0 ldd 2>/dev/null \
      | awk '$2 == "=>" && $3 ~ /^\// { print $3 }' \
      | sort -u | xargs -r readlink -f | sort -u \
      | xargs -r dpkg -S 2>/dev/null | cut -d: -f1 | sort -u \
      > /staging/usr/share/buzz/runtime-packages.txt; \
    cat /staging/usr/share/buzz/runtime-packages.txt; \
    test -s /staging/usr/share/buzz/runtime-packages.txt

###############################################################################
FROM ${DEBIAN_IMAGE} AS runtime

ARG ASTERISK_UID=2000
ARG ASTERISK_GID=2000
ENV DEBIAN_FRONTEND=noninteractive

LABEL org.opencontainers.image.title="buzz-asterisk" \
      org.opencontainers.image.version="22.11.0-buzz1" \
      org.opencontainers.image.description="Asterisk 22.11.0 for Buzz (ARI + chan_websocket + PJSIP)" \
      org.opencontainers.image.licenses="GPL-2.0-only"

COPY --from=build /staging/usr/share/buzz/runtime-packages.txt /tmp/runtime-packages.txt
# tzdata: GotoIfTime with Africa/Lusaka. gettext-base: envsubst. iproute2: wait for 172.30.0.1.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      $(cat /tmp/runtime-packages.txt) \
      ca-certificates tzdata gettext-base iproute2 procps; \
    rm -rf /var/lib/apt/lists/* /tmp/runtime-packages.txt; \
    groupadd --system --gid "${ASTERISK_GID}" asterisk; \
    useradd --system --uid "${ASTERISK_UID}" --gid asterisk \
      --home-dir /var/lib/asterisk --no-create-home --shell /usr/sbin/nologin asterisk

COPY --from=build /staging/ /

COPY config/ /opt/buzz/asterisk/templates/
COPY entrypoint.sh healthcheck.sh /usr/local/bin/

RUN set -eux; \
    mkdir -p /etc/asterisk /var/lib/asterisk-db /var/spool/asterisk/buzz-messages \
             /var/log/asterisk /var/run/asterisk /var/lib/asterisk/sounds/buzz \
             /opt/buzz/asterisk/local; \
    chown -R asterisk:asterisk /etc/asterisk /var/lib/asterisk-db /var/spool/asterisk \
             /var/log/asterisk /var/run/asterisk; \
    chmod 0750 /etc/asterisk /var/spool/asterisk/buzz-messages; \
    chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/healthcheck.sh; \
    /usr/sbin/asterisk -V

USER asterisk
WORKDIR /var/lib/asterisk

# Documentation only: the container uses network_mode: host.
EXPOSE 5060/udp 8088/tcp 10000-20000/udp

HEALTHCHECK --interval=30s --timeout=15s --start-period=60s --retries=3 \
  CMD ["/usr/local/bin/healthcheck.sh"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["run"]
