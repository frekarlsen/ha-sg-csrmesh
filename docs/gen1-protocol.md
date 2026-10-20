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
| Sett trim | `FF29` | `KIND IDX` | KIND `01` min, `02` oppstart, `03` maks. IDX er en **indeks i tabell** (se punkt 3) | `FF08 29 KIND`. **Verifisert** for maks 17.10.2026 |
| Les trim | `FF2A` | – | | `FF2B MIN START MAKS 00 00` (indekser). **Verifisert** 17.10.2026, se punkt 6.1 |
| Modus | `FF39` | `MODE` | `00` dimming, `01` TW, `02` av/på, `03` nedtelling, `04` RGB, `05` RGBW, `06` puls | `FF08 39 …` |
| Ekstern trykkbryter | `FF47` | `OPT` | `00` normal, `01` sensor (**ikke** `01`/`02` som i 3.0) | `FF08 47 OPT`. Kvittering verifisert 17.10.2026 (for `00`) |
| Fading + dimmehastighet | `FF48` | `FADE SPEED` | FADE i tideler sekund. Appens valg: `00` av, `05`, `0A`, `14`, `1E` (RGBW-typer: bare `05` og `1E`). SPEED `00` normal, `01` rask | `FF08 48 FADE`. **Verifisert** 17.10.2026 (`1E` og `00`) |
| Les fastvare | `FF5E` | – | | `FF5F A B C D E F G`. Appen viser `A.B.C/D.E.F/G`. **Verifisert** 17.10.2026 |
| Bryter: hold deg våken / sov | `FF12` | `AWAKE TID` | `01` våken (før oppsett), `00` sov (etter) | `FF08 12`. **Verifisert** 19.10.2026 |
| Bryterknapp → enhet (Gen2-brytere), **verifisert** 19.10.2026 | `FF13` | `BTN TYPE MÅL_HI MÅL_LO 00 TID` | sendes **til bryteren**. TYPE `00` av/på-par, `07` dimming med én knapp, `08` CCT. MÅL er big-endian | `FF08 13 BTN TYPE`?, se punkt 5 |
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
- **Sett maks 50 %, verifisert.** Lampen sto på 100 %.
  1. Sendt `4096ff2900000308`, to ganger. Svaret var `9640 → 0080 ff08 0000 29 03` på begge.
     Kvitteringen er `29` (kommandoen) og `03` (maks). Den har ingen statusbyte.
  2. Lampen gikk **med en gang** til omtrent halv styrke, uten noen ny nivåkommando.
  3. Tilbakelest: `ff2b 0000 35 54 08 00 00`, altså maks-indeks 8 = 50 %. Min og oppstart var uendret.
  4. Satt tilbake med `4096ff2900000332`. Lampen gikk tilbake til full styrke, og tilbakelest var
     `35 54 32 00 00`. Dimmeren står igjen på fabrikkverdiene.
- **Fading 3 s, verifisert.**
  1. Sendt `4096ff4800001e00`, to ganger. Svaret var `9640 → 0080 ff08 0000 48 1e` på begge.
     Kvitteringen gjentar fading-verdien.
  2. Av-slag er fortsatt momentant.
  3. Påslag fra av til 100 % toner gradvis opp. Fadingen gjelder bare påslag («Start Up Fading»).
     Første forsøk ble ikke lagt merke til, andre forsøk ble observert tydelig.
  - Fading kan ikke leses tilbake (appen har ingen kommando for det), og den opprinnelige verdien er ukjent.
  - Siden 9640 godtar fading, er den etter appens tabell en Gen3- eller Gen4-type, ikke en eldre `MicroPill`.
- **Fading av, verifisert.** Sendt `4096ff4800000000`, og svaret var `ff08 0000 48 00`. Påslag fra av
  til 100 % kom momentant i begge forsøkene. Fading står nå på av, som er appens laveste valg.
- **Fastvare, verifisert.** Sendt `4096ff5e0000`, og svaret var `ff5f 0000 01 02 00 01 02 00 01`. Appen
  viser det som `1.2.0/1.2.0/1`. Hva de tre delene betyr, er ukjent. Appen krever minst 7 byte etter
  taggen.
- **Trykkbryter normal, kvittert.** Sendt `4096ff47000000`, og svaret var `ff08 0000 47 00`. Kvitteringen
  gjentar valget. Det er ingen synlig endring, fordi normal trolig var fabrikkverdien.
**17.–19.10.2026: bryterparing, `c3e4` «trykkbryter_01» → `9640`, første forsøk (ikke lykkes; se
løsningen lenger ned).**
`c3e4` er lagt til fra HA (tag `0000`), og modellen er ukjent. Venstre side ble brukt, og
automasjonen var slått av under testen.

- **`FF12` hold deg våken / sov, verifisert.** `e4c3 ff12 0000 01 TID` ble kvittert med
  `ff08 0000 12` hver gang den kom fram innen ca. 1 s etter et trykk. `… 00 TID` (sov) er kvittert
  på samme måte. Byten er en bool fra appens `setSmartSwitchSleep(sleep)`: `01` = ikke sov, `00` = sov.
  Bryteren svarer likevel **ikke** når HA sender uten at noen har trykket rett før (testet ca. 60 s
  etter `01`), så «våken» varer ikke mellom trykk. Ved en feil ble også `0c` sendt og kvittert, og
  bryteren står igjen på `00`.
- **`FF13` knapp → dimmer: ingen kvittering og ingen virkning, fire forsøk.**
  - 17.10: `FF13` uten `FF12`, sendt 0,6 s etter trykket.
  - 19.10: `FF13` 10 s etter `FF12`-kvitteringen, altså for sent.
  - 19.10: `FF12 01` og `FF13` × 2 i samme vindu, `FF13` 2–4 s etter trykket.
  - 19.10: `FF13` som første melding, 0,5–1 s etter trykket, mens bryteren fortsatt sendte. Den var
    altså våken.
  - Sendt: `e4c3 ff13 0000 BTN 00 9640 00 TID` (BTN 0 og 1, TID 01, 02, 08, 09, 0b).
  - Bryteren hørte trolig kommandoen, men godtok den ikke. Mulige årsaker: et annet format for denne
    bryteren, en manglende `locationId` (enheter lagt til fra HA har aldri fått en fra SG-appen), eller
    et steg appen gjør først som vi ikke kjenner.
- **Andre meldinger fra bryteren ved hvert trykk:**
  - `0080 ff15 0000 BTN 01 01`: tastetrykket, som før.
  - `0080 ff07 0000 N`: appens «reset button response», en trykkteller (`resetCount`).
  - `0000 83 01 64 10 N`: standard CSRmesh **batteristatus** (`BATTERY_STATE`), med nivå `0x64` = 100 %
    og tilstand `0x10`. N er bryterens egen teller. Den kan gi batterinivået i HA.
- Neste steg: fang trafikken mens SG-appen konfigurerer en knapp på en bryter som er på gatewayen
  (test 6). Gatewayen har ni `SmartSwitchV2` og én `SmartSwitchGen4` («Smart Switch Kjeller»).
**19.10.2026: SG-appen konfigurerer en bryter, fanget. Verifisert.**
Bryteren er Smart Switch Soverom `99bc` (SmartSwitchV2, fastvare 1.9, tag `e7a4`). Høyre knapp ble
nullstilt og paret på nytt med Lys Nattbord `89e0` (MicroPillGen4). Tidene er når rammene kom fram
til HA, regnet fra bryterens første ramme.

| Tid | Retning | Ramme | Betyr |
|---|---|---|---|
| 0,0 s | 99bc → 8000 | `ff15 e7a4 03 00 09` | trykk, knapp 3 |
| +0,4 s | 99bc → 8001 | `ff0a e7a4 ff 64` | bryterens egen kommando (ukjent betydning) |
| +0,7 s | 8000 → 99bc | `ff12 e7a4 01 01` | hold deg våken, TID 01 |
| +0,6/0,8 s | 99bc → 8000 | `ff08 e7a4 12` | kvittering |
| +1,0 s | 8000 → 99bc | `ff17 e7a4 03 02` | nullstill knapp 3, TID 02 |
| +1,0/1,2 s | 99bc → 8000 | `ff08 e7a4 17 03` | kvittering med knappen |
| +1,2 s | 8000 → 99bc | `ff13 e7a4 03 00 89e0 00 03` | knapp 3 → 89e0, type 00, TID 03 |
| +1,7 s | 99bc → 8000 | `ff08 e7a4 13 03 00` | kvittering med knapp og type |
| +1,7 s | 8000 → 99bc | `ff12 e7a4 00 04` | sov, TID 04 |
| +1,8 s | 99bc → 8000 | `ff08 e7a4 12` | kvittering |
| +3,4 s | 99bc → 89e0 | `ff0a e7a4 00 64` | bryteren styrer Lys Nattbord direkte |

- **`FF13`-formatet er altså riktig**, og det samme som HA sendte til `c3e4`. Appen gjør hele
  sekvensen (våken, nullstill, sett, sov) innen 1,7 s, mens bryteren er våken.
- HA brukte 1,2–1,5 s per melding (to kopier og 0,3 s pause, og hver GATT-skriving via proxy tar
  ca. 0,4 s). `FF12` og `FF13` kom derfor aldri i samme vindu med riktig tempo, og `FF17` ble aldri
  sendt. Fra 0.6.3rc1 tar `send_on_press` `repeats: 1, gap: 0`.
- Én paring er **én** `FF13` med type `00` for én knapp. Det finnes ikke noe par-oppsett på
  protokollnivå for denne bryteren.
- `FF0A <tag> A B` er bryterens styringskommando. Den sendes direkte til målet (og til `8001`).
  `A`/`B` er ikke kartlagt (`ff 64` og `00 64` ble sett).
- `FF15` fra `99bc` har kroppen `03 00 09`, mens `c3e4` sender `BTN 01 01`. Forskjellen er ikke forstått.
- Under fangsten sendte telefonappen også `0000 ff10 0000` (navnespørring til alle med tag `0000`)
  hvert 4. sekund. `9640` svarte `ff11 0000 "GD8352" c3`, altså sitt navn i ASCII.

**19.10.2026: bryterparing fra HA, verifisert. `c3e4` → `9640`.**
Sendt med 0.6.3rc1 med `send_on_press` (`repeats: 1, gap: 0`) ved trykk på øverst venstre.
Tidene er regnet fra bryterens første ramme.

| Tid | Sendt | Kvittering |
|---|---|---|
| +0,48 s | `e4c3 ff12 0000 01 11` | `ff08 0000 12` |
| +1,20 s | `e4c3 ff17 0000 00 12` | `ff08 0000 17 00` |
| +1,68 s | `e4c3 ff13 0000 00 00 9640 00 13` | `ff08 0000 13 00 00` |
| +2,16 s | `e4c3 ff12 0000 00 14` | `ff08 0000 12` |

- **Resultat:** Øverst venstre slår på `9640`, nederst venstre slår av, og hold dimmer opp og ned.
  Det virker som oppsett fra SG-appen og trenger ikke HA. Dimmeren meldte selv `ff50 0000 01 64`
  ved påslag.
- **Én `FF13` konfigurerer hele vippen (venstre side).** BTN `00` er øverste knapp i paret, og bryteren
  lager selv av/dim ned på knappen under. Det stemmer med appen, som sendte én `FF13` med BTN `03` for
  «høyre knapp» på `99bc`. Tolkningen er at BTN er indeksen for øverste knapp, 0 for venstre og 3 for
  høyre, med samme nummerering som `FF15`.
- **Kravet er tempo:** våken, nullstill, sett og sov innen ca. 2 s etter trykket. Nullstilling (`FF17`)
  før `FF13` er med som i appen. Om den er nødvendig, er ikke testet.
- Kommandoen fra bryteren til dimmeren (trolig `FF0A`) ble ikke fanget av HA denne gangen.

**19.10.2026: bryter med tag `0000` → lampe med tag `e7a4`. Virker ikke.**
`c3e4` (lagt til fra HA, tag `0000`), høyre vippe (BTN `03`), mål Lys Nattbord `89e0` (MicroPillGen4,
på gatewayen, tag `e7a4`). Lampen var av.

| Tid | Ramme | Betyr |
|---|---|---|
| 11:40:05 | sendt `e4c3 ff12 0000 01 25`, `ff17 0000 03 26`, `ff13 0000 03 00 89e0 00 27`, `ff12 0000 00 28` (+0,47 / 0,83 / 1,08 / 1,55 s) | paring |
| | `ff08 0000 12`, `ff08 0000 17 03`, `ff08 0000 13 03 00`, `ff08 0000 12` | alt kvittert |
| 11:45:46 | `c3e4 → 89e0 ff03 0000 01 00 92` (×3), så `ff15 0000 03 01 01` | øverst høyre: bryteren sender **«på»** |
| 11:45:48 | `89e0 → 8000 ff05 e7a4 00 10 …` | lampen fortsatt av |
| 11:45:52 | `c3e4 → 89e0 ff03 0000 00 00 93` (×3), så `ff15 0000 02 01 01` | nederst høyre: **«av»** |

- **Lampen overså kommandoen.** Bryteren sender med sin egen tag, og lampen hører bare på sin. En
  bryter kan bare styre enheter med samme tag. Paring i HA må derfor bare tilby mål med samme tag som
  bryteren. Gatewayenhetene skal uansett flyttes over til HA.
- **`c3e4` styrer med `FF03`** (`<tag> PWR LEVEL TID`, nivå `00` = siste nivå?) og sender kommandoen
  *før* `FF15`. `99bc` brukte `FF0A`. De to bryterne er trolig ulike generasjoner, som også `FF15`-kroppen
  tyder på (`BTN 01 01` mot `BTN 00 09`).
- Knappene i `FF15` er 0 = øverst venstre, 1 = nederst venstre, 2 = nederst høyre, 3 = øverst høyre.

**19.10.2026: fjerne paring, verifisert. `FF17` alene.**
Ved trykk øverst høyre (11:49:07, bryteren sendte fortsatt `ff03` til `89e0`):

| Tid | Sendt | Kvittering |
|---|---|---|
| +0,72 s | `e4c3 ff12 0000 01 29` | `ff08 0000 12` |
| +1,20 s | `e4c3 ff17 0000 03 2a` | `ff08 0000 17 03` |
| +1,68 s | `e4c3 ff12 0000 00 2b` | `ff08 0000 12` |

- Etterpå (11:49:20 øverst høyre, 11:49:26 nederst høyre) sendte bryteren bare `ff15`, `ff07` og
  batteristatus, ikke noe `ff03`. **`FF17 03` fjerner hele høyre vippe.** Knappen gir fortsatt
  hendelser i HA.

- Feil i verktøyet: når to `capture` overlapper, slår den første av loggingen for den andre når den
  avslutter. Rettet i `coordinator.py`.
- Til sammenligning svarte `9640` på `FF05` med `0080 ff05 0000 00 64 00 01 02 1a 00` (av, nivå 100).

**19.10.2026: paring fra dialogen i 0.7.0rc1, og når `FF13` godtas.** `c3e4`, med sendinger fra
`send_on_press` (`repeats: 1, gap: 0`) og trykk på bryteren.

- **Dialogen (rc1):** Første runde for venstre vippe: `FF12 01` (+0,48 s), `FF17 00` (+0,72 s),
  `FF13 00 → 9640` (+1,20 s). `FF13` ble kvittert (`ff08 0000 13 00 00`, fire kopier), men det kom ingen
  kvittering for `FF17`. rc1 krevde begge og sendte på nytt ved hvert trykk. I de neste rundene kom `FF13`
  1,4–2,0 s etter trykket, og da ble ingenting kvittert. Bryteren styrte likevel lampen etter første
  runde, men dialogen ble aldri ferdig.
- **`FF13` på en tom knapp, uten `FF17`:** `FF12 01` (+0,48 s), `FF13 03 → 9640` (+0,96 s) → `ff08 0000 13
  03 00`. Neste trykk øverst til høyre tente Lys Vaskerom. **Virker.**
- **`FF13` på en knapp som allerede er koblet, uten `FF17`:** `FF13 03 → 89e0` (+1,08 s) → ingen kvittering.
  Nederst til høyre slukket fortsatt Lys Vaskerom, så den gamle koblingen sto. **Bryteren tar bare imot
  `FF13` på en tom knapp.** Derfor nullstiller appen først.
- **`FF17` på en koblet knapp:** `FF17 03` (+0,96 s) → `ff08 0000 17 03`. **`FF17` på en tom knapp:**
  også kvittert (`17 03`). Øverst til høyre gjorde deretter ingenting.
- **Vinduet:** Kommandoer kvitteres opp til ca. +1,2 s etter at HA ser trykket. Hver GATT-skriving
  via proxyen tok 0,36–0,6 s denne dagen.
- **Følger for 0.7.0rc2:** En kobling er ferdig når `FF13` er kvittert. Siden `FF13` bare godtas på en tom
  knapp, beviser det også nullstillingen, selv om `FF17`-kvitteringen gikk tapt. Er bare `FF17`
  kvittert, sendes bare `FF13` ved neste trykk. Da kommer den tidlig nok (+0,96 s).

**19.10.2026: en enhet fjernet fra SG-gatewayen er fortsatt i meshen.** En LEDDim Smart Pill Micro
(`@MODBC3`, `00:01:B3:13:DB:C3`) var «fabrikkresatt fra gatewayen», men svarte ikke på HAs MASP-søk
(søket via `@GD8352` hørte bare sitt eget ekko), og direkte tilkobling feilet på −93 dBm.

- Mens SG-appen var åpen, sendte telefonen `0000 ff10 0000` (navnespørring til alle med tag `0000`) hvert
  4. sekund. Svar: `df73 → 0080 ff11 0000 4d 4f 44 42 43 33 d8` = «MODBC3» + `d8`, og `9640 → … "GD8352" c3`.
- `73df ff04 0000` → `ff05 0000 00 32 00 00 0d` (av, nivå 50 %). **Gatewayens «fjern» sletter bare
  plasseringen (tag `e7a4` → `0000`). Enheten beholder nettverksnøkkelen** og legges til på adressen, uten
  nøkkelutveksling. Fra 0.7.0rc4 viser «Legg til ny enhet» slike enheter.
- Etterpå sendte pillen `ff07 0000 e1`, `e3`, `e4` og så `e4` omtrent hvert sekund. Det er trolig appens
  tilbakestillingsteller, sendt mens enheten slås av og på for fabrikkreset. Ikke kartlagt.
- Navnet i `FF11` er typen og de fire siste i MAC-adressen, det samme som annonseringsnavnet etter `@`.
  Den siste byten (`d8`, `c3`) er ukjent.

**19.10.2026: HeatReg Smart (`ThermostatGen3`), verifisert.** Tre termostater på gatewayen (tag
`e7a4`): `808c` Varmekabler Bad (fastvare 52.0), `8505` Varmekabler Kjellerstue (53.2) og `f03f`
Varmekabler Yttergang (53.2). Kryssjekket mot verdiene gatewayen viser i HomeKit.

Gatewayen spør hver termostat omtrent hvert 20. sekund:

| Spørsmål | Svar | Innhold (etter taggen) |
|---|---|---|
| `FF04` | `FF05 F SP SPd FWa FWb X Y` | F = flagg, SP + SPd/10 = **ønsket temperatur**, FWa.FWb = fastvare |
| `FF3C 00` | `FF3D Ra Rd Ga Gd Ma Md ..` | **romtemperatur** Ra + Rd/10, **gulvtemperatur** Ga + Gd/10, **maks gulv** Ma + Md/10 |
| `FF3C 01` | `FF5C ..` | kalibrering (offset rom/gulv), grunnvarme (varighet/prosent), NTC-type, adaptiv/åpent vindu |

- Eksempel `808c`: `ff05 e7a4 91 1c 00 34 00 30 00` = 28,0 °C, fastvare 52.0. `ff3d e7a4 15 03 1b 08 1e
  00 00` = rom 21,3 °C, gulv 27,8 °C, maks gulv 30,0 °C. HomeKit viste 28 og 27,8.
- **Flagg (byte 0):** bit 0 = på, bit 5 = **varmer nå** (reléet er på). `8505` sendte `b1` mens HomeKit
  viste «heating», de andre `91` («idle»). Bit 4 og 7 er satt på alle tre, betydning ukjent.
- HomeKit viser gulvtemperaturen som nåværende temperatur på alle tre. Appen velger rom eller gulv
  etter sensormodus (`ambient` 0, `floor` 1, `ambientAndFloor` 2), men den lagres i appen og står ikke i
  statusen. Integrasjonen viser gulv, og begge som egne sensorer.

| Kommando | Opkode | Data | Svar |
|---|---|---|---|
| Ønsket temperatur | `FF3E` | `INT TIENDEDEL 01 TID` | `FF08 3E`. **Verifisert** |
| Av / på | `FF42` | `PWR 00 TID` | ses i `FF05` bit 0. **Verifisert** |
| Les temperaturer | `FF3C` | `00` (temperaturer) / `01` (innstillinger) | `FF3D` / `FF5C` |

- Gatewayen (via HomeKit, 24 → 24,5): `3ff0 ff3e e7a4 18 05 01 00` → `ff08 e7a4 3e`, og neste `FF05` viste
  `18 05`.
- Fra HA direkte: `3ff0ff3ee7a418000131` → `ff08 e7a4 3e`, `FF05` viste `18 00`. HomeKit fulgte etter ved
  gatewayens neste spørsmål.
- `3ff0ff42e7a4000032` (av) → `FF05` flagg `90`. `3ff0ff42e7a4010033` (på) → `91`. Ingen `FF08` ble sett for
  `FF42`.

**19.–20.10.2026: Smart Switch V2 «trykkbryter_02» (`9280`, navn `MXCBB3`, tag `0000`) sender
etterslep som ligner trykk nederst til høyre. Verifisert.**
Kroppen i `FF15` er `BTN 00 08`, som `99bc` (`BTN 00 09`), ikke `BTN 01 01` som `c3e4`. Tider er når
rammen kom fram til HA, regnet fra første ramme etter trykket.

| Trykk | Rammer fra `9280` til `8000` |
|---|---|
| Nederst høyre, alene (20.10 08:25:48) | `ff15 0000 02 00 08` 0,0 s og 1,5 s; `ff07 0000 14` 3,4 / 4,3 / 5,1 / 6,0 s; `02 00 08` 4,5 / 5,2 / 6,9 s |
| Øverst venstre, alene (08:26:28) | `ff15 0000 00 00 08` 0,0 s; `02 00 08` 1,4 / 4,3 / 5,3 / 6,7 s; `ff07 0000 00` 3,4 / 4,0 / 5,2 / 6,0 s |
| Øverst venstre holdt ca. 3 s (08:27:07) | `ff07 0000 00` 0,0 / 0,5 s; `00 00 08` 0,9 / 1,9 / 3,2 s; ingen `02` etterpå |
| 19.10 kveld: øverst venstre, øverst høyre, nederst venstre | først riktig knapp (`00`, `03`, `01`), så `02 00 08` 3–4 ganger de neste ca. 7 s |

**20.10.2026, etter 0.7.0rc5:** etterslepet byttet knapp. Hvert trykk 15 s fra hverandre:

| Trykk | Første ramme | Etterslep (+1,0–1,6 s, så +4, +5, +7 s) | `ff07` |
|---|---|---|---|
| nederst venstre 08:50:00 | `01 00 08` | `00 00 08` ×4 | `04` |
| nederst høyre 08:50:15 | `02 00 08` | `00 00 08` ×4 | `05` |
| øverst høyre 08:50:30 | `03 00 08` | `00 00 08` ×4 | `06` |
| øverst venstre 08:50:45 | `00 00 08` | `00 00 08` ×4 | `07` |
| nederst høyre **holdt** ca. 3 s, 08:54:23 | `ff07 0000 07` (uendret) 0,0 / 0,6 s, så `02 00 08` 2,0 / 3,5 s | – | `07` |
| øverst venstre ca. 08:54:40 | ingen rammer (trykket kom ikke fram eller ble ikke registrert av bryteren) | – | – |
| nederst venstre 08:55:07 | `01 00 08` | `02 00 08` ×4 | `08` |

- **Etterslepet etter et kort trykk navngir den knappen som sist ble holdt inne**, ikke den som ble
  trykket. Før 08:27 var det `02`, etter hold øverst venstre 08:27 var det `00`, etter hold nederst
  høyre 08:54 `02` igjen.
- Første ramme er alltid riktig knapp. Første etterslep kommer 1,0–1,6 s etter.
- `ff07 0000 N` øker med én for hvert korte trykk og står stille ved hold. Den kom ca. 3,4 s etter
  trykket. Telleren hoppet fra `14` til `00` 20.10 08:26 (ukjent hvorfor). Brukes ikke.
- Holdt knapp gir `BTN 00 08` omtrent hvert 1–1,5 s.
- **I integrasjonen (0.7.0rc6):** for brytere med `BTN 00 xx` gjelder et vindu på 8 s fra hvert trykk.
  En annen knapp de første 2,5 s regnes som etterslep, og HA husker hvilken knapp etterslepet
  navngir. Senere i vinduet ignoreres bare den knappen og den trykkede; andre knapper er nye trykk.
  Tap: en annen knapp innen 2,5 s etter et trykk, eller etterslepsknappen innen 8 s. Gen4 (`BTN 01 01`)
  er ikke berørt. (rc5 ignorerte bare `02` og klarte ikke etterslep med `00`.)

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
| 2 | Les fastvare | `67caff5ee7a4` | `ff5f e7a4 …` med 7 byte etter taggen |
| 3 | Maks 90 % | `67caff29e7a4032a` | `ff08 e7a4 29 03 …`. Sett lampen til 100 % i HA: den skal lyse svakere. Les trim igjen (test 1): MAKS = `2a` |
| 3b | Tilbake til 100 % | `67caff29e7a40332` | `ff08 e7a4 29 03 …` |
| 4 | Fading 3 s (Gen4) | `67caff48e7a41e00` | `ff08 e7a4 48 …`. Slå av, og så på igjen fra et høyt nivå: lyset skal tones inn |
| 5 | Trykkbryter normal (Gen4) | `67caff47e7a400` | `ff08 e7a4 47 …`. Normal er trolig fabrikkverdien, så dette endrer ingenting |
| 6 | Fang paring fra appen | – | Mens fangsten går: i SG-appen, sett én knapp på en Smart Switch som fortsatt er på gatewayen til å styre en dimmer. Se etter `FF13` fra `8000` til bryteren, og svaret |

Test 6 er den raskeste veien til ekte bytes. Det samme gjelder trim og fading, hvis du endrer dem i
SG-appen for en enhet som fortsatt er på gatewayen mens fangsten går.

## 7. Åpent

- `FF08` har ingen status-byte for godtatt eller avvist på CSR. Integrasjonen regner kvittering som
  godtatt og stillhet som «send igjen». Byte 3 og videre i `FF2B` er fortsatt ukjent.
- Én stor vippe (app-valget `singleButton`): integrasjonen sender `FF17`/`FF13` for knapp 0 og 3, og
  regner topp som knapp 0+3 og bunn som 1+2 i `FF15`. Ikke testet på maskinvare ennå (test i 0.7.0rc1).
- HeatReg: sensormodus (rom/gulv) står ikke i statusen. Integrasjonen viser gulvtemperaturen, som
  gatewayen. Betydningen av flaggbit 4 og 7, og av `FF5C`, er ikke kartlagt. Ingen `FF08` for `FF42`.
- Modellen kan ikke leses over meshen. Navnet (`@GDxxxx`, også i `FF11`) kan gi typen, men det er bare
  bekreftet for `GD` = `DimmerGen4` (`9640`). `MO` er en LEDDim Smart Pill Micro ifølge SG-appen
  (`MODBC3`).
- Bryterens egen paringstabell kan ikke leses. HA fører sin egen oversikt.
- Smart Switch V2: hvorfor bryteren sender etterslep, og hva `ff07 0000 N` brukes til (6.1, `9280`).

## 8. I integrasjonen (0.7.0rc2)

| Funksjon | Kommandoer | Verifisert |
|---|---|---|
| Min / oppstart / maks, lest og satt | `FF2A`/`FF2B`, `FF29` | ✓ (`9640`, `89e0`) |
| Fading og dimmehastighet | `FF48` | ✓ fading, hastighet ikke observert |
| Trykkbryterinngang | `FF47` | kvittert |
| Fastvare på enhetssiden | `FF5E`/`FF5F`, HeatReg fra `FF05` | ✓ |
| Koble / fjerne bryterknapp | `FF12`, `FF17`, `FF13` | ✓ (`c3e4`), dialogen rettet i rc2, stor vippe ikke testet |
| Batteri på bryter | `83 01` | ✓ |
| HeatReg | `FF04`/`FF05`, `FF3C`/`FF3D`, `FF3E`, `FF42` | ✓ (`f03f`) |
