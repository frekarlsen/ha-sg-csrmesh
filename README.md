# SG Smart Gen1 (CSRmesh) for Home Assistant

Lokal styring av SG Smart-enheter som bruker CSRmesh, direkte fra Home Assistant over Bluetooth via en
ESPHome Bluetooth-proxy. Gatewayen, HomeKit og skyen trengs ikke. Integrasjonen bruker anleggets egen
nettverks-PIN og enhetsadresser.

Søsterprosjekt for SG Smart 3.0 (Bluetooth SIG Mesh):
[ha-sg-smart-mesh](https://github.com/frekarlsen/ha-sg-smart-mesh).

## Status (v0.6.2)

| Enhet | I HA | Status |
|---|---|---|
| LEDDim Smart, Smart Pill Micro (også Gen4) | `light` med dimming | Virker. Sett med `FF03`, les med `FF04`/`FF05`. Endringer gjort for hånd kommer som `FF50`/`FF88` |
| Smart Pill On/Off | `light` av/på | Virker |
| Smart Switch | `event` per knapp | Virker (`FF15`). Batteri er ukjent |
| HeatReg Smart | `climate` | Rammeverk. Formatet er ikke kartlagt |

Enheter kan legges til på tre måter, fra **Konfigurer**:

- **Flytt fra SG Gateway:** en enhet som gatewayen viser over HomeKit, får adresse, navn og rom automatisk.
- **Legg til ny:** en fabrikknullstilt enhet legges inn i meshen (MASP, via `recsrmesh`).
- **Legg til på adresse:** for avanserte brukere.

Enheter kan også fjernes, eventuelt med fabrikknullstilling.

Under arbeid: innstillinger som lagres i enheten (trim, fading, trykkbryterinngang) og direkte paring av
Smart Switch til dimmer. Se [docs/gen1-protocol.md](docs/gen1-protocol.md).

## Krav

- Home Assistant 2025.8 eller nyere.
- ESPHome Bluetooth-proxy med aktive tilkoblinger:
  ```yaml
  bluetooth_proxy:
    active: true
  ```
- Minst én mesh-node (`@NDxxxx`, `@MDxxxx` …) innen rekkevidde av proxyen. Hvilken som helst node sender
  videre inn i hele meshen.

## Verktøy for kartlegging

- `sg_csrmesh.capture`: logger alle rammer på ditt eget nettverk, dekryptert med din egen PIN. Rammene
  kommer som `sg_csrmesh_frame`-hendelser og vises under Diagnostikk.
- `sg_csrmesh.send`: sender rå meldinger (`<adresse LE> <opkode> <tag> <data>`).
- `sg_csrmesh.send_on_press`: venter på et trykk fra en batteribryter og sender da med en gang.

## Utvikling

```sh
uv venv -p 3.14 && uv pip install -r requirements-test.txt
git clone https://github.com/nkaminski/csrmesh /tmp/csrmesh   # valgfri kryssjekk av kryptoen
pytest tests
ruff check custom_components tests
```

Arkitektur:

```
csrmesh/crypto.py     nøkkel fra PIN, AES-128-OFB + HMAC-rammer        (ren Python)
csrmesh/protocol.py   SG-meldinger, statusparser, fragmentsamling      (ren Python)
csrmesh/settings.py   trim, fading, trykkbryter, modus (ikke verifisert) (ren Python)
bridge.py             én GATT-link via HA Bluetooth / ESPHome-proxy
coordinator.py        enheter, tilstand, levering med kvittering, verktøy
migrate.py            flytting fra SG-gatewayen (HomeKit)
provision.py          legge til og fjerne enheter (MASP)
light.py event.py climate.py   entiteter
```

## Kreditering og lisens

- **nkaminski**, [csrmesh](https://github.com/nkaminski/csrmesh): bearer, nøkkelavledning og krypto
  (LGPL-3.0/GPL-3.0). `csrmesh/crypto.py` er en reimplementasjon som er kryssjekket byte for byte.
- **fiskentnt**, [com.sgsmart.csrmesh](https://github.com/fiskentnt/com.sgsmart.csrmesh): SG-meldinger
  (LGPL-3.0).
- **oyvindkinsey**, [recsrmesh](https://github.com/oyvindkinsey/recsrmesh): MASP-tilknytning (LGPL-3.0).
- **fromm1990, einaros, erlwes**: kartla SG Gen1 som CSRmesh på hjemmeautomasjon.no og HA-forumet.

Lisensiert under **LGPL-3.0**. Uoffisiell, og ikke tilknyttet SG Armaturen.
