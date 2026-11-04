# syntax=docker/dockerfile:1.7
# Attendance app.
#
# Build:
#   docker build -f examples/attendance/Dockerfile \
#       -t opennvr/attendance:local-build .
#
# Small on purpose: the app polls snapshots and calls KAI-C, so it needs
# neither ffmpeg nor OpenCV. The faces are found by the adapter.

FROM python:3.11-slim AS runtime

WORKDIR /app

RUN pip install --no-cache-dir \
        "httpx>=0.27,<1.0" \
        "PyYAML>=6.0,<7.0"

COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir /opt/opennvr-app-sdk

COPY examples/attendance/attendance_app.py   ./attendance_app.py
COPY examples/attendance/attendance          ./attendance
COPY examples/attendance/config.example.yml  ./config.example.yml
COPY examples/attendance/config.docker.yml   ./config.docker.yml

ENV PYTHONPATH=/app

ENTRYPOINT ["python", "attendance_app.py"]
CMD ["--config", "config.yml"]
