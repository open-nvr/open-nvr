# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
# mDNS announcer for OpenNVR (HA-117). Linux hosts only: it needs
# network_mode: host, which Docker Desktop does not really provide.
FROM python:3.12-slim
RUN pip install --no-cache-dir "zeroconf>=0.132,<1"
COPY announcer.py /app/announcer.py
USER 65534:65534
CMD ["python", "/app/announcer.py"]
