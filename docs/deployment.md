# Reverto Deployment Guide

Productie-deploy opties voor Reverto. Voor dag-tot-dag operationele
procedures (startup, shutdown, credential rotation, emergency stop,
etc.) zie `docs/runbook.md`.

## Bare-metal (huidige default)

De referentie-setup. Portal + bots draaien direct op de host:

```bash
cd ~/reverto
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
make start     # portal naar logs/portal.log
```

Zie `docs/runbook.md` "Startup checklist" voor env-vars en hardening.

## Docker setup (optioneel)

Voor een reproduceerbare deploy of isolatie van de Python stack:

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# System deps voor ccxt / cryptography builds
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps — kopieer requirements eerst zodat image-layers
# cachebaar blijven bij code-only wijzigingen.
COPY requirements.txt requirements-ml.txt ./
RUN pip install --no-cache-dir \
        -r requirements.txt \
        -r requirements-ml.txt

# App code
COPY . .

# Non-root user — voorkomt dat een gecompromitteerde bot met root
# op de host schrijft.
RUN useradd -m -u 1000 reverto && \
    chown -R reverto:reverto /app
USER reverto

# Persistent mounts: state + DB + configs + credentials
VOLUME ["/app/logs", "/app/config/bots"]

EXPOSE 8080

CMD ["python", "main_web.py"]
```

### docker-compose.yml

Volledige stack inclusief Prometheus + Grafana voor monitoring:

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
      # ZET DEZE via een .env bestand of Docker secrets, NIET hier.
      - REVERTO_API_KEY=${REVERTO_API_KEY}
      - REVERTO_SECRET_KEY=${REVERTO_SECRET_KEY}
      # - BITGET_PASSPHRASE=${BITGET_PASSPHRASE}  # alleen voor live bots
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

Zie `docs/prometheus.yml` voor het voorbeeld; op Docker-compose
wordt deze read-only gemount op `/etc/prometheus/prometheus.yml`.

### Security considerations

- **Secrets**: gebruik een `.env` file via `env_file:` OF Docker
  secrets. NOOIT credentials in de image bakken; die blijven in elke
  image-layer tot in eeuwigheid zichtbaar.
- **Firewall**: `/metrics`, `/healthz`, `/readyz` niet publiek exposen —
  alleen Prometheus binnen het stack-netwerk (reverse proxy / ingress
  ACL). De portal zelf mag publiek mits achter TLS.
- **Volumes**: `logs/` (state.json, reverto.db, credentials.json,
  .credentials.key) en `config/bots/` moeten persistent zijn. Zonder
  deze volumes verliest elke rebuild alle bot-state en credentials.
- **Updates**: rolling restart via `docker-compose up -d --build`.
  Reverto's state overleeft container-restart dankzij de volumes; bots
  pikken hun state weer op via StateIO.
- **User**: container draait als non-root (`reverto`, uid 1000). De
  host directories die gemount worden moeten leesbaar/schrijfbaar zijn
  voor uid 1000.

### .env voorbeeld

Maak `.env` naast `docker-compose.yml` (gitignored):

```bash
# Portal authenticatie
REVERTO_API_KEY=<secrets.token_hex(32) uit een nieuwe Python shell>
REVERTO_SECRET_KEY=<secrets.token_hex(32) uit een nieuwe Python shell>

# Alleen voor live-bots met Bitget
BITGET_PASSPHRASE=<jouw Bitget passphrase>

# Grafana admin
GRAFANA_ADMIN_PASSWORD=<iets sterks>
```

### Logs

Docker default log driver is `json-file` met rotatie. Voor productie:

```yaml
services:
  reverto:
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
```

Alternatief: log-aggregator zoals Loki / Fluentd / Vector — scrape
portal.log + per-bot logs vanuit het logs-volume.

## Kubernetes (schets)

Voor K8s: gebruik de Dockerfile als basis en stel manifests op voor
Deployment + Service + ConfigMap + PersistentVolumeClaim + Secret.

Kernpunten:

- Één PVC voor `logs/` + één voor `config/bots/` (RWO is voldoende
  omdat Reverto single-pod draait).
- Liveness probe op `/healthz`, readiness op `/readyz`, met
  `timeoutSeconds: 5` zodat een trage probe niet tot restart leidt
  (StateIO interne 3s DB-timeout zit al in /readyz).
- Prometheus Operator met ServiceMonitor op `/metrics`.
- Emergency-stop workflow via `kubectl exec` naar de portal pod + curl
  naar localhost:8080 — NIET via een publieke endpoint.

Details zijn out-of-scope voor deze guide; de bare-metal + Docker
paden dekken de meeste operationele use cases.
