# CLAUDE.md – ha-sg-csrmesh (SG Smart Gen1, CSRmesh)

Home Assistant custom integration `sg_csrmesh` for first-generation SG Smart devices (CSRmesh).
Sister project: `frekarlsen/ha-sg-smart-mesh` (SG Smart 3.0, domain `bluetooth_mesh`). Keep the two
looking the same in HA (entity names, categories, translation style).

## Layout

- `custom_components/sg_csrmesh/csrmesh/` – pure protocol (no HA imports): `crypto.py`, `protocol.py`,
  `settings.py` (dimmer settings, switch configuration), `heatreg.py`. New commands go here first, with
  a test built from real captured bytes.
- `models.py` – SG models and which settings each has (drives which entities a device gets).
- `coordinator.py` – link, device list, state, delivery/retry, raw `send` and `capture` tools.
- Entities: `light.py`, `event.py`, `climate.py`, `number.py`, `select.py`, `sensor.py`. Settings stored in the device go under
  `EntityCategory.CONFIG`; pairings, battery and similar under `EntityCategory.DIAGNOSTIC`.
- `docs/` – protocol notes. Every hardware test is written down there with sent and received bytes.

## Rules learned in 3.0 (do not repeat these mistakes)

- **Options:** never copy a fixed list of keys. Always `{**entry.options, **new}`. A fixed list
  deletes settings added later.
- **Wait for the right answer:** SG devices publish their own status on the same opcode as the
  answer. Match on command kind and side/TID, not only on opcode.
- **Ack:** `01` accepted, `02` rejected, silence is not a rejection. Report a rejection at once; on
  silence send again with the same TID (the device drops duplicates).
- **Read back** what the device stored when it can be read. Settings that cannot be read are shown
  with the last value sent, kept in options.
- **Commands without an ack** (pairing) are sent twice with the same TID. HA keeps its own record of
  what it sent and says clearly that it is HA's record.
- **Plan for unreadable devices:** build HA's own record first; add reading from the device only when
  it is proven to work on hardware.
- **One read for many entities:** several entities asking for the same value share one background
  read. Cancel background jobs when the entry unloads.
- Use `name_by_user` from the device registry and follow renames via `EVENT_DEVICE_REGISTRY_UPDATED`.
- Drive which entities a device gets from a product table, so it only gets settings it has.
- User-facing errors: `HomeAssistantError` with `translation_key`.
- Units: check per field (3.0 trim used half-percent for min/start and whole percent for max).

## Gen1 specifics learned on hardware

- A device only obeys messages with its own tag (`e7a4` gateway, `0000` joined by HA). A switch sends
  with its tag, so it can only control lights with the same tag.
- A battery switch listens ~2 s after a press. Send wake/clear/set/sleep back to back (one copy, no
  pause) and check the acks afterwards; resend what is missing at the next press.
- Smart Switch configuration cannot be read back; HA keeps its own record (`pairs` per switch).

## Tests and tooling

- Python 3.14: `uv venv -p 3.14 && uv pip install -r requirements-test.txt`.
  `pytest-homeassistant-custom-component==0.13.346` (HA 2026.7.2). Bluetooth needs its own requirements
  in the venv (bleak, habluetooth, aiousbwatcher, serialx, aioesphomeapi).
- Shared fixtures in `tests/conftest.py`, helpers (`make_entry`, `inject`, `answering`) in
  `tests/helpers.py`. Requests wait 0.05 s in tests (`REQUEST_WAIT` is patched).
- HA uses eager tasks: use `await hass.async_block_till_done(wait_background_tasks=True)`.
- Always run the whole suite (`pytest tests`), and `ruff check custom_components tests` (ruff 0.16.0).
- All languages must have all keys (`tests/test_integration.py::test_translations_complete`).
- Versions: PEP 440 (`0.7.0rc1`, not `0.7.0-rc1`). `manifest.json` and `pyproject.toml` must match
  (tested).

## Workflow

1. Build an rc, put it on the Mac desktop as zip and unpacked:
   `SG reverse BLE/HA integrasjon/Gen1/<version>/`.
2. Fredrik installs and tests on hardware, reports bytes or screenshots.
3. Write each hardware test into the protocol notes (sent and received bytes).
4. When the rc is approved: PR → merge → draft release with zip → Fredrik publishes.
5. Secrets (tokens, keys, passphrases) are always written as `xxx` in commands and notes.

## GitHub from a cloud session

- Creating releases and pushing tags is refused (HTTP 403). Make the draft and the zip; Fredrik
  presses Publish.
- Another session may have changed the repo: fetch again right before tagging or merging.

## License and content

LGPL-3.0 (builds on nkaminski/csrmesh, fiskentnt/com.sgsmart.csrmesh, and uses recsrmesh). Do not
commit SG code, APKs or decompiled code. Document method and findings instead.
