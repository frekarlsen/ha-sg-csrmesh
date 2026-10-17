# SG Smart Gen1 (CSRmesh) – enhetsinnstillinger og bryterparing (protokollnotat)

Kilde: SG Smart-appen 5.0.958 (`com.sgas.leddimapp`, arm64 fra Google Play). Dart-koden er dekompilert
med blutter 17.10.2026, og Java-delen med jadx. Kommandoene er ikke testet på maskinvare, med mindre
det står «verifisert».

Kodenavnene under (`sendSetTrimLevelCommand` osv.) er appens egne. De står her for at funnene skal kunne
etterprøves. Ingen kode fra appen er kopiert inn i integrasjonen.

## 1. Hvordan appen sender til CSR-enheter

Flutter-koden kaller Java-pluginen `flutter_sg_ble` med `sendExtensionModelMessage(meshId, opcode, data)`.
Den går videre til CSRmesh-SDK-en, til `ExtensionModelApi.sendMessage(meshId, opcode, data)`. Med 16-bits
opkode tillater SDK-en høyst 9 databyte. På lufta blir det samme format som integrasjonen allerede bruker:

```
<mål-adresse LE> <opkode BE, FFxx> <tag 2 byte> <data …>
```

- **Taggen er SGs `locationId`** for enheten, sendt big-endian som de to første databytene. Det er de samme
  `E7 A4` som integrasjonen allerede lærer per enhet (`00 00` på enheter som HA har lagt til selv).
  Appen ignorerer svar der de to første bytene ikke er enhetens `locationId`.
- Appen nekter å sende til `0x0000`, unntatt for opkodene `FF10`, `FF0A`, `FF26` og `FFAA`. Den sender
  heller aldri til `0xFFFF`. Alt går direkte til enhetens egen adresse.
- Svar kommer på opkode `FF08` (generell kvittering) eller på en egen svar-opkode. Inn i appen går de via
  `MeshDevice.handleExtensionMessage`, som også gir det til oppgaven som venter.

## 2. Kommandoer

Alle bytene står etter taggen.

| Innstilling | Opkode | Data | Verdier | Svar |
|---|---|---|---|---|
| Sett trim | `FF29` | `KIND IDX` | KIND `01` min, `02` oppstart, `03` maks. IDX er en **indeks i tabell** (se punkt 3) | `FF08 29 KIND …` |
| Les trim | `FF2A` | – | | `FF2B MIN START MAKS 00 00` (indekser). **Verifisert** 17.10.2026, se punkt 6.1 |
| Modus | `FF39` | `MODE` | `00` dimming, `01` TW, `02` av/på, `03` nedtelling, `04` RGB, `05` RGBW, `06` puls | `FF08 39 …` |
| Ekstern trykkbryter | `FF47` | `OPT` | `00` normal, `01` sensor (**ikke** `01`/`02` som i 3.0) | `FF08 47 …` |
| Fading + dimmehastighet | `FF48` | `FADE SPEED` | FADE i tideler sekund (som i 3.0: `05`, `0A`, `14`, `1E`). SPEED `00` normal, `01` rask | `FF08 48 …` |
| Les fastvare | `FF5E` | – | | `FF5F` med versjonen i byte 0–2, 3–5 og 6– (vises som `a.b.c/d.e.f/…`) |
| Bryterknapp → enhet (Gen2-brytere) | `FF13` | `BTN TYPE MÅL_HI MÅL_LO 00 TID` | sendes **til bryteren**. TYPE `00` av/på-par, `07` dimming med én knapp, `08` CCT. MÅL er big-endian | `FF08 13 BTN TYPE`?, se punkt 5 |
| Bryterknapp → enhet (V1-brytere) | `FF14` | `A B 00 MÅL_HI MÅL_LO 00` | Gammel Smart Switch (type `SmartSwitch`) | – |
| Nullstill bryterknapp | `FF17` | `BTN X` | | – |

Fra før (integrasjonen v0.6.2): `FF03` sett, `FF04` spør, `FF05` status (`PWR LEVEL STATUS-ID …`), `FF15`
tastetrykk, `FF50` endring, `FF88` betjent for hånd.

Om kvitteringen: For trim sjekker appen at `FF08`-svaret har `29` og deretter samme KIND. For fading og
trykkbryter sjekker den bare at byte 0 er opkodens lave byte (`48` eller `47`). Jeg fant **ingen
status-byte** for godtatt eller avvist i CSR-grenen, slik 3.0 har (`01` eller `02`). Se hva som faktisk kommer.

## 3. Trim bruker indeks, ikke prosent

For SIG-mesh-enheter (3.0) sender appen halv-prosent for min og oppstart og hel prosent for maks. For
CSR-enheter slår den i stedet opp i to faste tabeller (`Device.minLevelList` og `maxLevelList`) og sender
**indeksen**:

- **Min og oppstart:** indeks 0–100 gir 0,5–40 %. Kurven er ikke lineær: indeks 11 er 0,5 %, 53 er 10 %,
  84 er 30 % og 100 er 40 %.
- **Maks:** indeks 0–50 gir 40–100 %. Indeks 0 er 40 %, 42 er 90 % og 50 er 100 %.

Ved sending går appen gjennom tabellen. Treffer prosenten eksakt, sendes **siste** indeks med den verdien
(0,5 % gir 11). Ellers sendes første indeks over. Begge tabellene og omregningen ligger i
`csrmesh/settings.py`, med tester.

## 4. Hvilke enheter som har hva (appens egne sjekker)

Appen skiller enhetstyper med en enum (`DeviceType`). Modellnavnene som gatewayen viser over HomeKit, er de
samme navnene (`Dimmer`, `MicroPillGen4` …). Integrasjonen har dem allerede i `migrate.py`. Utvalget under
gjelder modellene i anlegget.

| Modell (HomeKit) | Type | Min/oppstart | Maks | Fading og dimmehastighet | Trykkbryterinngang |
|---|---|---|---|---|---|
| `Dimmer` (LEDDim Smart, eldre) | 0 | – | – | – | – |
| `MicroPill` (eldre) | 6 | ✓ | ✓ | – | – |
| `OnOffPillMicro` | 13 | – | – | – | – |
| `DimmerGen4` | 50 | ✓ | ✓ | ✓ i dimmemodus | ✓ unntatt i puls-modus |
| `MicroPillGen4` | 54 | ✓ | ✓ | ✓ i dimmemodus | ✓ unntatt i puls-modus |
| `SmartSwitchV2`, `SmartSwitchGen4` | 8, 57 | – | – | – | – |
| `ThermostatGen3` | 21 | – | – | – | – |

- Fading, dimmehastighet og trykkbryter finnes bare på Gen3- og Gen4-typer (enum 23–63). Typer under 23
  får dem aldri.
- Typer fra 66 og opp er SIG-mesh, altså 3.0.
- Produktkoden står i samme enum (for eksempel `DimmerGen4` = 2080 og `MicroPillGen4` = 2007). Hvordan
  appen får vite typen til en CSR-enhet som den ikke selv har lagt til, er ikke kartlagt.

## 5. Bryterparing for Gen1 er annerledes enn i 3.0

I 3.0 pares bryteren ved å fortelle **dimmeren** hvilken bryter den skal lytte på (kind `13`,
`SetControllerSwitchTask`). Den oppgaven finnes bare for SIG-mesh.

For CSR-brytere konfigurerer appen **bryteren**. Den forteller bryteren hvilken adresse hver knapp skal
styre (`SetSwitchButtonMeshIdGen2Task` → `FF13`). Det betyr:

- Kommandoen går til en batteribryter, som bare hører etter i noen sekunder etter et trykk. Integrasjonen
  har allerede `sg_csrmesh.send_on_press` til dette.
- Paringen lagres i bryteren. Bryteren sender da selv til dimmeren, uten HA. «Styres av» og «Styrer» må
  likevel bygges som HAs egen oversikt, slik som i 3.0.
- Appen sjekker svaret mot knappen (byte 1) og TYPE (byte 2). Hvilken opkode svaret kommer på, er ikke
  bekreftet.
- **Uavklart:** om BTN er knapp 0–3 (som i `FF15`) eller et knappepar, og hvordan av og på fordeles på et
  par. Det avgjøres raskest ved å fange trafikken når SG-appen parer en knapp (se test 6).

## 6. Test på maskinvare

### 6.1 Resultater

**17.10.2026: `9640` «Lys Vaskerom».** Enheten er lagt til fra HA, så taggen er `0000`. Modellen er
ukjent, men den har trim, og da er den ikke en eldre `Dimmer`. Lampen var av.

- Les trim: sendt `4096ff2a0000`, to ganger. Svaret kom innen 0,3 s på begge:
  `9640 → 0080 ff2b 0000 35 54 32 00 00`.
  - Indeks `35` (53), `54` (84) og `32` (50) gir min 10 %, oppstart 30 % og maks 100 %. Det er det samme
    som 3.0-pillens fabrikkverdier.
  - Tolket som halv-prosent ville det blitt 26,5 / 42 / 50 %. Da ville oppstart ligget over appens grense på
    40 %. **Indeksformatet er dermed bekreftet**, og taggen `0000` virker for enheter lagt til fra HA.
  - Svaret har 5 byte etter taggen. De to siste var `00 00`, og betydningen er ukjent.
- Til sammenligning svarte `9640` på `FF05` med `0080 ff05 0000 00 64 00 01 02 1a 00` (av, nivå 100).

### 6.2 Plan

Bruk en `DimmerGen4` eller `MicroPillGen4` for alt unntatt test 1 og 2. Trim kan også testes på en eldre
`MicroPill`. Skriv adressen little-endian: enheten `CA67` blir `67ca`. Bytt `e7a4` med `0000` hvis enheten
er lagt til fra HA.

Slik lytter du: Under **Utviklerverktøy → Hendelser**, lytt på `sg_csrmesh_frame`. Start
`sg_csrmesh.capture` med `seconds: 120` i en annen fane. Hendelsene kommer bare mens fangsten går. Send så
kommandoene med `sg_csrmesh.send`, og noter sendte og mottatte bytes for hver test.

```yaml
action: sg_csrmesh.send
data:
  messages: ["67caff2ae7a4"]
  repeats: 2
```

| # | Hva | Send | Forvent |
|---|---|---|---|
| 1 | Les trim | `67caff2ae7a4` | `ff2b e7a4 MIN START MAKS ..` fra `ca67`. Regn om med tabellen. Fabrikk er trolig ca. 10 % / 30 % / 100 % (indeks 53 / 84 / 50) |
| 2 | Les fastvare | `67caff5ee7a4` | `ff5f e7a4 …` med minst 8 byte etter taggen |
| 3 | Maks 90 % | `67caff29e7a4032a` | `ff08 e7a4 29 03 …`. Sett lampen til 100 % i HA: den skal lyse svakere. Les trim igjen (test 1): MAKS = `2a` |
| 3b | Tilbake til 100 % | `67caff29e7a40332` | `ff08 e7a4 29 03 …` |
| 4 | Fading 3 s (Gen4) | `67caff48e7a41e00` | `ff08 e7a4 48 …`. Slå av, og så på igjen fra et høyt nivå: lyset skal tones inn |
| 5 | Trykkbryter normal (Gen4) | `67caff47e7a400` | `ff08 e7a4 47 …`. Normal er trolig fabrikkverdien, så dette endrer ingenting |
| 6 | Fang paring fra appen | – | Mens fangsten går: i SG-appen, sett én knapp på en Smart Switch som fortsatt er på gatewayen til å styre en dimmer. Se etter `FF13` fra `8000` til bryteren, og svaret |

Test 6 er den raskeste veien til ekte bytes. Det samme gjelder trim og fading, hvis du endrer dem i
SG-appen for en enhet som fortsatt er på gatewayen mens fangsten går.

## 7. Åpent

- Status-byte i `FF08` (godtatt eller avvist), og hva byte 3 og videre i `FF2B` betyr.
- Om CSR-enhetene sender svar uoppfordret, slik 3.0-pillen sender sin egen status før svaret.
- Knappenummerering og TYPE i `FF13`, og svarformatet.
- Ingen sjekket ennå: HeatReg (`ThermostatGen3`), produktgjenkjenning, og lesing av bryterens egen
  paringstabell.
