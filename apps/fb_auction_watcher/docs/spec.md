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
`highestBidder`, `bidCount`, `myStatus`, …

> **TODO – ufullstendig:** forarbeidet ble kuttet her. Resten av `lot`, samt `bid`,
> `capture` (råtekst) og eventuelle seksjoner etter datamodellen mangler.
