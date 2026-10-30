# SG Smart Gen1 (CSRmesh) for Home Assistant

Lokal styring av førstegenerasjons SG Smart-enheter (CSRmesh) direkte fra Home Assistant, over Bluetooth
via en ESPHome Bluetooth-proxy. Du trenger verken SG-gatewayen, HomeKit eller skyen. Integrasjonen bruker
anleggets egen nettverks-PIN og enhetenes mesh-adresser.

Søsterprosjekt for SG Smart 3.0 (Bluetooth SIG Mesh):
[ha-sg-smart-mesh](https://github.com/frekarlsen/ha-sg-smart-mesh).

> Uoffisiell og ikke tilknyttet SG Armaturen. Versjon 0.7.0 er i testfasen (`rc`).

## Støttede enheter

| Enhet | I Home Assistant | Status |
|---|---|---|
| LEDDim Smart (også Gen4) | `light` med dimming | Virker |
| LEDDim Smart Pill Micro (også Gen4) | `light` med dimming | Virker |
| LEDDim Smart Pill On/Off Micro | `light` av/på | Virker |
| Smart Switch (V2 og Gen4) | én `event` per knapp, batteri, «Styrer» | Virker. To vipper eller én stor vippe |
| HeatReg Smart | `climate`, gulv- og romtemperatur | Ønsket temperatur og av/på |

Endringer gjort for hånd (bryter eller trykknapp) fanges opp, så HA alltid viser riktig tilstand.

Innstillinger som lagres i selve enheten, finner du under **Konfigurasjon** på enhetssiden:

| Innstilling | Modeller | Lest fra enheten |
|---|---|---|
| Minimumsnivå, oppstartsnivå og maksnivå | Pill Micro og Gen4 | Ja, og lest tilbake etter endring |
| Fading og dimmehastighet | Gen4 | Nei, viser det HA sist sendte |
| Trykkbryterinngang (trykkbryter / sensor) | Gen4 | Nei, viser det HA sist sendte |

## Krav

- Home Assistant 2025.8 eller nyere.
- En ESPHome Bluetooth-proxy med aktive tilkoblinger:
  ```yaml
  bluetooth_proxy:
    active: true
  ```
- Minst én SG-enhet innen rekkevidde av proxyen. Meldinger videresendes gjennom hele meshen, så resten
  av huset nås via den.
- Nettverks-PIN-en til SG-anlegget (fra SG-appen eller gatewayen).

## Installasjon

**HACS (anbefalt)**

1. HACS → menyen øverst til høyre → **Egendefinerte repositorier**.
2. Legg til `https://github.com/frekarlsen/ha-sg-csrmesh` som type **Integrasjon**.
3. Installer **SG Smart Gen1 (CSRmesh)** og start Home Assistant på nytt.

**Manuelt**

Kopier mappen `custom_components/sg_csrmesh` til `config/custom_components/` og start Home Assistant
på nytt.

Legg deretter til integrasjonen under **Innstillinger → Enheter og tjenester → Legg til integrasjon →
SG Smart Gen1**, og oppgi PIN-en.

## Legge til enheter

Alt gjøres under **Innstillinger → Enheter og tjenester → SG Smart Gen1 → Konfigurer**:

- **Flytt en enhet fra SG-gatewayen:** navn, rom og modell hentes fra gatewayen (via HomeKit), og HA
  finner adressen selv.
- **Legg til en ny enhet:** for fabrikkresatte enheter, og enheter som er fjernet fra SG-gatewayen. HA
  søker gjennom meshen. Finner den ikke enheten, kan HA koble seg direkte til enheten, slik SG-appen gjør
  fra telefonen.
- **Legg til med mesh-adresse:** for avanserte brukere.
- **Endre en enhet:** bytt modell eller bryterpanel senere.

Modellen bestemmer hvilke innstillinger enheten får, og må velges ved oppsett, fordi den ikke kan leses
over meshen. SG-appen viser modell og fastvare under enhetsinformasjonen.

For en **Smart Switch** velger du også panel: to vipper (fire knapper) eller én stor vippe.

## Smart Switch

### Bruke knappene i automasjoner

Hver knapp blir en egen hendelsesentitet, for eksempel `event.trykkbryter_overst_venstre`. Bruk
utløseren **Hendelse mottatt** (`event.received`) med hendelsestypen `press`:

```yaml
triggers:
  - trigger: event.received
    target:
      entity_id: event.trykkbryter_overst_venstre
    options:
      event_type: [press]
```

Hvert trykk gir én hendelse. Holder du en knapp inne, registreres det også som ett trykk.

### Koble en bryter direkte til et lys

**Konfigurer → Koble en Smart Switch til et lys:** velg vippe og lys, trykk **Send**, og trykk så kort på
bryteren. Bryteren lagrer koblingen og styrer lyset direkte, også når HA er nede. Øverst slår på (hold:
dimmer opp), nederst slår av (hold: dimmer ned). Samme sted fjerner du en kobling.

- En bryter kan bare styre lys som er satt opp på samme måte: begge flyttet fra SG-gatewayen, eller begge
  lagt til av HA. Dialogen viser bare lys som passer.
- «Styrer» (på bryteren) og «Styres av» (på lyset) er HAs egen oversikt over koblinger gjort fra HA.
  Bryteren kan ikke spørres om koblingene sine, så koblinger gjort i SG-appen vises ikke.
- Knappene gir fortsatt hendelser i HA når de er koblet til et lys.

### Kjent begrensning for Smart Switch V2

En V2-bryter sender ekstra meldinger i omtrent 7 sekunder etter hvert korte trykk, og disse ser ut som
trykk på en annen knapp. Integrasjonen filtrerer dem bort. Et trykk på en *annen* knapp mindre enn
2,5 sekunder etter forrige trykk blir derfor ikke registrert. Det samme gjelder et nytt trykk på samme
knapp innen 8 sekunder. Gen4-brytere har ikke denne begrensningen.

## Feilsøking og kartlegging

- `sg_csrmesh.capture`: logger alle rammer på ditt eget nettverk, dekryptert med din egen PIN. Rammene
  kommer som `sg_csrmesh_frame`-hendelser og vises i diagnostikken.
- `sg_csrmesh.send`: sender rå meldinger (`<adresse LE> <opkode> <tag> <data>`).
- `sg_csrmesh.send_on_press`: venter på et trykk fra en batteribryter og sender da med en gang.

Protokollen og alle maskinvaretester er dokumentert i [docs/gen1-protocol.md](docs/gen1-protocol.md).

## Utvikling

```sh
uv venv -p 3.14 && uv pip install -r requirements-test.txt
pytest tests
ruff check custom_components tests
```

```
csrmesh/crypto.py     nøkkel fra PIN, AES-128-OFB + HMAC-rammer          (ren Python)
csrmesh/protocol.py   SG-meldinger, statusparser, fragmentsamling        (ren Python)
csrmesh/settings.py   trim, fading, trykkbryter, fastvare, bryteroppsett (ren Python)
csrmesh/heatreg.py    HeatReg: status, temperaturer, ønsket temperatur   (ren Python)
models.py             SGs modeller og hvilke innstillinger hver har
bridge.py             én GATT-forbindelse via HA Bluetooth / ESPHome-proxy
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
