# Linux experiment host

Recommended starting size: Ubuntu 24.04, two CPUs, four GB RAM, private persistent disk and SSH key access. No GPU or public web endpoint is needed. DigitalOcean lists its Basic regular CPU four GB plan at $24 per month as checked on October 2, 2026: [provider pricing](https://www.digitalocean.com/pricing/droplets). An equivalent existing Linux host is also suitable. No server has been purchased or provisioned by this repository.

## Install the pinned release

The following is a runbook for the chosen host, not evidence of a completed deployment. Use exactly one of systemd or the existing Docker Compose service. Do not run both workers against the same campaign.

```sh
sudo apt-get update
sudo apt-get install -y python3-venv git
sudo useradd --system --home-dir /var/lib/dwight --create-home --shell /usr/sbin/nologin dwight
sudo install -d -m 700 -o dwight -g dwight /var/lib/dwight
sudo install -d -m 750 -o root -g dwight /etc/dwight
sudo git clone https://github.com/neekitalian/dwight-schrute-bot.git /opt/dwight
cd /opt/dwight
sudo git checkout REVIEWED_COMMIT
sudo python3 -m venv .venv
sudo .venv/bin/python -m pip install -r requirements.lock
sudo .venv/bin/python -m pip install --no-deps .
sudo .venv/bin/python -m unittest discover -s tests -q
```

Substitute the reviewed commit. Never let an unattended worker pull latest code or latest models. The installed source must match the release hashes.

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

Alpaca paper is the intended next stage. Before automated paper orders, implement and validate fresh executable quotes, account reconciliation, partial fill protection, entry expiry, session close exits and actual fill accounting. The existing long only bracket library is insufficient for an unattended strategy that also produces short signals. No live account capability is authorized by these deployment steps.
