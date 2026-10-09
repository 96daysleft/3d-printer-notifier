# 3D Printer Notifier

Sends Discord notifications for your 3D printers: print started, progress
milestones, finished, errors. A camera snapshot is attached when one is available.
One process watches any number of printers, of any supported type, over each
printer's local WebSocket API. No cloud account is needed.

| `type` | Printer | Protocol | Default port |
|---|---|---|---|
| `u1` | Snapmaker U1 | Moonraker JSON-RPC (Klipper) | 80 |
| `cc1` | Elegoo Centauri Carbon | SDCP v3 | 3030 |

This project replaces the separate `u1-notifier` and `cc1-notifier` projects.

## What gets notified

| Event | U1 | CC1 | Toggle |
|---|---|---|---|
| Printer connected (on every reconnect) | ✅ | ✅ | `onConnect` |
| Current status after connecting | ✅ | ✅ | always |
| Print started | ✅ | ✅ | `onPrintStart` |
| Progress milestones (default 25/50/75 %) | ✅ | ✅ | `onProgress`, `progressMilestones` |
| Print finished | ✅ | ✅ | `onPrintFinish` |
| Print cancelled / error, firmware errors | ✅ Klipper shutdown/disconnect, `!!` G-code errors | ✅ PrintInfo error codes, SDCP errors, failed commands | `onError` |
| Printer notices | – | ✅ SDCP notices | always |
| Other state changes (pause, resume, calibrating…) | ✅ | ✅ | always |

Each Discord message is sent with the **printer's name as the sender**, and
the footer shows the printer model. You can always tell which printer an alert
came from. Alerts are buffered and sent every `pollIntervalMinutes`. A
printer's first status is sent a few seconds after it connects.

## Configuration

Configuration is a YAML file: `config.yaml`, `$CONFIG_FILE`, or the first
CLI argument. Keys can be camelCase or snake_case. Any value can reference
environment variables as `${VAR}` or `${VAR:-default}`, so you can keep
secrets out of the file.

```yaml
discord:
  webhookUrl: ${DISCORD_WEBHOOK_URL}

notify:                       # defaults for every printer
  progressMilestones: [25, 50, 75]

printers:
  - name: U1 Left             # unique; used as the Discord sender name
    type: u1
    host: 192.168.1.50
  - name: U1 Right
    type: u1
    host: 192.168.1.51
    apiKey: ${U1_RIGHT_API_KEY}   # only if Moonraker requires one
  - name: Centauri
    type: cc1
    host: 192.168.1.60
    discordWebhookUrl: ${CENTAURI_WEBHOOK}   # its own channel
    notify:
      onProgress: false                      # per-printer override
```

See [config.example.yaml](config.example.yaml) for every option.

### Global settings

| Key | Default | Description |
|---|---|---|
| `discord.webhookUrl` | – | Default webhook. Required unless every printer sets its own |
| `discord.username` | `3D Printer Notifier` | Sender name for service-level messages |
| `notify.*` | all `true` | Default event toggles (see table above) |
| `notify.progressMilestones` | `[25, 50, 75]` | Progress percentages to announce |
| `notify.withSnapshot` | `true` | Attach a camera frame to alerts |
| `pollIntervalMinutes` | `5` | How often buffered alerts are sent |
| `maxAlertsPerPoll` | `5` | Detailed alerts per Discord message (1–9). The rest are counted in the summary |
| `logLevel` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. The `LOG_LEVEL` env var overrides it |
| `logFormat` | `console` | `console` or `json` |
| `healthPort` | `8080` | Serves `GET /healthz`. `0` disables it |

### Printer settings

Common to every type:

| Key | Description |
|---|---|
| `name` | Unique display name (required) |
| `type` | `u1`, `cc1`, … (required) |
| `host` | IP or hostname (required) |
| `port` | Override the type's default port |
| `enabled` | `false` keeps the printer in the config without monitoring it |
| `discordWebhookUrl` | Send this printer's alerts somewhere other than `discord.webhookUrl` |
| `camera.snapshotUrl` | JPEG snapshot or MJPEG stream. Defaults to the type's built-in camera URL |
| `notify.*` | Override any global `notify` setting for this printer |

Type-specific settings:

| Type | Key | Description |
|---|---|---|
| `u1` | `apiKey` | Moonraker API key. Only needed if the notifier's network isn't a trusted client. Sent as the `X-Api-Key` header |
| `cc1` | `mainboardId` | SDCP mainboard ID. Optional: when empty, it is discovered over UDP port 3000 at connect time and written to the log, so you can pin it |

Default camera URLs:
- `u1`: `http://<host>/server/files/camera/monitor.jpg`
- `cc1`: `http://<host>:3031/video` (MJPEG; the first frame is used)

## Running

### Kubernetes (Helm)

The chart is published to GHCR as an OCI artifact:

```bash
helm install printers oci://ghcr.io/96daysleft/charts/3d-printer-notifier \
  -f examples/values-multi-printer.yaml
```

In the chart's [values.yaml](helm/3d-printer-notifier/values.yaml), the
config keys (`discord`, `notify`, `printers`, …) are top-level values. The
chart renders them into `config.yaml` and stores it in a Secret.

To keep webhooks and keys out of your values:
- Write `${VAR}` references in values and provide the variables with
  `extraEnv` / `extraEnvFrom`, for example from a Secret synced from OpenBao.
  See [examples/values-multi-printer.yaml](examples/values-multi-printer.yaml).
- Or set `existingSecret` to a Secret that contains a complete `config.yaml`.

There is also an Argo CD `Application` example in
[examples/argocd-application.yaml](examples/argocd-application.yaml).

The chart always runs a single replica, because more would duplicate every
notification. `/healthz` is wired up as the liveness probe. It returns 200
while the process is up, even when a printer is powered off.

### Docker

```bash
cp config.example.yaml config.yaml   # edit it
cp .env.example .env                  # values for ${VAR} references
docker compose up -d
```

### Local development

```bash
./run.sh                                  # venv + install + run with ./config.yaml
venv/bin/pytest                           # tests (the Helm tests need `helm` on PATH)
```

## Adding a printer type

Each printer type implements the `PrinterClient` interface in
[printer_notifier/printers/base.py](printer_notifier/printers/base.py). The
base class handles connecting, reconnecting with backoff, heartbeats and alert
buffering. A new type provides:

1. A config model that subclasses `PrinterConfig`, with a `type: Literal["<name>"]`
   field and any protocol-specific fields.
2. A `PrinterClient` subclass decorated with `@register_printer_type`. It
   implements `url`, `on_open()` (subscribe or request status) and
   `handle_message()` (parse a message and call `self.emit(Alert(...))`).
   Optionally it can override `default_snapshot_url`, `connect_kwargs()`,
   `before_connect()` and `send_heartbeat()`.
3. An import in [printer_notifier/printers/\_\_init\_\_.py](printer_notifier/printers/__init__.py).

Config validation, Helm values, Discord formatting and multi-instance support
then work without any further changes. Use [u1](printer_notifier/printers/u1/__init__.py)
and [cc1](printer_notifier/printers/cc1/__init__.py) as references.

## Releasing

Pushing a `vX.Y.Z` tag runs the [release workflow](.github/workflows/release.yml),
which:
- builds the Python package and creates a GitHub release
- pushes `ghcr.io/96daysleft/3d-printer-notifier:{X.Y.Z, X.Y, latest}`
- pushes the Helm chart, with `version` and `appVersion` set to `X.Y.Z`, to
  `oci://ghcr.io/96daysleft/charts`

## License

MIT
