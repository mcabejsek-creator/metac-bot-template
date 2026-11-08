# Společná konfigurace workeru a bezpečné odmítnutí neověřené identity

Základ: `05b7ba631daaedd0d36424302dbeee117a7e4581` po PR #33.
Nový detached worktree: `/workspace/vrchcab-worker-config-05b7ba6`.
Byl nutný právě jeden povolený `git fetch --no-tags origin main`.
Starší pracovní kopie se nemění. Další síťové požadavky, instalace,
GitHub zápisy, placené modely ani skutečné forecastování neproběhly.

## Jediný zdroj konfigurace v této verzi kódu

`worker_configuration.WORKER_CONFIG` je neměnná datová konfigurace:

| Nastavení | Zachovaná hodnota |
| --- | --- |
| Research reporty na otázku | 2 |
| Prognózy na report | 3 |
| Researcher | `perplexity-agent` |
| Agent fáze každého reportu | `initial`, `fact-check` |
| Agent pokusy na jednu atomickou otázku bez retry | **4** |
| Automatický limit, stále v `perplexity_run_budget.py` | **2** |

`main.py` používá tento objekt přímo pro konstruktor bota, výběr researcheru
a názvy obou skutečných Agent fází. Controller čte tentýž modul a z něj
odvozuje potřebný počet pokusů. Počet reportů ani násobek už neodvozuje
z tvrzení volajícího. Počet prognóz, modely, prompty, retry politiky,
publikování, rozpočtové limity a CLI zůstávají zachované.

Jde o vazbu na konfiguraci zdrojového kódu, nikoliv o důkaz původu
vykonávaného procesu. `frozen=True` brání náhodným změnám objektu; není to
bezpečnostní hranice proti škodlivému kódu ve stejném procesu.

## Rozhodnutí handoff

Dosavadní kontroly aktivace, intentu, approval epoch a času zůstávají.
Potom `evaluate_offline_worker_handoff`:

1. Načte společnou konfiguraci; chybějící objekt nebo nesprávný typ
   znamená `worker_configuration_unavailable`.
2. Volitelné staré argumenty researcher/počet reportů chápe pouze jako
   kontrolu konzistence. Podstrčená jednička proti skutečným dvěma
   reportům znamená `worker_configuration_mismatch`, nikoliv nový plán.
   Jejich vynechání nic nepovoluje: plán se stále načte ze společného modulu.
3. Skutečné **čtyři** požadavky odmítne před čtením providerů důvodem
   `worker_research_plan_exceeds_automatic_attempt_cap`.
4. Pokud by budoucí zdrojová konfigurace měla plán v limitu, zůstávají
   všechny existující kontroly fronty, publikace, kreditů, denní kvóty,
   nákladů a opakovaného čtení control stavu. Navíc se porovná konfigurace
   před a po preflightu; změna znamená
   `worker_configuration_changed_during_preflight`.
5. Ani jinak konzistentní plán v limitu nesmí přejít do kladného výsledku.
   Chybí důkaz identity skutečně vykonávaného workeru, takže výsledek je
   `worker_execution_identity_unverified`.

Funkce nepřijímá callerem vyplněný konfigurační objekt, schválené SHA,
režim ani potvrzovací příznak identity. Proměnná `GITHUB_SHA` ani lokální
HEAD nejsou náhradou takového důkazu. Nebyla přidána cesta ke kladnému
rozhodnutí, CAS claim, transport, start workeru nebo workflow.

## Proč zatím nelze ověřit skutečně vykonávaný worker

Současné řízení má intent, approval epoch a kvótu, ale nemá důvěryhodně
ověřenou vazbu **schválený neměnný zdrojový artefakt → konkrétní běh →
skutečně použitá konfigurace**. Neexistuje autentizovaný doklad, že
budoucí worker spustí právě posouzený kód a režim. Tvrzení o SHA v požadavku
či prostředí může být stejné i při jiném kódu, již vytvořené instanci bota
nebo odlišné konfiguraci. Samotný import společného modulu to neřeší.

Z tohoto důvodu by nové „ověřeno“ boolean pole nebo pouhé srovnání callerem
dodaného SHA vytvořilo falešnou autoritu. Tato změna je nezavádí. Důvěryhodná
runtime vazba vyžaduje samostatný návrh na úrovni způsobu spouštění a
ověřování artefaktu; do té doby zůstává handoff odmítnutý.

Přetrvává také závod mezi poslední kontrolou a případným budoucím spuštěním.
Jednorázové převzetí intentu, kontrola zastavení před každou placenou
operací, autoritativní billing a vyjasnění výsledků jsou další samostatné
podmínky. Neřeší se změnou konfigurace ani ignorováním queued běhu
#34463156990. Fronta, žurnál a aktivní automatizace zůstávají nedotčené.

## Ověření zachovaného chování a bezpečných selhání

- Skutečný produkční plán 2 × 2 je odmítnut kvůli limitu 2, i bez callerem
  dodaného počtu reportů. Podstrčená hodnota 1 se odmítá jako nesoulad.
- AST test kontroluje, že jediný produkční konstruktor a obě Agent fáze
  opravdu odkazují na společnou konfiguraci.
- Lokální porovnání celého AST `main.py` s výchozím commitem po dosazení
  přesunutých hodnot je totožné. Tím jsou na úrovni zdroje zachované
  prompty, modely, nastavení ensemble i ostatní logika. Není to náhrada
  kompletního SDK regresního běhu.
- Testy pokrývají neplatný/chybějící konfigurační objekt, neměnnost hodnot,
  změnu plánu na šest požadavků, zastaralé tvrzení volajícího a změnu
  konfigurace během čtení. I hypotetický plán v limitu zůstane odmítnutý,
  včetně syntetických SHA/režimových tvrzení.
- Pro izolované testy dřívějších finančních a control kontrol se pouze
  v testovacím procesu dočasně nahrazuje společný modul hypotetickým
  jednoreportovým plánem. Tato fixture nepotvrzuje runtime identitu,
  nevolá worker a nikdy nezpůsobí kladný handoff. Produkční regrese používají
  původní skutečnou konfiguraci 2 × 3.

Příkazy a výsledky na dostupném Pythonu 3.12.14:

```bash
python -B tests/test_autonomous_runtime_offline.py
```

**37/37 prošlo**, 0 failures/errors/skips, 0 síťových pokusů. Dalších
**19/19** existujících `tools/test_*.py` pod auditním zákazem sítě také
prošlo. Celkem **56 úspěšných offline testů**.

```bash
python -B -m tests.run --strict-known-bugs
git diff --check
```

Kompletní runner skončil s exit 1 před discovery:
`ModuleNotFoundError: requests`. Zde také chybí `forecasting-tools` a OpenAI;
nic se nedoinstalovávalo. Kompletní SDK sada tedy nespustila žádný test.
`git diff --check` a gramatika Pythonu 3.11 pro dotčené Python soubory prošly.

## Doporučený další krok

Navrhnout nezávisle ověřitelný doklad vazby schváleného neměnného artefaktu
na konkrétní worker a jeho konfiguraci, nejprve offline. Bez této vazby
ponechat odmítnutí identity. Před přijetím samotného přesunu konfigurace je
navíc nutné plné offline SDK ověření v již vybaveném prostředí nebo CI.
