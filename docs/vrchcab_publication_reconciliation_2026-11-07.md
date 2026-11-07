# Vrchcab publication reconciliation: evidence snapshot (2026-11-07)

**Scope:** Read-only investigation of GitHub Actions metadata and job logs, with no forecasts submitted, no historical writes retried, and no credentials exposed. This is NOT an exhaustive publication inventory or authorization to initialize a new journal.

## Verified infrastructure state

- Repository: `mcabejsek-creator/metac-bot-template`.
- Production `main` HEAD at last check: `f0da8ce7b3351f48115fdeb8394fcfb2e88c1341`. No production merge during this investigation.
- Rollout PR #1 stays open/unmerged; diagnostic PR #2 stays a separate draft.
- Exactly one workflow run remained queued (run `34463156990`, created 2026-09-10 09:53:49 UTC, old commit `e521169cc6bc9cdba5b9e83efeafd020e430f76c`). Its GitHub jobs endpoint returned zero jobs, and no artifacts were available. This does NOT prove it can never start if GitHub repairs the queue.
- At the check, GitHub returned zero runs in `in_progress`, `waiting`, `pending` or `requested` states. Other than the old run there were zero `queued` runs.

## Confirmed historical forecast posts

Two distinct October 5 workflow logs explicitly contain the SDK message `Posted prediction on question ...`. No question IDs or forecast values are reproduced here:

| GitHub run | Evidence timestamp (UTC) | Recorded estimated model cost |
|---|---|---|
| [37306672801](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/37306672801) | 2026-10-05 12:03:46 | USD 0.12956 |
| [37373289415](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/37373289415) | 2026-10-05 21:08:08 | USD 0.13630 |

These are evidence of two acknowledged publication events in **sampled** logs, not a full list of published forecasts.

Some neighboring success logs loaded open MiniBench questions but returned `No new questions to forecast on this run`; this is compatible with the SDK skipping questions already forecast.

## Overlapping/cancelled runs that remain ambiguous

The following workflow runs are marked completed with failure/cancelled jobs, but their GitHub job logs currently return `BlobNotFound`, and no run artifacts are available:

- [37366002616](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/37366002616): 2026-10-05 19:50:41–20:05:44 UTC.
- [37367114796](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/37367114796): 2026-10-05 20:01:06–20:16:10 UTC.
- [37372241017](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/37372241017): 2026-10-05 20:50:40–21:05:45 UTC.

The 20:50 run overlapped the 21:01 run, which later reported one confirmed publication. This is **an overlap and an uncertainty**, not proof of duplicate publication. A cancelled run can, in principle, have made a POST before cancellation; absence of logs cannot reconcile it.

## API diagnostics, limitations

- On 2026-11-07 11:39 UTC a previously user-approved, one-time authenticated Metaculus check returned **zero open posts on first page** in Fall FutureEval 2026 (`33121`) and MiniBench (`minibench`). Two GET requests were sent, no POSTs. This describes only that point in time.
- Three earlier history-audit attempts failed. The third failed with HTTP 429, and the later pinned audit script was not subsequently invoked.
- An offline method-compatibility check on installed `forecasting-tools 0.2.92` found that this release does not expose the upstream `_get_acting_user_forecasts` helper being compared. Therefore any suspected GET-vs-POST mismatch between the historical-audit script and a newer upstream SDK remains **unproven for the installed release**. Do not infer the historical audit is correct or incorrect from this comparison.
- GitHub logs are incomplete; this report does NOT establish a complete history for Vrchcab (Metaculus user 307558), nor reconcile uncertain POST acknowledgments.

## Required before first new publication

1. Obtain an authoritative resolution for old queued run `34463156990`, ideally GitHub Support clearing it; or otherwise establish a technically enforceable barrier to **every** old publisher execution.
2. Validate a bounded, account-specific historical forecast inventory using a Metaculus-compatible read method, within API limits and only with explicit authorization for any further real API calls. Investigate known overlapping/cancelled windows separately.
3. Reconcile uncertain historical writes and prove new journal initialization will not incorrectly claim a blank or comprehensive history.
4. Verify all production publication entry points share the new lock and journal; check latest main/PR integration offline.
5. Obtain user's explicit consent before merge, journal initialization, any live forecast or scheduling.

**Safety:** This document is evidence only. No production merge, initialization, scheduler restart or live write is authorized.

## Potvrzený projektový inventář (2026-11-07, 12:34 UTC)

Na výslovné schválení uživatele byl přes nový `METACULUS_TOKEN_V2` spuštěn samostatný jednorázový, čistě čtecí GET audit dvou soutěží. [Úspěšný běh 42373102862](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/42373102862). V CI prošlo všech šest syntetických offline testů, po nich následovaly přesně dva skutečné GET požadavky, žádné POST ani volání LLM. Jednorázový spouštěč byl po použití odstraněn.

| Projekt | Otázky vrácené API | Otázky s historií | Historické záznamy | Nejstarší záznam UTC | Nejnovější záznam UTC |
|---|---:|---:|---:|---|---|
| Fall FutureEval 2026 (33121) | 119 | 111 | 111 | 2026-09-28 14:21:25 | 2026-11-06 12:02:17 |
| MiniBench (minibench) | 60 | 60 | 60 | 2026-11-02 00:47:01 | 2026-11-03 22:47:04 |
| **Celkem** | **179** | **171** | **171** | | |

API v každé z 171 otázek s předpovědí vrátilo právě jeden historický záznam. To je důležitý **důkaz absence opakovaných předpovědí na jednu otázku uvnitř těchto dvou projektů**, nikoliv absolutní garance neexistence jakýchkoliv duplicit mimo projekty ani potvrzení všech možných nejednoznačných pokusů. Osm otázek vrácených z FutureEval nemá v API historii tohoto bota; nelze z toho usuzovat, zda zůstaly kdykoliv předpověditelné či měly soutěžní okno.

**Reconciliation flag zůstal FALSE.** Rozsah čtení je omezen na dva projekty, data viditelná účtu Vrchcab a současný stav API. Projektově zaměřené GET nevylučují nejasné starší publikace v dalších turnajích/testovacích režimech. Opačně, historické potvrzené příspěvky v GitHub logách jsou s tímto inventářem slučitelné, ale jejich přesné mapování na otázky nebylo provedeno (záměrně se neukládala ID).

### Další bezpečné kroky

1. Dokončit statickou kontrolu všech starých publikačních vstupů a nové rotace tokenu. Starý běh 34463156990 zůstal ve frontě, ale API před rotací přijímalo jeho token a po rotaci odmítá starý token HTTP 403; screenshot Metaculus potvrdil regeneraci s deklarovaným zneplatněním starého klíče.
2. Na oddělené větvi otestovat integraci PR #1 a PR #3 včetně všech offline regresí; **bez merge do main**.
3. Ujasnit rozsah historické rekonciliace (zahrnuje i testování a Metaculus Cup); neočekávat, že pouze dva projekty tvoří veškerou historii.
4. Nezadávat inicializační potvrzení `all-prior-runs-reconciled`, dokud skutečně nemáme důkaz vyřešení všech nejistých publikací.
5. Teprve s novým explicitním souhlasem provést merge, inicializaci žurnálu, jeden kontrolovaný produkční běh a pozdější obnovení cron-job.org.

Žádné nové publikování zde nebylo povoleno.

## Offline integrace oprav s novým tokenem (7. 11. 2026)

Na oddělené diagnostické větvi byla po čistě lokálním merge ve **virtuálním GitHub runneru** integrována produkční `main` (`f0da8ce7b3351f48115fdeb8394fcfb2e88c1341`) s návrhem PR #3 (`7af78705f4833c3106221b988abed7c18759fdf6`), který zahrnuje celý opravný PR #1 a izolaci `METACULUS_TOKEN_V2`. Nebyl proveden žádný push/merge do produkční `main`.

- První offline integrace [42373366225](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/42373366225) selhala jedině proto, že původní regresní test v `tests/test_github_workflows.py` stále očekával staré GitHub secret `METACULUS_TOKEN`; nikoli kvůli selhání botovy logiky.
- Očekávání v testu bylo opraveno v návrhové větvi PR #3 (commit `7af78705f4833c3106221b988abed7c18759fdf6`), spolu s kontrolou, že se nepoužije starý secret.
- Opakovaný [offline integrační běh 42373534086](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/42373534086) byl úspěšný: **4/4 token isolation tests, 273/273 offline regressions, 0 unexpected live attempts**.
- Runner neměl `METACULUS_TOKEN`, `METACULUS_TOKEN_V2` ani LLM API tajemství. Jednorázový spouštěč byl po použití odstraněn.
- Production `main`, stavové úložiště `metaculus-publication-state`, první ostrý publish a cron-job.org nebyly změněny.

**Význam:** Technická kompatibilita balíku oprav a rotace přístupových údajů je podložena offline testy. **Nejde** o samostatný důkaz, že všechny staré pokusy byly rekonciliovány. Potřebujeme separátní výslovné schválení před produkčním merge, journal init a jakýmkoli živým odesláním předpovědí.

## Opakované publikace v testovacím režimu (ověřeno z historických logů)

Nezávislá a **bez-API** kontrola pěti dostupných GitHub Actions logů režimu `Test Bot` potvrdila, že se testovací předpovědi na stejné otázky opakovaly. Skript v analýze porovnal interně ID otázek uvedená v potvrzovacích logových hláškách, ale **žádná ID ani hodnoty prognóz nebyly vypsány do této zprávy**.

| Run | Datum UTC | Počet potvrzených POST |
|---|---|---:|
| [34461778866](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/34461778866) | 2026-09-10 | 10 |
| [39569483713](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/39569483713) | 2026-10-21 | 11 |
| [39570865331](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/39570865331) | 2026-10-21 | 11 |
| [39572843071](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/39572843071) | 2026-10-21 | 11 |
| [41676843011](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/41676843011) | 2026-11-03 | 10 |

Celkem **53 potvrzených událostí na 12 různých otázek**, z nichž **11 mělo opakované publikace** v těchto pěti vzorkovaných bězích. Maximálně 5 záznamů na jednu otázku v tomto vzorku. Toto není plný inventář testovacího projektu.

Výsledek je konzistentní s tím, že režim `test_questions` záměrně nastavuje `skip_previously_forecasted_questions=False` a publikuje. **Neprokazuje** nežádoucí duplicity v soutěžích FutureEval nebo MiniBench; u těch API vrátilo jednu historickou předpověď na každou z 171 otázek s historií. Je však nezbytné testovací režim zahrnout do návrhu izolace publikačních cest a rekonstrukce celkové historie.

