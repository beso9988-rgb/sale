# Tazkarti monitor

## What this update does

- Watches every team in the public matches list. No team filter is enabled.
- Removes fixed six-second and three-to-six-second waits from the scan path.
- Reuses one browser and uses page readiness instead of `networkidle`.
- Uses a start-to-start cadence, with positive jitter and backoff after errors.
- Keeps a durable Telegram outbox. Failed deliveries remain queued for retry. Delivery is at least once, so a crash immediately after sending can cause a duplicate.
- Saves state atomically and preserves the previous snapshot after partial or unknown scans.
- Stops on HTTP 401/403/429 or a detected verification/block page. The stop is saved as `halt_reason`; restarting the process does not start site requests again.
- Sends distinct changes from the known match-data feeds without the old five-minute suppression. General endpoint discovery is now off unless explicitly enabled.
- Labels public-list changes as signals, not confirmed ticket stock.

## Limits that matter

This is not a prediction system. A change can only be detected after a source exposes it. There is no promise of knowing a minute before release, and no measured seconds-level end-to-end latency yet.

The public Tazkarti label can say Available while every category inside an account is Sold Out. This code does **not** confirm category inventory for every match and does not click through booking dialogs. A listing or JSON change must not be treated as proof that a ticket can be bought. All-match coverage means all cards loaded in the configured list, not a verified crawl of every competition or hidden/paginated fixture.

At an opted-in 10-second cadence, a healthy source change is targeted on the next scan, nominally 0 to 11 seconds later, **plus** page loading, parsing, queue backlog, Telegram delivery and network delay. Slow scans, expired sessions, throttling and outages can make it much longer. No release occurred during testing, and no speed benchmark is claimed.

GitHub Actions still runs every five minutes and can start late. It is not the fast path. Use one continuously running server process for lower latency. Avoid duplicate server processes and disable the scheduled Actions monitor only after verifying the replacement service.

## Review and test before deployment

```sh
python -m unittest -v test_monitor
python -m py_compile monitor_runtime.py tazkarti_watch.py
```

The tests are offline and mock browser/network responses. They do not verify current site selectors, server access, actual Telegram delivery, or real inventory transitions.

## Server configuration

Keep the existing `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in a protected environment file, not in GitHub, source code, logs, or chat. Never upload session files. No Telegram changes are required if the current bot/chat already works, but live delivery still needs a deployment smoke test.

Default configuration:

```ini
TARZKARTI_CONTINUOUS=1
TARZKARTI_POLL_SECONDS=60
```

Only after the account owner explicitly accepts the risk of the server IP or account being throttled/blocked, a deployment may use:

```ini
TARZKARTI_POLL_SECONDS=10
TARZKARTI_FAST_POLL_RISK_ACK=1
```

The acknowledgement is a safety gate, not a claim that 10-second polling is safe or permitted by the site. Do not enable it silently. Values below 10 seconds are rejected. There is no proxy rotation, challenge bypass, or automatic retry after a block.

Optional diagnostics: `TARZKARTI_API_DISCOVERY=1` and `TARZKARTI_SAVE_DEBUG=1`. Leave both off for normal use. Debug output can include page data. An optional `TARZKARTI_STORAGE_STATE` file can restore an existing authorized browser session, but it does not add category-inventory checking. Protect such files with mode 600, never commit them, and never send their contents through chat.

## Linux service checklist

The repository contains older Oracle/Linux paths, but the active provider, unit name, Python environment and application directory must be verified before changing a live server. Do not run the unrelated notification-ingress setup script just to update this monitor; it changes listeners and web-server configuration.

1. Stop the existing monitor and record its service definition, environment-file path and current commit for rollback. Do not alter or print secrets.
2. Back up existing state files and deploy the reviewed branch or patch to the verified application directory. Use the same Python environment that already has Playwright and Chromium, or install those dependencies in a dedicated environment.
3. Run the offline tests. Preserve the existing `seen_tazkarti.json`; the new outbox and known-feed state will be created as needed.
4. Set the confirmed cadence. Keep the working directory stable and writable by the service user, so state survives restarts. Do not run two processes against these files.
5. Configure systemd with `Restart=on-failure` and `RestartPreventExitStatus=78`. A persisted halt still prevents further site requests even if an older service keeps restarting.
6. Start the service. Check that one healthy scan completes and that a deliberately generated test notification reaches the intended Telegram chat, using the existing protected credentials. Do not claim deployment success until both checks pass.
7. Review logs for actual cycle duration and false signals before considering a faster cadence. A rolling positive signal should be checked against the logged-in booking dialog.
8. On a halt, inspect the reason and resolve it manually. Only then remove `halt_reason` and `halt_notified` from the local state and restart. Do not delete the state automatically to resume polling.

The Actions cache now persists listing state, known-feed state and the Telegram outbox between runs. It deliberately does not cache sessions or diagnostic files. The cache namespace changes once for this version, so the first updated Actions run establishes a fresh baseline.

## External advance warnings

A private Telegram channel is not read by this monitor. Manually forwarded channel alerts can trigger a separate authorized check. They remain third-party reports, not proof that tickets are on sale. Neither channel access nor Telegram account credentials are required by this patch.
