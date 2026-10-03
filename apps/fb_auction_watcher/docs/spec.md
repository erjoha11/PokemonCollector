# FB Auction Watcher – spec

- **Id:** `fb-auction-watcher`
- **Type:** Chrome-utvidelse (Manifest V3)
- **Eier/bruker:** Erik Johansen
- **Status:** spec, ingen funksjonalitet bygget ennå

## Problem

Jeg kjøper Pokémon-kort i én Facebook-gruppe med opptil ~100 auksjoner per dag.
Facebook sorterer på relevans/ny aktivitet/nye innlegg – aldri på sluttid. Det er umulig å
holde oversikt over hva som slutter når og hvor jeg leder eller er overbudt.

## Mål v1

- Én tabell over alle salg i gruppa, gruppert og sortert på sluttid, med live nedtelling.
- Lots jeg har budt på fremheves (Leder / Overbudt).
- Åpne en auksjon og se lots og bud i et ryddig overlegg i stedet for kommentarfeltet.
- Kun Chrome på PC/Mac. Ingen mobil, ingen server, ingen webapp.

## Domenet

```
Innlegg = listing: oversiktsbilder av hele auksjonen, regler, sluttid
 └ Toppnivå-kommentar MED bilde = lot (singel eller bundle)
    └ Svar under lot-kommentaren = bud (navn, beløp, tid)
```

- Kommentarer uten bilde er prat. Svar fra selger er ikke bud.
- Salgstyper: auksjon, claim (første kommentar får kjøpe), fastpris.
- Close-regler: hard close, eller soft close (bud nær slutt forlenger, f.eks. 5 min).
- Sluttid og regler står i fritekst ("slutter søndag kl 20") → tolkes i `Europe/Oslo`.
- Mitt Facebook-navn: Erik Johansen (konfigurerbart).

## Ufravikelige regler

- **KUN LESING.** Utvidelsen byr, claimer, kommenterer eller liker aldri. Eneste klikk:
  "Vis flere kommentarer", "Vis N svar" og feed-sortering. (Høyeste bud er bindende.)
- Ingen headless/server-scraping. Alt kjører i min egen innloggede Chrome.
- Rolig tempo: feed-skann hvert 10–15 min ±20 %, pause når PC er låst (`chrome.idle`),
  aldri parallelle faner mot Facebook. Må kunne slås av.
- Selgerens originaltekst vises alltid ved siden av tolkede verdier.
- Råtekst lagres (capture-tabell) så tolkning kan kjøres på nytt.
- Aldri CSS-klasser som selektorer (Facebook obfuskerer). Bruk role, aria-label,
  struktur og tekstmønstre. Facebook virtualiserer lister og er en SPA.

## Arkitektur

- **Content scripts:** (a) feed-skann i en festet gruppefane, (b) post-leser + overlegg (Shadow DOM).
- **Service worker:** koordinering, kall til Claude API for tolkning, lagring.
- **Lagring:** IndexedDB (`idb`) bak et `Store`-grensesnitt, så Supabase kan byttes inn senere.
- **Sider i utvidelsen:** tabellside (`dashboard.html`), Chrome Side Panel.
- **Teknologi:** TypeScript, Vite, Preact, idb, zod. API-nøkkel i `chrome.storage.local`.

## Populering

- **Backfill første gang:** sorter på "Nye innlegg", scroll rolig, lagre hvert innlegg når det
  vises, stopp ved 3 dager gammelt. Deretter inkrementelt: stopp ved første kjente innlegg.
- En sjeldnere runde på "Ny aktivitet" fanger eldre innlegg med nye bud.
- **Detaljlesing (lots/bud)** bare: når jeg åpner en post, og automatisk hvert 15. min for
  auksjoner jeg har budt på. Resten leses ikke før de åpnes.

## Tolkning (LLM)

- **Feed-kall:** innleggstekst → `type`, `title`, `endsAt` (ISO), `endsAtText`, `closeRule`,
  `softCloseMinutes`, `closeRuleText`, `increment`, `price`, `shippingText`, `soldOrWithdrawn`.
- **Detalj-kall:** innlegg + kommentarer med svar →
  `lots[{commentId, kind, title, cards[], startBid, bids[{replyId, bidderName, amount, valid, note}]}]`.
  Regler:
  - `"250kr"` / `"250,-"` / `"bud 250"` = 250, `"2.5k"` = 2500
  - selger er aldri budgiver
  - `"200 sorry mente 250"` = 250
  - bud ≤ gjeldende høyeste = ugyldig
- Kun JSON, valideres med zod, ett nytt forsøk ved feil. Billig modell. Logg tokenbruk.
- **Etterbehandling i kode:** `highestBid`, `myStatus` (`none`/`lead`/`outbid`), tider.

## Datamodell

**listing:** `id`, `fbPostUrl` (unik), `sellerName`, `type`, `title`, `postedAt`, `endsAt`,
`endsAtText`, `closeRule`, `softCloseMinutes`, `closeRuleText`, `increment`, `price`,
`shippingText`, `thumbnailUrl`, `commentCount`, `commentCountPrev`, `lotCount`,
`myLotStatus{lead,outbid}`, `lifecycle`, `detailFetchedAt`, `firstSeenAt`, `lastSeenAt`

**lot:** `id`, `listingId`, `position`, `fbCommentUrl`, `kind` (`single`|`bundle`), `title`,
`cards[{name, set, number, condition}]`, `imageUrl`, `startBid`, `highestBid`,
`highestBidder`, `bidCount`, `myStatus`, `myHighestBid`

**bid:** `id`, `lotId`, `bidderName`, `amount`, `bidAt`, `rawText`, `isMe`, `valid`, `note`

**capture:** `id`, `listingId`, `kind` (`feed`|`detail`), `rawText`, `capturedAt`,
`parseStatus`, `parseError`

**settings:** `groupUrl`, `myFbName`, `scanIntervalMin` (12), `backfillDays` (3),
`captureEnabled`, `apiKey`

**userState:** `lastDashboardVisitAt`, `seenListingIds`

## Statuser

Per listing:

| Status | Betydning |
|---|---|
| Ny | Sett første gang etter siste besøk på tabellsiden |
| Aktivitet | Flere kommentarer enn sist (`commentCount` > `commentCountPrev`) |
| Slutter snart | Under 1 time igjen |
| Ukjent sluttid | Sluttid kunne ikke tolkes |
| Avsluttet? | Sluttid passert, men soft close-vinduet er ikke over |
| Avsluttet | Sluttid (og eventuelt soft close-vindu) passert |
| Solgt/trukket | Selger har markert salget som solgt eller trukket |

Per lot: **Leder** / **Overbudt**. En listing viser oppsummert, f.eks. "Leder 2 · overbudt 1".

## Design

- **Farger:** blå `#2457D6` = Leder, oransje `#C2570C` = Overbudt, rød `#B42318` = under 1 t.
- **Skrift:** IBM Plex Sans / IBM Plex Mono.
- **Språk:** norsk UI.

### Tabellside (`dashboard.html`)

- Fire tall øverst: aktive, innen 1 t, overbudt, nye.
- Grupper: innen 1 t · i dag · i morgen og senere · claim/fastpris · avsluttet.
- Kolonner: Slutter · Salg (tittel, selger, Ny, +N kommentarer) · Type · Lots · Bud ·
  Din status · Oppdatert.
- Filtre: Alle / Auksjon / Claim / Fastpris / Mine bud / Nye + søk.
- Rader jeg er aktiv i har farget venstrekant og kan foldes ut til "Dine lots i denne
  auksjonen" (bilde, høyeste bud, mitt bud, status).
- Klikk på tittel åpner Facebook-posten.

### Sidepanel

- Bryter Fanger / Pauset.
- Tellere: innen 1 t, overbudt, nye.
- Filtre: Alle / Mine bud / Innen 1 t.
- Kompakt liste + lenke til full tabell.

### Overlegg på posten

- Toppfelt: selger, tittel, stor nedtelling, close-regel, selgerens regler ordrett +
  tolkning, frakt, "Les på nytt".
- Velger: "Alle lots" / "Bare mine".
- "Dine lots" først (overbudt først, blå/oransje ramme, mitt bud vist), deretter "Andre lots".
- Valgt lot: budliste, høyeste og neste gyldige bud, knapp "By på Facebook" som skjuler
  overlegget og scroller til svarfeltet under lotet (skriver aldri).
- "Vis Facebook-siden".

## Plan

Én modul om gangen; jeg tester mellom hver.

| # | Modul | Innhold |
|---|---|---|
| 0 | Eksempler | 3–5 ekte auksjonsposter lagret i `samples/` (gitignored) |
| 1 | Spike | Content script som utvider kommentarer/svar og lager rå JSON for én post |
| 2 | Lagring | `Store`-grensesnitt på IndexedDB med tester |
| 3 | Tolkning | Claude API + zod + etterbehandling, testet mot samples |
| 4 | Feed-skann | Backfill + inkrementelt + idle-pause |
| 5 | Tabellside | |
| 6 | Overlegg | Inkl. automatisk gjenlesing hvert 15. min av auksjoner jeg har budt på |
| 7 | Sidepanel | |
| 8 | Senere | Prising, varsler, Supabase, lot-visning |

Byggerekkefølge i praksis: 0 → 1 → 3 → 2 → 4 → 5 → 6 → 7 (tolkning testes mot
spike-JSON før lagringen kobles på).

## Risiko

| Risiko | Tiltak |
|---|---|
| Metas vilkår forbyr automatisert innhenting | Reglene over: kun lesing, egen Chrome, rolig tempo, av-bryter |
| Facebook endrer HTML | `samples/` som regresjonstest; ingen CSS-klasse-selektorer |
| Selgere skriver ulikt | Vis alltid originaltekst ved siden av tolkning; lagre råtekst |
| Ikke sanntid | Vis "sist lest" på alt |

## Arbeidsform

- Vis plan før kode for hver modul.
- Kode og commits på engelsk, UI på norsk.
- Etter hver økt: oppdater denne spec-en med det vi har lært, og commit.
