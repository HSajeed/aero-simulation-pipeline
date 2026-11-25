# Python side of the pipeline (Phase 10): Ubuntu 26.04 + pinned OpenVSP .deb + pip deps.
# OpenFOAM is NOT in this image: aero drives the host's Podman through its socket and every
# OpenFOAM stage runs in the pinned opencfd/openfoam-default:2412 image, exactly as on the host.
# Build: make image   (needs vendor/OpenVSP-3.57.0-Ubuntu-26.04_amd64.deb, see README)
FROM docker.io/library/ubuntu:26.04

ARG OPENVSP_DEB=OpenVSP-3.57.0-Ubuntu-26.04_amd64.deb
ENV DEBIAN_FRONTEND=noninteractive

COPY vendor/${OPENVSP_DEB} /tmp/
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv podman make ca-certificates \
      desktop-file-utils `# needed by the OpenVSP .deb postinst` \
      /tmp/${OPENVSP_DEB} \
 && rm -rf /var/lib/apt/lists/* /tmp/${OPENVSP_DEB}

# podman inside the image is only a client of the host's Podman service (socket mounted at run time)
RUN mv /usr/bin/podman /usr/bin/podman.real \
 && printf '#!/bin/sh\nexec /usr/bin/podman.real --remote --url "${CONTAINER_HOST:-unix:///run/podman/podman.sock}" "$@"\n' \
      > /usr/bin/podman && chmod +x /usr/bin/podman

COPY pyproject.toml /src/
COPY src /src/src
COPY templates /src/templates
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir \
      /opt/OpenVSP/python/openvsp_config /opt/OpenVSP/python/utilities \
      /opt/OpenVSP/python/degen_geom /opt/OpenVSP/python/openvsp \
 && /opt/venv/bin/pip install --no-cache-dir -e "/src[dev]"

ENV PATH=/opt/venv/bin:$PATH
# Editable install: code and templates/ come from /src (this image); configs/ and runs/ from the
# mounted repo (working directory).
CMD ["aero", "--help"]
