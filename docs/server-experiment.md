# Linux experiment host

The existing Ubuntu host with two CPUs and four GB RAM is suitable for the current CPU research and shadow worker. No GPU, second server, Docker installation or public application endpoint is required. Keep credentials and state on private persistent disk. Provider charges continue while the existing Droplet is allocated.

## Install the pinned release

Use the reviewed installer from the repository with a full commit SHA. It installs Python dependencies, creates a non-login service user, runs tests as that user, verifies the systemd units and leaves them disabled. It does not create credentials, enable orders or substitute synthetic data for a real release.

```sh
sudo bash scripts/install-shadow-server.sh --commit FULL_REVIEWED_COMMIT_SHA
sudo /opt/dwight/.venv/bin/python /opt/dwight/scripts/server-preflight.py
```

An active worker or dirty/unexpected checkout blocks replacement. Run exactly one worker per campaign. Never let an unattended worker pull the latest code or model. The installed source must match the release hashes. Root owns the application; the service user can write only private state. The installer scopes Git trust to its own verified checkout when running tests.

## Private preparation

Create `/etc/dwight/paper.env` privately, owner root, group dwight, mode 0640, using `.env.example` as the schema. Keep the credentials out of command history. Do not enable shell tracing. The systemd shadow service reads that file; interactive data download commands must receive the same environment through a private mechanism such as `dwight.ops.load_env(Path('/etc/dwight/paper.env'))`. Never source an unreviewed file as shell code.

Download the QQQ history and run real evaluation using the workflow guide, with private data and experiments under `/var/lib/dwight`. Insufficient observations or weak results remain visible; do not lower real sample minimums to force a model. Review a candidate before shadow. For this host, use an absolute stop path `/var/lib/dwight/STOP` in a copied shadow policy before freezing the release. Store the resulting candidate at `/var/lib/dwight/releases/candidate`. Do not edit the policy after freezing.

Prepare the private campaign under `/var/lib/dwight/campaign` with the requested recipients. It must refer to host paths when started. Do not copy an already started campaign to another active worker.

## Enable observation and report generation

```sh
sudo cp deploy/linux/dwight-shadow.service /etc/systemd/system/
sudo cp deploy/linux/dwight-reports.service /etc/systemd/system/
sudo cp deploy/linux/dwight-reports.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/dwight-shadow.service /etc/systemd/system/dwight-reports.service /etc/systemd/system/dwight-reports.timer
sudo systemctl daemon-reload
sudo systemctl enable --now dwight-shadow.service
sudo journalctl -u dwight-shadow.service -n 30 --no-pager
```

During an open session, verify an `observed` heartbeat, then use `campaign-start` as described in the report guide. Only then enable `dwight-reports.timer`. The shadow worker submits no orders. The report timer writes files, not emails. Confirm the timer with `systemctl list-timers dwight-reports.timer` and inspect service logs after the first generated report. A server reboot must preserve all campaign and observation files.

The provided units restart a crashed shadow process. A provider error can leave a running process abstaining, so inspect `dwight health`, recorded errors and external host monitoring. The Codex follow-up will report meaningful observed failures while its host is available; it is not a substitute for server monitoring.

To stop safely, create `/var/lib/dwight/STOP` as the dwight user and stop the shadow unit. Preserve all ledgers. Freeze the model for the entire observation week. Offline improvements can run separately without replacing the observed version.

## Paper execution boundary

For the unfiltered manual baseline, the installer also provides
`dwight-manual-observer.service`. Follow the separate [manual observer
guide](manual-observer.md). It does not require a qualified model, but still
requires working market-data access. A revision halt exits with status 3 and
the unit deliberately does not restart it automatically. The manual observer
does not start the model-shadow campaign or its milestone clock.

The selected account is native TradingView paper, with human order entry and private fill imports. See [the manual journal](manual-paper.md). An Alpaca paper executor is a separate optional route. Before automated paper orders, implement and validate fresh executable quotes, account reconciliation, partial fill protection, entry expiry, session close exits and actual fill accounting. The existing long only bracket library is insufficient for an unattended strategy that also produces short signals. No live account capability is authorized by these deployment steps.
