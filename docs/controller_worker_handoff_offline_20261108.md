# Controller → worker: minimální čtecí kontrola předání

Výchozí commit: `9a63d1a749972c497e36a24b80f1307308815fb7` po PR #32.
Samostatný detached worktree: `/workspace/vrchcab-autonomous-worker-9a63d1a`.
Původní pracovní kopie a její čtyři lokální soubory zůstaly beze změny.
Pro získání commitu byl proveden právě jeden povolený
`git fetch --no-tags origin main`. Další síťová volání neproběhla.

Opravy PR #32 už jsou v základu. Neimplementují se znovu a nepřebírají se
staré návrhy PR #2/#7. Historickou revizi zachovává
[původní dokument](vrchcab_automatic_operation_review_20261108.md).

## Co tento krok přidává

Do existujícího `autonomous_orchestrator.py` přibyla pouze funkce
`evaluate_offline_worker_handoff`. Nevzniká nový obecný orchestrátor,
transport, workflow ani CLI. `main.py`, modely, prompty, počet reportů,
limity, publikační a control schema i aktivní automatizace se nemění.

Dosavadní `evaluate_offline_tick` rozhoduje o NOVÉ rezervaci a záměrně
odmítá existující pending intent. Nelze jej proto použít jako vstupní
kontrolu workeru, který má převzít PRÁVĚ rezervovaný intent. Nová funkce
pokrývá tento odlišný krok. Není náhradou za první preflight.

Postup v paměťových testech:

1. Existující `reserve_only_after_preflight` zapíše jeden syntetický CAS.
2. Nová čtecí kontrola musí dostat tentýž intent a approval epoch. Vyžaduje
   oddělené `activation_confirmed=True`; bez něj nekontaktuje ani čtečky.
3. Controller musí být stále enabled. Pending intent musí existovat a být
   nejvýše pět minut starý; budoucí čas či prošlé okno se odmítají, pending
   se nemaže. Pět minut používá existující `STATUS_MAX_AGE`.
4. Argumenty výzkumného plánu slouží pouze k předběžnému odmítnutí
   neplatného nebo příliš nákladného plánu. Nejsou důkazem skutečné
   konfigurace workeru a samy nesmějí založit kladné rozhodnutí.
5. Znovu se používá `build_dispatch_snapshot`: čerstvé nezávislé čtečky,
   nulový počet running/queued publisherů, jistý předchozí publikační
   výsledek, darované kredity, Perplexity, obě aktivace a první pokus.
   Důkazy navíc nesmějí předcházet právě rezervovanému intentu.
6. Úplná denní evidence musí souhlasit s uloženým dnem a počtem pokusů.
   Poslední již rezervovaný slot je přípustný (`attempts == max`);
   nevytváří se nový pokus ani CAS. Pro peníze se používá stávající přesný
   parser controlleru. Platí `held >= reserve` a
   `max(held, charged + reserve) <= cap`: nové známé náklady nesmějí
   spotřebovat prostor pro plánovanou práci. Tato plánovací rezerva stále
   není provider-side tvrdý limit jedné účtované operace.
7. Po čtení zdrojů se znovu přečte controller. Změna stavu nebo revize,
   včetně vypnutí kill switche, předání odmítne.
8. I při splnění všech těchto podmínek současná architektura postrádá
   důvěryhodné svázání výzkumného plánu se skutečným workerem. Kontrola
   proto vrací odmítnutí `worker_research_plan_not_bound_to_configuration`.

`now` může být pevný čas pro offline scénáře nebo injektované hodiny pro
budoucí skutečné čtečky. Čas se kontroluje před i po čtení zdrojů: nové
časové značky po dokončení čtení nejsou nesprávně považované za budoucí.
Posun hodin zpět nebo vypršení předávacího okna během čtení blokují výsledek.

Funkce nyní nemá žádnou cestu ke kladnému výsledku. Hodnota `1` od
volajícího ani jiné jeho tvrzení nemůže nahradit chybějící vazbu na
konfiguraci. Nevznikl nový příznak „ověřeno“, který by mohl volající sám
nastavit. `durably_reserved` je false, protože tato funkce nic nezapisuje.
Pending a kvóta zůstávají stejné. Výjimka při čtení rovněž nepovoluje
žádný fallback nebo spuštění.

## Proč se zde ještě nespouští skutečný worker

Rezervovaný intent sám není trvalým jednorázovým převzetím workerem.
Opakované kontroly bez svázané konfigurace vždy odmítnou předání. Budoucí
kladné rozhodnutí by navíc nesmělo samo opravňovat několik placených běhů:
mezi posledním čtením a případnou operací existuje závod. Proto zde není
callback spouštějící forecastování ani propojení na živý `forecast_for_mode`.

Toto je minimální bezpečná část propojení, nikoliv hotový automatický
publisher. Před skutečným spuštěním nadále chybí:

- atomické a trvalé jednorázové převzetí intentu konkrétním worker run ID,
  vazba na schválené source SHA a první pokus, bez retry při nejasné ACK;
- důvěryhodně ověřená aktivace a zdroje: datové příznaky samy nejsou
  autentizace ani samostatné schválení placeného provozu;
- důvěryhodná vazba skutečné konfigurace workeru na posuzovaný výzkumný
  plán; počet reportů z volání funkce ani pouze AST kontrola v testu nestačí;
- další kontrola zastavení a výdajů před každou placenou operací a POST;
- přesná identifikace vlastního aktivního workeru při budoucí kontrole
  fronty. Nynější kontrola je PŘED startem a vyžaduje frontu prázdnou;
  dnešní čtečka počítá i budoucí worker. Žádná obecná výjimka pro aktivní
  run ani starý queued run #34463156990 zde nevzniká;
- bezpečné vyjasnění výsledků, zvlášť prázdného výběru a nejasné publikace,
  a kompletní nezávislá evidence nákladů.

Současné autentizované čtečky nenastavují chybějící Perplexity/historické
důkazy či provozní schválení na true. Reálný vstup proto zůstává blokovaný.
Stav starého queued běhu nebyl online kontrolován, měněn ani ignorován.

## Nesoulad dvou a čtyř Agent požadavků

`main.py` stále konfiguruje dva research reporty a tři prognózy na report.
Každé `run_research` má fázi initial a fact-check: dva reporty potřebují až
čtyři Agent POST pokusy bez retry. `MAX_AUTO_AGENT_POST_ATTEMPTS` zůstává 2.

Při vstupní hodnotě `2` handoff plán odmítá důvodem
`worker_research_plan_exceeds_automatic_attempt_cap` před čtením providerů.
Nezávislá revize ale správně odhalila, že původní hodnota `1` od volajícího
mohla projít všemi kontrolami, přestože skutečný worker používá dva reporty.
AST kontrola produkčního konstruktoru v testu sama tuto runtime vazbu nevynucuje.

Minimální oprava zachovává všechny předchozí kontroly a ruší závěrečný
kladný výsledek: dosud chybí důvěryhodný zdroj konfigurace skutečného workeru,
takže i jinak konzistentní jednoreportová fixture končí důvodem
`worker_research_plan_not_bound_to_configuration`. Nová regrese ověří
skutečné dva reporty a obě Agent fáze z `main.py`, podstrčí `1` a vyžaduje
odmítnutí bez změny control stavu. Neprovádí se import ani spuštění bota.

Produkční ensemble se nesnižuje, research se nevypíná a automatický limit
ani dolarová expozice se nezvyšují. Supervised čtyřpokusový režim zůstává
oddělený a beze změny. Žádný placený požadavek se neodesílá.

## Ověření

Na dostupném Pythonu 3.12.14, bez instalací:

- Nová regrese před opravou selhala na `AssertionError: True is not false`;
  ostatních 30 testů prošlo. Původní slabina je tak reprodukovaná.
- `python -B tests/test_autonomous_runtime_offline.py` po opravě:
  **31/31 prošlo**, z toho 15 handoff testů; 0 failures/errors/skips
  a síťových pokusů. Dřívější hypotetické průchody nyní vyžadují konkrétní
  odmítnutí kvůli nesvázané konfiguraci; ostatní kontroly se neobcházejí.
- Existující `tools/test_*.py` pod stejným auditním zákazem sítě:
  **19/19 prošlo**, 0 síťových pokusů.
- Celkem **50 úspěšných testů**. Pouze paměťové odpovědi/CAS a výslovně
  syntetické dočasné SQLite databáze; žádný skutečný žurnál se neotevírá.
- Gramatika Pythonu 3.11 pro oba změněné Python soubory prošla; nejde
  o spuštění testů interpreterem 3.11. `git diff --check` prošel.
- `python -B -m tests.run --strict-known-bugs`: exit 1 ještě před discovery,
  `ModuleNotFoundError: requests`. `forecasting-tools` a OpenAI zde také
  chybí. Tato kompletní sada nespustila žádný test. Uživatelem potvrzených
  511 testů PR #32 je historický výsledek základu, nikoliv ověření této změny.

Nové metody rozšiřují existující `AutonomousRuntimeOfflineTests`, takže
patří i do standardního `tests.run` discovery. Celý suite je potřeba později
ověřit v již vybaveném prostředí nebo nezávislém offline CI; nic se zde
nedispatchuje.

## Jeden další krok

V samostatném kroku navrhnout ověřitelnou vazbu mezi skutečnou konfigurací
workeru a kontrolou předání. Nemůže jít jen o nový příznak či datový objekt
vyplněný volajícím. Do té doby musí předání zůstat odmítnuté. Jednorázové
převzetí workerem, finanční nesoulad, neověřené náklady a starý queued běh
zůstávají dalšími samostatnými blokacemi; tato oprava je neřeší.
