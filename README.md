# SG Smart Gen1 (CSRmesh) for Home Assistant

Lokal styring av SG Smart-enheter som bruker CSRmesh, direkte fra Home Assistant over Bluetooth via en
ESPHome Bluetooth-proxy. Gatewayen, HomeKit og skyen trengs ikke. Integrasjonen bruker anleggets egen
nettverks-PIN og enhetsadresser.

Søsterprosjekt for SG Smart 3.0 (Bluetooth SIG Mesh):
[ha-sg-smart-mesh](https://github.com/frekarlsen/ha-sg-smart-mesh).

## Status (v0.7.0)

| Enhet | I HA | Status |
|---|---|---|
| LEDDim Smart, LEDDim Smart Pill Micro | `light` med dimming | Virker. Endringer gjort for hånd kommer som `FF50`/`FF88` |
| LEDDim Smart Pill On/Off Micro | `light` av/på | Virker |
| Smart Switch | `event` per knapp, batteri, «Styrer» | Virker. To vipper eller én stor vippe velges ved oppsett |
| HeatReg Smart | `climate`, gulv- og romtemperatur | Ønsket temperatur og av/på, verifisert |

Innstillinger som lagres i enheten, vises under **Konfigurasjon** på enhetssiden:

| Innstilling | Modeller | Lest fra enheten |
|---|---|---|
| Minimumsnivå i dimmeren, oppstartsnivå, maksnivå | Pill Micro og Gen4 | Ja, og lest tilbake etter endring |
| Fading og dimmehastighet | Gen4 | Nei: viser det HA sist sendte |
| Trykkbryterinngang (trykkbryter / sensor) | Gen4 | Nei: viser det HA sist sendte |

### Oppsett av en enhet

Alt gjøres under **Innstillinger → Enheter og tjenester → SG Smart Gen1 → Konfigurer**:

- **Flytt en enhet fra SG-gatewayen:** navn, rom og modell hentes fra gatewayen (HomeKit), og HA finner
  adressen selv.
- **Legg til ny (fabrikkresatt) enhet:** HA legger enheten inn i nettverket. Du velger navn, rom og modell.
- **Legg til med mesh-adresse:** for avanserte brukere.
- For en **Smart Switch** velger du også panelet: to vipper (fire knapper) eller én stor vippe.
- **Endre en enhet:** bytt modell eller bryterpanel senere. Enheter lagt til før 0.7.0 får et varsel
  under Reparasjoner til modellen er valgt.

Modellen bestemmer hvilke innstillinger enheten får. Den kan ikke leses over meshen. SG-appen viser modell og
fastvare under enhetsinformasjonen: Gen4-enheter har fastvare 1.x.x, eldre LEDDim Smart 1.59 og eldre Pill
Micro 1.13.

### Koble en Smart Switch til et lys

**Konfigurer → Koble en Smart Switch til et lys:** velg vippe og lys, trykk Send, og trykk så kort på
bryteren. Bryteren lagrer koblingen og styrer lyset direkte, også når HA er nede. Øverst slår på (hold:
dimmer opp), nederst slår av (hold: dimmer ned). Samme sted fjernes en kobling.

- En bryter kan bare styre lys som er satt opp på samme måte: begge flyttet fra SG-gatewayen, eller begge
  lagt til av HA. Dialogen viser bare lys som passer.
- «Styrer» (på bryteren) og «Styres av» (på lyset) er HAs egen oversikt over koblinger gjort fra HA. En
  bryter kan ikke spørres om koblingene sine, så koblinger gjort i SG-appen vises ikke.
- Knappene gir fortsatt hendelser i HA, også når de er koblet til et lys.

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
csrmesh/settings.py   trim, fading, trykkbryter, fastvare, bryteroppsett (ren Python)
csrmesh/heatreg.py    HeatReg: status, temperaturer, ønsket temperatur   (ren Python)
models.py             SGs modeller og hvilke innstillinger hver har
bridge.py             én GATT-link via HA Bluetooth / ESPHome-proxy
coordinator.py        enheter, tilstand, levering med kvittering, innstillinger, bryteroppsett
migrate.py            flytting fra SG-gatewayen (HomeKit)
provision.py          legge til og fjerne enheter (MASP)
light.py event.py climate.py number.py select.py sensor.py   entiteter
```

## Kreditering og lisens

- **nkaminski**, [csrmesh](https://github.com/nkaminski/csrmesh): bearer, nøkkelavledning og krypto
  (LGPL-3.0/GPL-3.0). `csrmesh/crypto.py` er en reimplementasjon som er kryssjekket byte for byte.
- **fiskentnt**, [com.sgsmart.csrmesh](https://github.com/fiskentnt/com.sgsmart.csrmesh): SG-meldinger
  (LGPL-3.0).
- **oyvindkinsey**, [recsrmesh](https://github.com/oyvindkinsey/recsrmesh): MASP-tilknytning (LGPL-3.0).
- **fromm1990, einaros, erlwes**: kartla SG Gen1 som CSRmesh på hjemmeautomasjon.no og HA-forumet.

Lisensiert under **LGPL-3.0**. Uoffisiell, og ikke tilknyttet SG Armaturen.
