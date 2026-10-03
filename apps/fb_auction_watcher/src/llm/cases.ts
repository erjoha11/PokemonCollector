// Evaluation cases for Claude (claude -p through the native bridge), from real posts in samples/
// with names replaced ("Selger Testesen" is the seller). Each has the answer a careful human
// would give. Run them with `npm run eval:claude` (opt-in: uses your Claude plan, needs
// native/install.sh); tests/claude-eval.test.ts is skipped otherwise. 23/23 on 2026-10-03 (Haiku).
// The reference "today" for relative dates is Saturday 3 October 2026, Europe/Oslo.

export const TODAY = "Saturday 2026-10-03";

export type EndTimeCase = { text: string; want: string | null; rulesCanDoIt: boolean };
export type BidCase = { seller: string; text: string; want: number | null; rulesCanDoIt: boolean };

export const END_TIME_CASES: EndTimeCase[] = [
  { text: "AUKSJON/BUDRUNDE\nMinstepris: 10kr\nSluttid: Søndag 04.10.2026 KL 18.00\nAntisnipe 5 min: Ja", want: "2026-10-04 18:00", rulesCanDoIt: true },
  { text: "Claim salg-annonse\nSluttid (Lørdag 3. oktober 23.59):\nObjektbeskrivelse: Holo kort", want: "2026-10-03 23:59", rulesCanDoIt: true },
  { text: "AUKSJON\nSluttid: Ikveld 3/10, kl 22.00\nAntisnipe 5 min: Ja", want: "2026-10-03 22:00", rulesCanDoIt: true },
  { text: "Gengar AUKSJON\nMinimum budøkning: 10kr,-\nSluttid: 03.10 Lørdag kl22:00", want: "2026-10-03 22:00", rulesCanDoIt: true },
  { text: "Claimsalg\nStart: 03.10.26 kl 12:00\nSlutt: 05.10.26 kl 21:00\nPris: står på kortet", want: "2026-10-05 21:00", rulesCanDoIt: true },
  { text: "AUKSJON/BUDRUNDE-annonse\nMinstepris: 2800kr\nSluttid: 03.10.26 kl 18:00", want: "2026-10-03 18:00", rulesCanDoIt: true },
  { text: "Budrunde på disse kortene! Auksjonen avsluttes søndag kveld klokka ni. Minstepris 50kr", want: "2026-10-04 21:00", rulesCanDoIt: false },
  { text: "LYNAUKSJON\nAvsluttes i morgen kl 20\nMinstepris: 100,-", want: "2026-10-04 20:00", rulesCanDoIt: false },
  { text: "Auksjon på Charizard, slutter mandag 5. oktober halv ti på kvelden", want: "2026-10-05 21:30", rulesCanDoIt: false },
  { text: "30th Celebration-auksjon, avsluttes ikveld\nAUKSJON/BUDRUNDE-annonse\nMinstepris: 1kr på hvert salg", want: null, rulesCanDoIt: false },
  { text: "FASTPRIS-annonse\nFastpris: 950kr\nObjektbeskrivelse: Gardevoir ex Delta Species", want: null, rulesCanDoIt: true },
];

const S = "Selger Testesen";
export const BID_CASES: BidCase[] = [
  { seller: S, text: `${S} 250`, want: 250, rulesCanDoIt: true },
  { seller: S, text: "850", want: 850, rulesCanDoIt: true },
  { seller: S, text: `${S} 110kr`, want: 110, rulesCanDoIt: true },
  { seller: S, text: `${S} 1.400`, want: 1400, rulesCanDoIt: true },
  { seller: S, text: "2.5k", want: 2500, rulesCanDoIt: true },
  { seller: S, text: `${S} 200 sorry mente 250`, want: 250, rulesCanDoIt: false },
  { seller: S, text: `${S} 580?`, want: 580, rulesCanDoIt: false },
  { seller: S, text: `${S} byr 300 på denne`, want: 300, rulesCanDoIt: false },
  { seller: S, text: `${S} .`, want: null, rulesCanDoIt: true },
  { seller: S, text: "Sendt PM, sjekk meldingsforespørsler og spam", want: null, rulesCanDoIt: false },
  { seller: S, text: `${S} kan du sende flere bilder av baksiden?`, want: null, rulesCanDoIt: false },
  { seller: S, text: `${S} 10 mer enn høyeste bud`, want: null, rulesCanDoIt: false },
];
