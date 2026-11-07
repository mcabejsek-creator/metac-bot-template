# Závěrečný audit a plán nasazení

Navazující [konkrétní runbook](rollout-runbook.md) obsahuje aktuální kroky,
odpovědnosti a souhlasy. CPython **3.11.17** byl nyní skutečně otestován:
273/273 v strict režimu i simulovaných Actions. Main byl při posledním
autorizovaném čtení stále `91bf94b`, stavová větev chyběla. Reálná oprávnění a
staré běhy zůstávají k ověření vlastníkem; připravená offline CI šablona není aktivní.

**Stav po opravě PUB-MODE-1: kód připraven k řízenému nasazení po ručních
krocích níže.** Větev `fix/cross-mode-publication`: 273 testů prošlo v přísném
režimu i simulovaných Actions, bez selhání a živých pokusů. Původní NO-GO z
`0a98f97` je odstraněno, test zůstal běžnou regresí. Podrobnosti a nový požadavek
kompatibility SQLite v2 jsou v [opravě koordinace mezi režimy](cross-mode-publication.md).
Oprávnění, aktuální remote refs, Secrets a běžící produkce v tomto kroku nebyly
online ověřovány; žádné nasazení neproběhlo.

## Porovnání s produkcí z auditu 0a98f97

- Výchozí opravy: `fix/question-context-integrity`, `ae727b3`.
- Z GitHubu načtený `main`: `91bf94b167b5d96f5e96616b613d757173208f5f`.
- Společný základ: `1eaf23cda3eb017cdd4a09038bd2e3694549a214`.
- Původní pracovní větev: `review/deployment-readiness`; nyní `fix/cross-mode-publication`.
  Produkční ref nebyl změněn. `main.py` zůstává bajtově stejný jako v `0a98f97`.

Produkce má proti společnému základu jedinou změnu v `main.py`: MC prompt nově
zohledňuje oznámené/věrohodně hlášené plány, naplánovaná nasazení a očekávané
pohyby; nemá se ukotvit na současném stavu, pokud důkazy podporují budoucí změnu.
Celý nový odstavec včetně navazujícího pravidla přesného časového okna byl převzat
**verbatim**. SHA-256 převzatého zdrojového bloku:
`5cc10ad40f83f623042f6ea8961670ddeeb97fca4aa36c4117988ffd9a0fc247`.
Nové prognostické instrukce tohoto auditu nepřibyly. Předchozí opravy kontextu
a časových údajů zůstávají vedle nového produkčního odstavce.

AST porovnání potvrdilo totožnou CLI konfiguraci modelů/parametrů mezi produkcí,
`ae727b3` a sjednoceným souborem: GPT-4o, temperature 0.3, timeout 40,
allowed_tries 2; parser/summarizer GPT-4o-mini, Perplexity Agent, ensemble 2×3,
summary-to-forecast False, publishing/skip defaults a původní turnaje/režimy.
Ostatní prognostické změny už byly ve společném základu a zůstávají zachovány.
Žádný nevyřešený konflikt prognostické logiky nebyl nalezen.

Při původním auditu byly publikační, runtime, research, validation/context moduly,
závislosti a všech pět workflow totožné s `ae727b3`. Navazující oprava mění pouze
publikační adaptér/žurnál a jejich offline testy; zbytek zůstává zachován.
Původní sjednocení proběhlo převzetím obsahu;
nebyl proveden Git merge, push, vzdálený PR ani deployment. Síť sloužila pouze
k read-only Git fetch/ls-remote na GitHubu; žádné API bota nebylo kontaktováno.

## Aktuální ověření a původní nález

Po sjednocení nejprve prošlo všech původních 242 regresí. Další integrační případ
odhalil chybu, která už existovala v publikačním adaptéru, nikoli konflikt promptů.
V `0a98f97` tedy prošlo 242 z 243 testů. Nynější výsledky po opravě:

| Ověření výsledného stromu | Výsledek |
| --- | --- |
| Celá offline sada, strict režim | 273 testů: všechny prošly |
| Celá offline sada, simulované GitHub Actions/run attempt 2, strict režim | 273 testů: všechny prošly |
| Expected failures / errors / skips / neočekávané živé pokusy | 0 / 0 / 0 / 0 |
| Instalované závislosti, `pip check` | Bez rozbitých požadavků |
| SDK, cryptography, requests, NumPy, OpenAI, PyYAML | Nainstalované verze odpovídají lockfilu |
| Syntaxe Python/YAML/Bash | Python 3.11 grammar a všech 15 workflow run kroků prošlo; skripty nebyly spuštěny |

**PUB-MODE-1 — původně vysoká závažnost, nyní opraveno.**
Původní umístění v `0a98f97`: `publication.py:194–196, 204–210, 261–275`, reprodukce v
`tests/test_github_publication.py`,
`RemotePipelineTests.test_tournament_only_recovers_comment_from_previous_cup_intent`.

1. Cup/test běží se `skip=False` a intentem podle svého run ID. Předpověď je
   confirmed, komentář je explicitně rejected (simulace 403).
2. Nezávislý tournament runner dostane `already_forecasted=True`, `skip=True`.
3. Adaptér kvůli obnově přepne příznak na False. Použije ovšem nový klíč `initial`,
   opraví komentář starého intentu a pokračuje vytvořením nové prognózy.
4. Simulace zaznamená **2 předpovědi a 2 komentáře místo 1+1**, navíc 18 modelových
   volání. Test nepoužívá skutečný GitHub ani Metaculus transport.

Společný lock ani CAS samy chybu neřešily: šlo o dva odlišné intenty. Oprava
trvale přiřazuje běh k původnímu plánu ještě před obnovou, včetně CAS retry.
Nové testy pokrývají opačný přechod, test_questions, reruny, přerušení, zastaralou
history, ready forecast i všechny stávající typy reportů. Žádný test není xfail.

## Co ochrany skutečně zajišťují

| Oblast | Ověřený stav / hranice |
| --- | --- |
| Všechny writery | Tournament, Cup, Test Bot a inicializátor mají stejnou repo-wide concurrency skupinu, `cancel-in-progress: false`, scope/path/branch. Review má vlastní skupinu a `contents: read`. |
| Trvalý stav | AES-GCM + HKDF z existujícího stabilního Metaculus tokenu; HMAC názvy; AAD váže repo/ref/path/scope/resource. Podmíněný GitHub Contents PUT potvrzuje přesný blob i commit. |
| Výpadek runneru | Potvrzený plán a sending přežijí VM. Ztracená ACK, timeout či přerušení POSTu nezpůsobí automatický resend. Confirmed forecast se v témže plánu neopakuje při opravě komentáře. |
| Přechod režimů/rerun | Vazba na původní plán se potvrdí před obnovou a přežije dokončení komentáře i CAS konflikt. Cup/test rerun bez doloženého intentu či pending plánu se zastaví před modely; nová samostatná run ID zachovávají záměrné přeforecastování. |
| Poškození/chybění stavu | Registrovaná chybějící část, změna klíče, chybný index, 403/rate limit nebo size limit blokují zápis. V Actions není fallback na lokální DB ani automatická reinicializace. |
| Limity a zrušení | Bot 180 min, otázka 60 min, conditional 120 min; model 240 s, blocking 300 s, Agent 210 s. Všechny náročné role sdílejí limit 1. Supervisor po TERM poskytne 60 s a pak zabije procesovou skupinu; publikační job má 210 min. |
| Inicializace | Ruční workflow, offline gate před Secrets, potvrzení `all-prior-runs-reconciled`; nová orphan větev. Existující/nečitelný ref se nikdy nepřepisuje, konflikt/lost ACK vyžaduje kontrolu. |

`metaculus-publication-state` při předchozím read-only ověření vzdálených refs
**neexistovala**; její aktuální existenci je nutné znovu ověřit před zavedením.
Nebyla zde vytvořena. Během auditu nebyla ověřena skutečná Actions oprávnění/rulesets,
Secrets, stav běžících/čekajících jobů, billing poskytovatelů ani obsah historie
na Metaculus. Původní audit běžel na Pythonu 3.12; navazující příprava nyní
ověřila celou sadu i na skutečném Pythonu 3.11.17, viz runbook výše.
Existující inicializační workflow nejprve spustí celou offline sadu na 3.11,
tedy tento budoucí test předchází i inicializačnímu zápisu.

Reprodukce výsledků bez klíčů:

```bash
.venv/bin/python -B -m tests.run --strict-known-bugs --report /tmp/metac-cross-mode-strict.json
GITHUB_ACTIONS=true GITHUB_REPOSITORY=offline-owner/offline-repo \
  GITHUB_RUN_ID=4242 GITHUB_RUN_ATTEMPT=2 METACULUS_PUBLICATION_BACKEND=github \
  .venv/bin/python -B -m tests.run --strict-known-bugs --report /tmp/metac-cross-mode-actions.json
```

Oba příkazy nyní vracejí exit 0 se všemi 273 testy úspěšnými. Všechny
transporty a modely jsou simulované, síťové zábrany zůstávají aktivní.

## Přechod ze starých workflows

Nové concurrency chrání pouze běhy s novým kódem. Staré workflow SHA a jiné
repozitáře/skripty se stejným účtem nový lock ani žurnál neznají. Terminal/cancelled
status jobu není důkaz, že server nepřijal právě probíhající POST.

Prázdný index **neimportuje** historické předpovědi ani původní lokální žurnály.
Potvrzovací volba inicializátoru je tvrzení operátora, nikoli automatická kontrola
serverové historie. Bez vyjasnění starých zápisů nelze zaručit bezduplicitní cutover.
Historické úspěšné prognózy tournament obvykle vynechá podle API history, ale
chybějící/zastaralá history není receipt konkrétního požadavku. Cup/test záměrně
přeforecastovávají při novém run ID a nejsou bezpečný offline smoke test.

Žurnál omezuje duplicity za cenu dostupnosti: sending/uncertain může blokovat
i předpověď, která se na server nikdy nedostala. Report a plán zůstanou uloženy;
neexistuje garance automatického doručení před uzávěrkou. Batch timeout může
odložit další otázky, u přeforecastovávajícího Cup mohou opakované dlouhé dávky
některé otázky dlouhodobě odsouvat. To není důvod prodlužovat deadline nebo
resetovat identitu bez provozních měření.

## Plán prvního nasazení — před schváleným spuštěním

1. **Oprava a offline gate hotovy:** PUB-MODE-1 i všech 273 regresí prochází.
   Před zavedením znovu načíst `main` a ověřit případné další ruční úpravy;
   nynější oprava podle zadání živá volání neprováděla. Offline gate nadále
   ponechat před Secrets a prvním zápisem, také na skutečném Pythonu 3.11.
2. **Pauza a inventura.** Ručně zakázat všechny tři publikační workflows ještě
   před změnou produkčního kódu; zastavit/ukončit všechny staré aktivní i queued
   běhy, další writery a zakázat reruny jejich SHA. Vyjasnit jejich částečné a
   nejisté zápisy podle autoritativních důkazů. Pokud výsledek nelze určit,
   publikování ponechat vypnuté. Nejde obejít novým prázdným žurnálem.
3. **Předpoklady.** Nový Secret není potřeba. Ručně ověřit existující stabilní
   `METACULUS_TOKEN` (šifrování vyžaduje alespoň 32 znaků), dostupnost již používaných
   model/research Secrets a kreditů, možnost `GITHUB_TOKEN` s `contents: write`
   vytvořit ref a přímo checkpointovat stavovou větev. Rulesets nesmějí vyžadovat
   PR/status check pro každý stavový commit. Současně zabránit mazání/force-pushi
   stavu a omezit jeho správu; main má zůstat chráněný. Token se nyní nerotuje.
4. **Zavedení kódu a inicializace.** Teprve schválenou zelenou verzi zavést jako
   jeden celek při vypnutých publisherech. Pokud stavová větev dosud nikdy
   neexistovala, spustit pouze `Initialize publication state` z této verze
   s potvrzením vyjasnění starých běhů. Existující v1 žurnál se migruje na v2,
   nereinicializuje se; zmizelý historický žurnál vyžaduje vyjasnění/obnovu.
   Offline gate na Pythonu
   3.11 musí projít. Ověřit úspěch ref/indexu a použitou source SHA; při chybě
   nezkoušet reset/reinicializaci existujícího refu. Inicializátor nepublikuje.
5. **Jeden schválený ruční tournament běh.** Povolit jen tournament, zkontrolovat
   vybranou větev/source SHA a spustit právě jednou; Cup a Test Bot zatím vypnuté.
   Jde již o skutečný produkční běh nad oběma původními turnaji, ne o canary jediné
   otázky. Ověřit počty forecast/comment proti žurnálu a Metaculus, confirmed
   checkpointy, délky/retry, případné rejected/uncertain a absenci souběžných
   starých writerů. Nepoužívat další run ani test_questions jako slepý retry.
6. **Rozšíření.** Po vyhodnocení znovu povolit schválené ostatní writery. Cup má
   zachovaný cron `0 0 */2 * *` v UTC (v listopadu 01:00 Praha); není to přesný
   nepřetržitý 48hodinový interval. Review je samostatné, závisí na jeho existující
   repository variable. Stávající triggery se při auditu nemění.

## Zastavení a rollback

Při nejasném zápisu, opakovaných chybách nebo poškození stavu vypnout writery,
ukončit běhy a vyjasnit rozpracované operace. Uchovat **nejnovější** šifrovaný
index/části, jejich historii a přístup k odpovídajícímu starému šifrovacímu klíči;
klíč neukládat do repozitáře ani logů. Stav nikdy nevracet spolu se zdrojovým kódem.

Preferovat opravu vpřed nebo návrat na dříve ověřenou verzi, která stále rozumí
stejnému žurnálu, identitám a CAS. Před obnovením provést offline testy a kontrolu
pending operací. Nová podmínka je podpora **SQLite v2 vazeb**: `0a98f97` ani starší
žurnálový kód nelze použít k aktivnímu rollbacku; při otevření v2 selže. Schéma
nesnižovat a vazby neodstraňovat. `91bf94b` je referenční původní kód, **není bezpečný
aktivní rollback target po prvním novém zápisu**: nečte žurnál. Pokud se musí vrátit tento
kód, publikování zůstává vypnuté. Nikdy nespouštět starý workflow rerun jako rollback.

Bez serverové idempotence či jednoznačného receipt Metaculus nelze současně
garantovat žádné duplicity a vždy dokončené doručení. Reconcile vyžaduje ověřený
výsledek, zastavené původní workery a přesný operation hash; shoda hodnoty/textu
nebo prázdná history není dostatečný důkaz pro automatické odblokování.
