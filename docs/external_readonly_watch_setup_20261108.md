# Bezpečná příprava externího čtecího hlídače (8. 11. 2026)

## Stav: POUZE NÁVRH, NIC NESPOUŠTĚT

Tato příprava leží výhradně v **draft PR #7**. Žádný nový cron-job.org job
nebyl vytvořen ani aktivován, žádný živý monitor nebyl ručně spuštěn, do
`main` nebyla sloučena žádná změna a produkční bot zůstává vypnutý.

## Architektura

1. **Nový, nezávislý** cron-job.org job (výchozí stav `enabled: false`).
2. V budoucnu po **samostatném schválení** odešle POST jen na GitHub API:
   `https://api.github.com/repos/mcabejsek-creator/metac-bot-template/actions/workflows/external_readonly_metaculus_watch.yaml/dispatches`
3. JSON tělo: `{"ref":"main","inputs":{"mode":"CHECK_OPEN_POSTS_READ_ONLY"}}`.
4. Workflow `.github/workflows/external_readonly_metaculus_watch.yaml`:
   - pouze `workflow_dispatch`, žádný `schedule`, `push` nebo `repository_dispatch`;
   - default `DO_NOT_RUN`, požadavek na `main`, explicitní čtecí volba,
     první `run_attempt`, `contents: read`, žádné publikační kroky;
   - nejprve **offline self-check**, teprve pak existující hlídač
     `tools/watch_open_metaculus_questions_readonly.py`;
   - při každém úspěšném běhu maximálně **2 autentizované GET** na Metaculus:
     první stránka Fall FutureEval a MiniBench, žádný POST ani placený model;
   - hlídač **NEOVĚŘUJE**, zda jsou otázky opravdu způsobilé k předpovědi,
     neprochází další stránky a není oprávněním k publikování.
5. Konfigurace cron-job.org: `config/cronjob_external_readonly_disabled.template.json`.
   Je **veřejná pouze jako vzor**; hlavička Authorization je nefunkční zástupný
   text, v repozitáři se nikdy nesmí objevit skutečný token.

## Důležitá bezpečnostní hranice tokenu

GitHub API `workflow_dispatch` vyžaduje fine-grained GitHub token s
`Actions: write` pro vybraný repozitář. Tato pravomoc se **neváže jen na jedno
workflow**; tentýž token může spustit i jiná workflow ve stejném repozitáři.
Proto:

- Nikdy neaktivovat externí plánovač, **dokud** default-off publikační
  pojistky všech tří live workflow v PR #7 nebudou samostatně schválené,
  bezpečně sloučené do `main` a ověřené tam.
- Doporučený PAT: *fine-grained*, jen
  `mcabejsek-creator/metac-bot-template`, repository permission
  **Actions: Read and write** a ostatní nepovinná oprávnění bez přístupu;
  krátká expirace (např. 30 dnů), průběžná rotace.
- PAT vložit výhradně do zabezpečené konfigurace **nového** cron-job.org jobu;
  nedávat ho do URL, GitHubu, chatu, issue ani repozitáře.
- **Metaculus V2 token zůstává jen v GitHub Actions secrets**, nikdy se
  nekopíruje do cron-job.org. Externí služba pouze vyvolává GitHub workflow.
- Vypnutý původní cron-job.org job, který dispatchoval
  `run_bot_on_tournament.yaml`, musí zůstat vypnutý a nesmí se upravovat
  na novou úlohu (risk záměny URL).
- Pro nejstriktnější oddělení pravomocí by šlo zřídit samostatný repozitář
  obsahující pouze čtecí monitor, ale to je další změna vyžadující souhlas.

## Nastavení v cron-job.org po budoucím schválení

1. Otevřít `https://console.cron-job.org/` → **Create cronjob**;
   vytvořit **nový** job, neobnovovat původní.
2. Titulek: `Vrchcab – EXTERNAL Metaculus WATCH ONLY`.
3. URL přesně z oddílu Architektura, metoda **POST**, typ
   `application/json`; tělo JSON viz výše.
4. Hlavičky: `Authorization: Bearer <fine-grained GitHub PAT>`,
   `Accept: application/vnd.github+json`,
   `Content-Type: application/json`;
   `X-GitHub-Api-Version: 2026-03-10`.
5. Interval **každých 15 minut**, časové pásmo `UTC`, minuty
   `1,16,31,46`. Se současným prvním-stránkovým monitorem to znamená
   maximálně **192 Metaculus GET/den** při pravidelném běhu, nikoli POST.
6. Nastavit `Save responses: off`, upozornění na chybu i automatické vypnutí,
   timeout 30 sekund, bez následování přesměrování. **Enabled: off**.
7. Zkontrolovat výchozí `main`: oba požadované prvky musí být po samostatném
   merge dostupné v GitHubu. Především tři live publikátory musí skutečně
   obsahovat `publication_approval` s výchozím `DO_NOT_RUN`.
8. Až po zvláštním schválení provést **jediný testovací dispatch**, ověřit
   v Actions úspěch offline kroku, pak read-only kroku a nanejvýš 2 GET.
   HTTP 2xx na volání dispatch *jen přijímá požadavek*, samo neprokazuje
   úspěšný monitor (ten se ověřuje na stránce Actions).
9. Teprve potom řešit povolení opakování a případné vypnutí nespolehlivého
   interního `schedule`, aby nechodily zbytečné zdvojené GET.

GitHub aktuálně dokumentuje u dispatch endpointu odpověď **200** s ID běhu;
historické rozhraní mohlo vracet 204. V obou případech je potřeba kontrolovat
výsledný Actions běh, nikoli jen HTTP odpověď externího plánovače.

## Diagnostika předchozího HTTP 422

Cron-job.org 7. listopadu automaticky vypnul starý externí job po **26**
následných neúspěšných spuštěních. Poslední odpověď GitHubu byla HTTP 422
na cíli `run_bot_on_tournament.yaml/dispatches`. To prokazuje **odmítnutí
dispatch požadavku**, nikoli konkrétní příčinu; bez kompletního GitHub
response body nelze rozlišit zakázaný workflow, nesprávný ref/inputs,
omezení API nebo jiný validační problém. Nikdy z toho nevyvozovat, že stačí
starý cron prostě znovu zapnout.

## Referenční dokumentace

- GitHub REST API, workflow dispatch:
  https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event
- cron-job.org REST API:
  https://docs.cron-job.org/rest-api.html
- Zdrojový bezpečně otestovaný hlídač:
  `tools/watch_open_metaculus_questions_readonly.py`.
