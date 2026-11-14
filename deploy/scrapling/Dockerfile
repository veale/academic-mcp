FROM ghcr.io/d4vinci/scrapling:0.5.1@sha256:b4e92cde7e7e2d9f5940e90ee796cbf7340843a6de7213c0c409fe3d2aadfd87
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && /app/.venv/bin/patchright install chromium \
    && rm -rf /var/lib/apt/lists/*
COPY server.py legacy-schemas.json healthcheck.py /opt/scrapling/
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD ["/app/.venv/bin/python", "/opt/scrapling/healthcheck.py"]
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/app/.venv/bin/python", "/opt/scrapling/server.py"]
CMD []
