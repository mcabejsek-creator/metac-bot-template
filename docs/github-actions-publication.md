# Publikování z nezávislých GitHub Actions runnerů

Následná větev `fix/bounded-runtime` doplňuje [časové limity a bezpečné
zrušení](runtime-limits.md). Níže je stav a ověření tohoto publikačního kroku
`8929cac`; následný wrapper mění spouštění, nikoli zde popsaný CAS protokol.

Navazuje na `d44239a`; implementace je pouze připravená ve větvi
`fix/github-actions-publication`. Nebylo provedeno živé API volání, inicializace
vzdálené větve, změna Secrets, push, merge ani nasazení. Skutečná oprávnění a
chování služeb nebyly testovány online; testy simulují popsané REST kontrakty.

## Audit všech workflow

| Workflow | Původní stav | Připravená ochrana |
| --- | --- | --- |
| `run_bot_on_tournament.yaml` | Ruční, bez concurrency; výchozí tournament mód publikuje. | Společná skupina, trvalý GitHub backend. Ruční trigger a příkaz zachovány. |
| `test_bot.yaml` | Ruční; skupina podle názvu workflow; `test_questions` také publikuje. | Stejná skupina, scope a backend jako produkce. |
| `run_bot_on_metaculus_cup.yaml` | Ruční + `0 0 */2 * *`; vlastní skupina; záměrně přeforecastovává. | Stejná skupina a backend. Cron i `skip=False` chování zachovány. |
| `review_bot.yaml` | Ruční + `0 6 * * 1`, volitelně zapnutý; review příkaz a výstupní artefakty. | Vlastní skupina zůstává; GitHub token má jen `contents: read`. Nepracuje s žurnálem. `days` jde přes citovanou env proměnnou a musí být nezáporné číslo. |
| `initialize_publication_state.yaml` | Nové servisní workflow. | Pouze explicitní ruční inicializace po potvrzení vyjasnění předchozích běhů; stejná skupina jako všichni zapisující. Nespouští bota. |

Společná skupina je `metaculus-publication-${{ github.repository }}` bez názvu
workflow, větve nebo run ID. `cancel-in-progress: false` nepřeruší aktivní zápis
kvůli novému běhu. Concurrency není FIFO ani záruka, že každý pending run proběhne;
GitHub může čekající běhy nahradit. Nové záměrné běhy Cup/test režimu mohou znovu
forecastovat; rerun pokusu stejného `GITHUB_RUN_ID` obnoví původní záměr. Číslo
`GITHUB_RUN_ATTEMPT` netvoří nový záměr a nesmí obejít rozpracovaný zápis.
Publikační workflow i inicializátor nejprve spustí offline regresní sadu, v kroku
bez Metaculus Secrets; teprve pak přichází původní bot nebo inicializační příkaz.
Review vstup už není vložen přímo do shellového kódu: tím se zavírá cesta, jak
by přes `days` mohl být spuštěn jiný příkaz mimo společný publikační zámek.

**Concurrency sama nestačí.** Neuchová záznam o POSTu po zániku VM a neřeší, co se
stalo těsně před přerušením běhu. Cache/artefakt uložený až na konci jobu také
nepokrývá tuto mezeru; cache může být navíc zastaralá nebo odstraněná.

## Trvalý stav uvnitř GitHubu

`github_publication.py` doplňuje backend stejného žurnálu z předchozí opravy.
Nepřidává externí databázi, PAT ani novou placenou službu. Používá automatický
`GITHUB_TOKEN` s `contents: write` a GitHub REST Contents API.

- Vyhrazená větev: `metaculus-publication-state`. Zdrojová větev se nemění.
- Soubor `publication.enc` obsahuje šifrovaný index známých otázek.
- `publication.enc.d/<HMAC>.enc` obsahuje šifrovaný SQLite žurnál jedné otázky,
  včetně uloženého reportu, plánovaných kroků a stavů různých záměrů.
- Nová část vzniká atomickým create-if-absent bez `sha`; při konfliktu se přečte
  vítězný soubor. Existující část se mění pouze s očekávaným blob `sha`.
- Žurnál otázky se nejprve uloží, poté zaregistruje v indexu. Teprve obě potvrzení
  dovolí navazující rezervaci zápisu. Osiřelou autentizovanou část po pádu při
  registraci lze znovu zaregistrovat bez změny jejího obsahu.
- Chybějící část **registrované** otázky je chyba, nikdy nový prázdný žurnál.
  Chybějící/poškozený index se také automaticky nevytváří.
- Běžný checkpoint přepisuje jen příslušnou otázku. Neukládá se celý šifrovaný
  archiv všech reportů při každé operaci; nezávislé otázky si nepřepisují stav.

Pro každý krok platí pořadí: trvale uložený plán → potvrzený GitHub commit stavu
`sending` → jednopokusový POST Metaculus → potvrzený GitHub commit výsledku.
Kód ověřuje blob SHA přesného odeslaného checkpointu a commit SHA v odpovědi.
Konfliktní změna se může omezeně zopakovat **jen na transakci žurnálu**, nikdy
na Metaculus POSTu. Zastaralé čtení nemůže přepsat novější blob při podmíněném PUT.
Neočekávaná odpověď, chybějící potvrzení nebo nedostupná služba nedovolí POST.
V Actions není automatický fallback na lokální databázi.

| Místo pádu | Co udělá další nezávislý runner |
| --- | --- |
| Před trvalým uložením plánu | Žádný Metaculus POST nebyl autorizován. |
| Plán existuje, ztratilo se potvrzení uploadu/indexu | Načte autentizovaný uložený plán a dokončí registraci. |
| GitHub přijal `sending`, jeho odpověď se ztratila | Metaculus se v tomto běhu nevolá; další runner stav ponechá zablokovaný. |
| Metaculus mohl přijmout POST, odpověď se ztratila | `sending/uncertain` přežije zánik VM; žádné automatické opakování. |
| Metaculus odpověděl úspěšně, potvrzení výsledku v GitHubu selhalo | Je-li checkpoint skutečně uložený, další runner ho přečte. Jinak zůstává blokace. |
| Předpověď je potvrzená, komentář jednoznačně odmítnutý | Další runner načte původní report a zkusí pouze komentář, bez modelů a přeforecastování. |

Záruka vychází z podmíněných aktualizací GitHub Contents API, nikoli pouze z
workflow zámku nebo neověřeného `Idempotency-Key` na Metaculus. Nezávislé runnery
nesdílejí soubory ani paměť; sdílejí pouze tuto GitHub větev.

## Soukromí a provozní limity

Git větev může být veřejná, proto jsou index i reporty šifrované AES-GCM.
HKDF se samostatnou doménou odvodí klíč z **existujícího** `METACULUS_TOKEN`;
HMAC skrývá názvy částí. AAD váže ciphertext na repozitář, větev, cestu, scope a
konkrétní otázku. Záměna částí nebo poškození se odmítne. Hlavičky ani tokeny se
do žurnálu neukládají. `cryptography` již je v původním lockfilu; závislosti se
neměnily. Tento režim vyžaduje skutečný stabilní tajný token z Actions, nikoli
proxy placeholder. GitHub token se pro šifrování nepoužívá, protože se mění.

Z toho plynou konkrétní omezení: změna Metaculus tokenu/scope/umístění vyžaduje
předem připravenou migraci indexu i částí při zastavených writerech. Bez ní se
publikování zablokuje, nikoli začne od nuly. Znalost starého tokenu dovoluje
dešifrovat historické ciphertexty i po jeho odvolání pro API; jako u šifrované
zálohy nelze revokovat již získaný klíč. Časy commitů a velikosti souborů nejsou
skryté. Kdo tento způsob uložení nechce, má použít jiné důvěryhodné trvalé úložiště.

Limit inline souboru je konzervativně 900 000 B, dekomprimovaného snapshotu 64 MiB.
Překročení limitu, API/rate limit, nedostatečné oprávnění nebo pravidlo větve
zastaví publikování. Velmi dlouho přeforecastovávaná otázka může nakonec potřebovat
řízenou migraci. Potvrzené identity se nesmějí automaticky promazávat. REST
požadavky a Git historie spotřebovávají běžné limity GitHubu; nevzniká nová služba,
ale není tu garance neomezené kapacity či nepřetržité dostupnosti.

## Jak zjistit přijetí Metaculus zápisu

Zkontrolován byl lokální SDK kontrakt `forecasting-tools 0.2.92`:

- Forecast POST na `/questions/forecast/` obsahuje question ID, `source=api` a
  předpověď. Neodesílá ani nevrací v použitém rozhraní identitu našeho záměru.
- `questions.py` čte `my_forecasts.history/latest`, hodnoty a časy. Stejná hodnota
  může pocházet z jiné publikace; samotné `already_forecasted` není potvrzení
  konkrétního nejasného požadavku. Prázdná či opožděná historie není důkaz opaku.
- Komentář se vytváří přes `/comments/create/`; SDK writer neuchovává serverový
  identifikátor ani neposkytuje ověřený kontrakt úplného, konzistentního čtení
  soukromých komentářů. Shoda textu bez vazby na operaci také nestačí.

Bez živého ověření nebo autoritativní specifikace nelze tvrdit, že API jiné
možnosti nemá. **Z dostupných podkladů ale nelze bezpečně implementovat automatické
vyjasnění.** Neprovádí se heuristické odblokování podle textu, času nebo historie.
Zůstává explicitní `reconcile` z předchozí opravy, až po ověření výsledku na serveru
a zastavení původních workerů. Automatické „exactly once a vždy dokončit“ nelze
zaručit ani s databází, pokud Metaculus neposkytne idempotentní klíč nebo jednoznačný
serverový receipt. Bezduplicitnost má přednost před neověřeným retry.

## Podmínky budoucího nasazení — nyní neprovedeno

Nejprve je nutné ukončit staré aktivní/čekající writery a vyjasnit jejich neznámé
zápisy. Nový kód neumí zpětně donutit historické workflow SHA používat nový zámek
a žurnál. **Nespouštět reruny starých verzí**, další skripty ani jiné repozitáře
se stejným účtem mimo společnou koordinaci. Všechny tři publikující workflow musí
používat stejnou větev, cestu, scope a token daného účtu.

Po review a nasazení se jednorázově ručně spustí `Initialize publication state`
s potvrzením `all-prior-runs-reconciled`. Vytvoří novou orphan větev s kompletním
šifrovaným indexem; existující ref se nikdy neresetuje. Nepotřebuje nový Secret,
ale repozitář musí dovolit `contents: write` a běžné commity do stavové větve.
Při ztrátě potvrzení inicializace se stav nejprve ověří, nikoli přepíše.

Stavovou větev a její soubory nemazat, nevracet na staré verze a neforce-pushovat;
zajistit odpovídající ochranu a přístupová práva. Správce s možností vrátit celé
úložiště do minulosti může porušit každý klientský žurnál. Zálohy nesmějí být
obnoveny jako čerstvý prázdný stav. Lokální SQLite zůstal pro vývoj mimo Actions.

Pokud GitHub oprávnění, kvóty nebo tento typ úložiště nevyhoví, nejjednodušší
alternativou je **jeden self-hosted runner s trvalým lokálním SQLite a společnou
concurrency skupinou**, s výslovně ověřenou konfigurací lokálního backendu.
Tato větev lokální fallback v Actions záměrně nepovoluje. Externí transakční služba
je potřeba až tehdy, když musí zapisovat více hostů a nelze využít GitHub stav.
Ani jedna alternativa neodstraní nejasný výsledek Metaculus POSTu bez podpory API.

## Offline ověření

`python -B -m tests.run`: **120 testů, 103 prošlo, 17 očekávaných selhání** ostatních
známých chyb; žádné neočekávané selhání, chyba, skip ani pokus o živé volání.
Stejný výsledek má běh se simulovanými Actions proměnnými, GitHub backendem a
opakovaným run attempt. Oproti `d44239a` je opravený také kontrakt `CONCURRENCY-2`;
zbývajících 17 známých selhání se nezměnilo. Přísný režim skončí správně kódem 1
a vykáže právě těchto 17 chyb, bez dalších chyb či síťových pokusů.

Nové testy pokrývají nezávislé runnery, souběžné create/CAS, zastaralé čtení,
ztracená potvrzení plánu/registrace/rezervace/výsledku, částečný zápis, reruny,
chybějící části, šifrování a ruční vyjasnění s přesným hashem operace. Ověřena byla
také syntaxe Python/YAML/Bash a zachování původních triggerů, cronů a bot příkazů.
`main.py`, lokální transakční logika, prognostické prompty a závislosti zůstaly
beze změny. Offline testy na Pythonu 3.12 nenahrazují ověření oprávnění a služeb při
pozdějším zavedení; skutečný Actions runner s Pythonem 3.11 zde spuštěn nebyl.
