# Reverto Deployment Guide

Production deployment options for Reverto. For day-to-day
operational procedures (startup, shutdown, credential rotation,
emergency stop, etc.) see `docs/OPERATIONS.md`.

## Bare-metal (current default)

The reference setup. Portal + bots run directly on the host:

```bash
cd ~/reverto
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
make start     # portal output to logs/portal.log
```

See `docs/OPERATIONS.md` "Startup checklist" for env vars and
hardening.

## Docker setup (optional)

For a reproducible deploy or isolation of the Python stack:

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# System deps for ccxt / cryptography builds
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps: copy requirements first so image layers stay
# cacheable across code-only changes.
COPY requirements.txt requirements-ml.txt ./
RUN pip install --no-cache-dir \
        -r requirements.txt \
        -r requirements-ml.txt

# App code
COPY . .

# Non-root user: prevents a compromised bot from writing as root
# on the host.
RUN useradd -m -u 1000 reverto && \
    chown -R reverto:reverto /app
USER reverto

# Persistent mounts: state + DB + configs + credentials
VOLUME ["/app/logs", "/app/config/bots"]

EXPOSE 8080

CMD ["python", "main_web.py"]
```

### docker-compose.yml

Full stack including Prometheus + Grafana for monitoring:

```yaml
services:
  reverto:
    build: .
    ports:
      - "8080:8080"
    volumes:
      - ./logs:/app/logs
      - ./config/bots:/app/config/bots
    environment:
      # SET THESE via a .env file or Docker secrets, NOT here.
      - REVERTO_API_KEY=${REVERTO_API_KEY}
      - REVERTO_SECRET_KEY=${REVERTO_SECRET_KEY}
      # - BITGET_PASSPHRASE=${BITGET_PASSPHRASE}  # only for live bots
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8080/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

  prometheus:
    image: prom/prometheus:latest
    ports:
      - "9090:9090"
    volumes:
      - ./docs/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./docs/alerts.yml:/etc/prometheus/alerts.yml:ro
      - prometheus_data:/prometheus
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.retention.time=30d
    restart: unless-stopped
    depends_on:
      - reverto

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    volumes:
      - grafana_data:/var/lib/grafana
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD:-changeme}
      - GF_USERS_ALLOW_SIGN_UP=false
    restart: unless-stopped
    depends_on:
      - prometheus

volumes:
  prometheus_data:
  grafana_data:
```

### Prometheus scrape config

See `docs/prometheus.yml` for the example; on Docker compose this
is mounted read-only at `/etc/prometheus/prometheus.yml`.

### Security considerations

- **Secrets**: use a `.env` file via `env_file:` OR Docker
  secrets. NEVER bake credentials into the image; they remain
  visible in every image layer forever.
- **Firewall**: do not publicly expose `/metrics`, `/healthz`,
  `/readyz`; only Prometheus inside the stack network (reverse
  proxy / ingress ACL). The portal itself can be public as long
  as it is behind TLS.
- **Volumes**: `logs/` (state.json, reverto.db, credentials.json,
  .credentials.key) and `config/bots/` must be persistent.
  Without these volumes every rebuild loses all bot state and
  credentials.
- **Updates**: rolling restart via `docker-compose up -d --build`.
  Reverto's state survives the container restart thanks to the
  volumes; bots pick up their state again via StateIO.
- **User**: the container runs as non-root (`reverto`, uid 1000).
  The host directories that get mounted must be readable/writable
  by uid 1000.

### .env example

Create `.env` next to `docker-compose.yml` (gitignored):

```bash
# Portal authentication
REVERTO_API_KEY=<secrets.token_hex(32) from a fresh Python shell>
REVERTO_SECRET_KEY=<secrets.token_hex(32) from a fresh Python shell>

# Only for live bots with Bitget
BITGET_PASSPHRASE=<your Bitget passphrase>

# Grafana admin
GRAFANA_ADMIN_PASSWORD=<something strong>
```

### Logs

Docker's default log driver is `json-file` with rotation. For
production:

```yaml
services:
  reverto:
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
```

Alternative: a log aggregator like Loki / Fluentd / Vector,
scraping portal.log + per-bot logs from the logs volume.

## Kubernetes (sketch)

For K8s: use the Dockerfile as the base and write manifests for
Deployment + Service + ConfigMap + PersistentVolumeClaim +
Secret.

Key points:

- One PVC for `logs/` + one for `config/bots/` (RWO is sufficient
  because Reverto runs single-pod).
- Liveness probe on `/healthz`, readiness on `/readyz`, with
  `timeoutSeconds: 5` so a slow probe does not lead to a restart
  (StateIO's internal 3s DB timeout already feeds into /readyz).
- Prometheus Operator with a ServiceMonitor on `/metrics`.
- Emergency-stop workflow via `kubectl exec` into the portal pod
  + curl to localhost:8080, NOT via a public endpoint.

Details are out of scope for this guide; the bare-metal + Docker
paths cover most operational use cases.
