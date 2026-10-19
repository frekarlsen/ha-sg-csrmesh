# Changelog

## 0.6.3rc1 – 2026-10-19

- `send_on_press` takes `repeats` and `gap`. With `repeats: 1, gap: 0` a switch configuration
  (wake, clear, set, sleep) fits in the switch's ~2 s wake window, as the SG app does it. No pause
  after the last message, and the log shows each message's time after the press.

- `capture` calls that overlap no longer cut each other short (the first one to end used to switch
  logging off for the one still running).
- Trim read and set (max) verified on hardware; see docs/gen1-protocol.md 6.1.
- Options flow keeps every existing option when saving (it used to copy a fixed list of keys, which
  would have deleted any option added later).
- `csrmesh/settings.py`: builders and parsers for trim (`FF29`/`FF2A`/`FF2B`), mode (`FF39`),
  push-button input (`FF47`), fading and dimming speed (`FF48`), firmware (`FF5E`) and the
  acknowledgement `FF08`, plus the app's trim level tables. Mapped from the SG app, **not verified on
  hardware** and not used by the integration yet.
- Tests for v0.6.2 (protocol, settings, config/options flow, light, switch), `pyproject.toml`,
  `requirements-test.txt`, `CLAUDE.md`, `docs/gen1-protocol.md`.

## 0.6.2 – 2026-09-28

First version in git. Lights (dimmer, on/off), Smart Switch buttons as events, status polling and
reports made by hand (`FF50`/`FF88`), move devices from the SG gateway (HomeKit), add new devices
(MASP via recsrmesh), add by address, remove (optionally factory reset), settings (poll interval,
minimum level), capture/send/send_on_press tools, Norwegian translation.
