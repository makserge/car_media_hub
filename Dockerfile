ARG BUILD_FROM
FROM $BUILD_FROM

# Debian (trixie) base: Chromium comes from Debian's own repository, where the security team ships updates.
# python3-aiohttp / python3-mutagen come from apt (no pip, no compiler, no wheels needed on any architecture).
# 7zip and libarchive-tools (bsdtar) extract .7z and .rar releases; Chromium is the browser of the Review page.
# --no-install-recommends leaves out Chromium's recommended fonts, so the two font packages are named here: freefont (wide
# Latin/Cyrillic/Greek coverage) and liberation (Arial/Times/Courier metrics that most web pages ask for).
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       python3 python3-aiohttp python3-mutagen ca-certificates 7zip libarchive-tools chromium xvfb fonts-freefont-ttf fonts-liberation \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Two unprivileged users without shell. 'browser' (1010) runs Chromium and Xvfb; its home is only its profile folder
# (/data/browser-profile, created and owned at run time). 'extract' (1011) runs 7z/bsdtar on downloaded archives.
# The add-on drops privileges itself (Python's subprocess user=/group=), so no su-exec/setpriv is needed.
RUN groupadd -g 1010 browser \
    && useradd -u 1010 -g browser -M -d /data/browser-profile -s /usr/sbin/nologin browser \
    && groupadd -g 1011 extract \
    && useradd -u 1011 -g extract -M -d /nonexistent -s /usr/sbin/nologin extract \
    && mkdir -p -m 1777 /tmp/.X11-unix

# Chromium's WebRTC may only use the proxy (no direct UDP / STUN / TURN to the LAN); also enforced by the command line
COPY policy/cmh.json /etc/chromium/policies/managed/cmh.json

COPY app/ /app/
COPY config.yaml /app/config.yaml
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ENV PYTHONPATH=/app PYTHONUNBUFFERED=1

CMD ["/run.sh"]
