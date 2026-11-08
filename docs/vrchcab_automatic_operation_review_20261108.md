# Vrchcab: revize automatického provozu a vyhodnocování

Lokální revize nad `8b5489c4ed7b9a2d4197ebac74a0c1b9b9fed2ae`, 8. 11. 2026.
Pracovní kopie byla na začátku čistá. Tento dokument rozlišuje současný kód,
syntetické ověření a návrh budoucího nasazení. Nepřebírá návrhy ze starých PR #2
a #7. Historické počty testů v README nejsou výsledkem této revize.

**Závěr: základ je použitelný pro další vývoj, unattended publikování zatím NO-GO.**
Nevznikl nový publisher, časovač, produkční adaptér ani automatické oprávnění.
Nebyly provedeny síťové požadavky, modelová volání, GitHub Actions, instalace,
změny secrets, skutečného publikačního žurnálu nebo aktivních automatizací.
Všechny změny zůstávají v této izolované pracovní kopii.

## Co už je implementované

| Oblast | Zjištění z aktuální implementace |
| --- | --- |
| Inventura a výběr | `bounded_open_question_selection.py` vyžaduje úplné omezené skeny obou turnajů, kontroluje vlastní historii a odmítá neúplné/rozporné stránky. `automatic_one_selection.py` vybírá nejvýše jednu podporovanou atomickou otázku, přednostně nejbližší uzávěrku s alespoň 90 minutami času. |
| Prázdná inventura | `forecast_for_mode` vrací prázdný výsledek před kontrolou kreditů, inicializací žurnálu a prognózováním. Nula otázek je legitimní výsledek, nikoliv důvod k retry placené práce. |
| Publikování | `publication.py`, `publication_journal.py` a `github_publication.py`: zmrazený plán, claim před odesláním, šifrovaný trvalý GitHub stav, CAS, obnova původního intentu mezi režimy, oddělený forecast a soukromý komentář. `sending/uncertain` se automaticky znovu neposílá. |
| Rozpočty a lhůty | OpenRouter kontroluje identitu darovaného klíče a rezervu $5. Perplexity počítá pokusy před odesláním, zastavuje po neznámém výsledku a eviduje vrácené ceny. Runtime má lhůty operací/otázky/běhu a procesový supervisor. Rezerva OpenRouter ani kontrola ceny po odpovědi nejsou tvrdý dolarový limit jednotlivého požadavku. |
| Řízení | `scheduler_policy.py`, `dispatch_intent_controller.py`, `autonomous_orchestrator.py`: default OFF, 15minutové rozestupy, čerstvost důkazů, nulová fronta publisherů, denní kvóta, CAS rezervace intentu a blokace pending výsledku. Orchestrátor nic nedispatchuje. |
| Dostupné adaptéry | Existují `GitHubContentsControlStore` a čtecí adaptéry GitHub fronty, darovaných kreditů a šifrovaného žurnálu. Nejsou zapojené do živého automatického workeru. Chybějící důkazy o Perplexity, historii a aktivaci nenahrazují příznakem `True`. |
| Monitor | Oddělený přibližně 15minutový read-only monitor zůstává beze změny. Veřejný workflow watch sleduje pouze první stránky; sám nepotvrzuje úplnost inventury ani vlastní historii prognóz. |
| Review | `review_bot.yaml` už volá `bot-review review`, vytváří `review.json` a `review.md`, má `contents: read`, vlastní concurrency a 90denní artefakty. Týdenní běh je podmíněný `REVIEW_BOT_ENABLED`; aktuální vzdálená hodnota nebyla zjišťována. |

Uživatelem dodaný výsledek inventurního preflightu #42522722302 (5 GET,
0 validovaných otevřených otázek) je úspěšný bezpečný prázdný snímek. Během
této revize nebyl opakován a nedokazuje, že později žádná otázka nepřibude.
Stejně tak dodaný queued stav #34463156990 nebyl nově online kontrolován;
zůstává blokací a nebyl z žádné kontroly vyloučen.

## Dvě lokální opravy

1. **Trvalá denní rezerva.** Kontrola přijímala další pokus podle
   `max(uložená rezerva, ověřená útrata) + nová rezerva`, ale ukládala jen
   `uložená rezerva + nová rezerva`. Příklad: uložených $0.10, ověřených $0.60,
   nový pokus $0.40, denní limit $1.00 → ukládalo se $0.50. Pozdější nižší
   snímek tak mohl znovu uvolnit prostor. Nyní se ukládá stejná konzervativní
   hodnota použitá při přijetí, tedy $1.00. Automatické refundy či reset nepřibyly.
2. **Časová vazba výsledku na intent.** `assess_settlement` odmítá dokončení
   workflow před `pending.reserved_at`. Zachovává přípustnou shodu na přesnost
   jedné sekundy. Stále pouze vyhodnocuje způsobilost pro samostatně schválené
   vyjasnění; pending nemaže a žádný žurnál neaktualizuje.

Obě opravy se týkají dosud neaktivního řízení. Nemění prognostické prompty,
modely, ensemble, rozpočtová schválení, workflow ani formát uloženého stavu.

## Skutečné mezery před soutěžním automatickým provozem

| Priorita | Chybějící část / překážka | Minimální potřebný výsledek |
| --- | --- | --- |
| Blokace | Starý publisher #34463156990 je podle zadání queued. | Doložené vyřešení přes existující GitHub Support #4966035; případné nejasné historické zápisy zvlášť vyjasnit. Žádná výjimka podle stáří běhu. |
| Blokace | Perplexity nemá v současném kódu důvěryhodné průběžné ověření wallet/úplného vyúčtování. Šestihodinové ruční potvrzení ani response cost nestačí na trvalý provoz. | Vymahatelná politika výdajů nebo výslovně schválená omezená expozice, důvěryhodné vyúčtování všech i nejistých pokusů, denní limit a kvóta bez automatického resetu při nejasnosti. Zachovat kvalitní research. |
| Chyba kontraktu | Automatický guard dovoluje 2 Agent pokusy. CLI konfigurace má 2 research reporty a každý používá initial + fact-check, tedy potřebuje 4 bez retry. Dosavadní test propojení selectoru nahrazuje budget i forecastování. | Ověřit skutečný 2×3 pipeline se simulovanými modely a skutečným guardem. Zachování ensemble vyžaduje konkrétně schválený čtyřpokusový kontrakt; nyní nebyl limit zvýšen ani počet reportů snížen. |
| Blokace | Zmrazení staré epochy žurnálu nedokazuje kompletní historické výsledky; dostupná diagnostika jednoho postu není úplný audit. | Nezávislé vyjasnění relevantních forecastů i komentářů; nejasná ACK ponechává publikování zablokované. Žurnál se neresetuje. |
| Implementace | Chybí automatický worker, dispatch transport a jejich bezpečné zapojení do existujícího controlleru. | Jeden oddělený default-OFF worker/controller, oba skeny znovu před výběrem, sdílený publikační lock/žurnál, `run_attempt == 1`, kontrola aktivace těsně před každým placeným požadavkem a publikačním POST. Žádná nová obecná orchestrační vrstva. |
| Implementace | Důkazy řízení jsou částečně pouze tvrzení v datových objektech; rezervace intentu není potvrzený dispatch. | Ověřit autentický zdroj, repo, workflow, schválené source SHA, intent, skutečné run ID a run attempt. CAS před jediným dispatch pokusem; při nejasné ACK neodesílat znovu a nevymazat pending. Dokončení po rezervaci je nutná, nikoliv postačující podmínka. |
| Implementace | Zatím není bezpečné dokončení cyklu bez kandidáta / po zrušení / při nejasném výsledku. | Rozlišit `no_candidate`, `published`, `failed_before_spend`, `uncertain`, `rejected`; automatické uvolnění pouze po autoritativním prokázání bezpečného výsledku. Dnes se pending oprávněně automaticky neuvolňuje. |
| Implementace | Chybí trvalé provozní a výdajové vyhodnocení celého automatického cyklu. | Oddělené soukromé záznamy výsledku běhu, nákladů, latence a blokací; zachovat neoceněné pokusy. Rozpočet se neobnovuje z pouhého zeleného workflow ani o půlnoci. |
| Ověření | Kompletní SDK regresní sada zde není spustitelná bez instalace. | Před nasazením spustit existující strict suite v již vybaveném prostředí, včetně reálného SDK a simulovaných Actions. Tento krok ji nenahrazuje. |

Single-job provoz s 60minutovým limitem otázky a 90minutovým vstupním oknem
nemůže zaručit pokrytí všech krátce otevřených souběžných otázek. Nezvyšovat
souběžnost ani objem placené práce bez měření a odpovídajícího schválení.

## Automatické vyhodnocování: využít existující review

Zachovat oddělený čtecí `review_bot.yaml`. V tomto kroku se nespouští ani
nemění. Nejprve je nutné offline ověřit výstupní schéma a význam metrik
`metaculus-bot-review==0.1.1` z lockfilu; balíček zde není nainstalovaný a jeho
výpočty nebyly nezávisle ověřeny. Není důvod stavět druhý sběrač stejných dat.

Budoucí rozšíření jeho výstupu má následující kontrakt:

1. **Kohorta a vazba.** FutureEval 33121 a MiniBench vykazovat zvlášť, stejně
   tak typy otázek a verze bota. Vyhodnocovat pouze skutečně potvrzené prognózy
   s doloženým vyřešením. V soukromém záznamu spojit post/question ID,
   předpověď a čas z potvrzeného zmrazeného plánu, source SHA, model/prompt
   verzi, intent/run ID a autoritativní resolution. Časové údaje ukládat UTC.
   Oddělit provozní výsledek publikace od pozdější kvality prognózy.
2. **Žádná časová kontaminace.** Hlavní srovnání použije první potvrzenou
   prognózu před uzávěrkou. Poslední včasná prognóza může být vedlejší metrika.
   Nevybírat zpětně nejúspěšnější pokus. Oficiální časově vážené skóre převzít
   jen jako ověřenou hodnotu Metaculus, neodvozovat je z poslední pravděpodobnosti.
   Benchmark musí odpovídat stejnému času; konečný community forecast není
   férové srovnání s předpovědí vytvořenou dříve.
3. **Metriky podle typu.** Binary: Brier `(p-y)^2` a log loss, nižší je lepší;
   kalibrace a počty v intervalech pravděpodobností. Multiple choice: explicitně
   součet Brier přes všechny možnosti a log loss správné možnosti. Numeric/date:
   CRPS z platné uložené CDF a skutečného výsledku, pokrytí intervalů 50/80/95 %;
   dodržet původní jednotky, časovou osu, meze a transformaci. Neprůměrovat raw
   CRPS mezi dolary, daty a jinými jednotkami ani různé škály Brier. Oficiální
   soutěžní skóre evidovat zvlášť s názvem a zdrojem pravidla.
4. **Neznámé výsledky.** Open/unresolved, annulled/ambiguous, nekompatibilní
   resolution, chybějící forecast, neplatná distribuce a neúplná historie mají
   oddělené počty a důvody. Nejsou to nuly ani chybné předpovědi. Změněné
   resolution vytvoří novou verzi vyhodnocení a zachovají starou.
5. **Opakování a pokrytí.** Klíč výsledku: turnaj + question ID + hash potvrzené
   prognózy + verze resolution + verze scoreru. Překryv týdenních dotazů nesmí
   navyšovat N. Současných 30 dní a 90denní retence artefaktů nestačí na úplnou
   dlouhodobou historii: uchovat soukromý trvalý export a poslední úspěšný bod
   sběru, překryv a schválený omezený backfill po výpadku. Publikační žurnál
   kvůli skórování neměnit; souhrn na veřejné run page jen agregovaný.
6. **Provozní souhrn.** Počet nalezených/vybraných/publikovaných/vyřešených
   otázek, `no_candidate`, důvody odmítnutí, čekání ve frontě, čas do forecastu,
   confirmed forecast/comment zvlášť, nejasné výsledky a skutečné ověřené
   náklady vedle neoceněných pokusů. Bez úplného pozorování příležitostí nelze
   tvrdit procentuální pokrytí turnaje. Cena odpovědí není provider invoice.
7. **Zpětná vazba.** Týdenní report nejprve informuje operátora. Uvádí N a
   nejistotu, nepovažuje malé vzorky za důkaz zlepšení. Při více prognózách
   jedné otázky počítá nejistotu po otázkách. Nepřepisuje automaticky prompty,
   modely, rozpočty, kvóty ani aktivaci publisheru. Review nesmí opravit žurnál
   nebo odblokovat uncertain forecast jako vedlejší efekt.

Před rozšířením review ověřit synteticky: `p=0.8, y=1` → Brier `0.04`, log
loss přibližně `0.22314355`; perfektní a obrácená předpověď, MC permutace
možností, platná/nespojitá CDF a meze, chybějící a změněné resolution,
opakovaný překryv dotazů, forecast po uzávěrce a nedokončený sběr. Nulový počet
vyřešených otázek znamená `insufficient_data`, nikoliv 0% nebo 100% úspěšnost.
Tyto testy scoreru jsou návrh přijímacích podmínek, nebyly zde vydávány za hotové.

## Ověření v této pracovní kopii

| Kontrola | Výsledek |
| --- | --- |
| Nová stdlib integrační sada před opravou | 16 testů: 14 prošlo, 2 selhaly přesně na uvedených chybách. Žádný pokus o síť. |
| Stejná sada po opravě | **16/16 prošlo**, 0 failures/errors/skips, 0 síťových pokusů. |
| Existující `tools/test_*.py` | **19/19 prošlo** se stejným auditním zákazem síťových spojení; 14 testů watch + 5 statických testů izolace secrets. Pouze syntetické údaje. |
| Ochrana a lokální integrita | Samostatná úmyslná DNS sonda byla auditní ochranou zablokována před překladem jména. Syntaxe tří dotčených Python souborů odpovídá gramatice 3.11; nejde o běh testů na interpreteru 3.11. `git diff --check` prošel a workflow, hlavní bot, publikační moduly i dependency soubory jsou proti výchozímu commitu nezměněné. |
| Celá `python -B -m tests.run --strict-known-bugs` | **Blokováno před discovery**, exit 1: `ModuleNotFoundError: requests`. SDK `forecasting-tools`, OpenAI a review balíček zde rovněž nejsou instalované. Nula testů této kompletní sady bylo spuštěno; žádný nový report z ní nevznikl. Nic se nedoinstalovávalo. |

Nová sada `tests/test_autonomous_runtime_offline.py` je přímo spustitelná
bez SDK a zároveň se přidává do běžného `tests.run`. Používá skutečné čisté
moduly controlleru, budgetu, queue readeru a SQLite koordinátoru. GitHub
odpovědi jsou paměťové, CAS je paměťový, publikační operace přijímá pouze
lokální seznam a SQLite soubor je výslovně syntetický v dočasném adresáři.
Žádný skutečný publikační žurnál se neotevírá. Testy ověřují default OFF,
queued blokaci, zastaralé/chybějící zdroje, ztracenou CAS ACK, kill-switch
readback, časové přiřazení výsledku, cenu neznámého pokusu, restart a zákaz
duplicitní syntetické publikace. Neověřují SDK prognostický pipeline ani živé
oprávnění, účtování, distribuci kill switch nebo obsah serverové historie.

Reprodukce dostupných offline kontrol bez instalace:

```bash
cd /workspace/metac-bot-template
python -B tests/test_autonomous_runtime_offline.py
python -B - <<'PY'
import runpy
import sys
import unittest
scope = runpy.run_path('tests/test_autonomous_runtime_offline.py', run_name='offline_guard')
sys.addaudithook(scope['reject_network'])
suite = unittest.defaultTestLoader.discover('tools', pattern='test_*.py')
result = unittest.TextTestRunner(verbosity=2).run(suite)
attempts = scope['NETWORK_ATTEMPTS']
print(f'Offline audit guard: {len(attempts)} network attempts.')
raise SystemExit(0 if result.wasSuccessful() and not attempts else 1)
PY
```

## Jeden nejbližší krok k nasazení

**Vlastník má přes existující ticket GitHub Support #4966035 vyžádat doložené
vyřešení queued publisheru #34463156990.** Tento krok nezakládá nový workflow
ani nezveřejňuje prognózu. Po později autorizovaném čtecím ověření musí fronta
obsahovat nula aktivních i čekajících publisherů; nejasné historické publikace
stále vyžadují vlastní vyjasnění. Odstranění frontové blokace samo o sobě
nenahrazuje výdajové kontroly, dokončení workeru, úplné offline testy ani
výslovné schválení skutečného automatického provozu.
