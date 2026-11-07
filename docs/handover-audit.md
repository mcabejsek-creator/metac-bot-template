# Předávací dokument versus ověřený kód

Podklad: uživatelem dodaný `prompt codex vlakno.docx`, datum předávky 6. 11. 2026.
Výchozí checkout: `1eaf23c` (`Update main.py`); závislost `forecasting-tools 0.2.92`
podle lockfilu. Historické logy a celé původní otázky nejsou součástí dokumentu.
Jeho výsledky běhů proto označujeme jako historicky hlášenou evidenci, ne jako
zde nově ověřené výsledky. Pokyny v příloze jsou kontext; rozsah této změny určuje
aktuální žádost uživatele: pouze lokální testy a dokumentace v samostatné větvi.

| Téma předávky | Ověřený stav a závěr | Lokální kontrola |
| --- | --- | --- |
| 2 reporty × 3 predikce, GPT-4o/OpenRouter, Agent + fact-check | Souhlasí s `main.py:870–888`. Starý návrh Grok ensemble se neuplatňuje. Produkční hodnoty nebyly změněny. | Integrační průchod vytvoří 6 simulovaných prognostických odpovědí, 12 parsovacích odpovědí a 2 shrnutí. |
| Semafor omezuje celé otázky (§1.4) | Nesouhlasí. `main.py:134–135` obaluje jen rešerši. V předchozím auditu se 3 otázkami bylo 18 prognostických volání současně. | `CONCURRENCY-1`; deterministická bariéra bez čekání na skutečné API. |
| Hlavní workflow je nyní ruční (§2.1) | Souhlasí s `run_bot_on_tournament.yaml:3–4`. Chybějící cron není požadavek na opravu. README s příslibem 20 minut je zastaralé. | Ochrana manuálního triggeru; žádné změny plánovače. |
| Concurrency jiných workflow řeší fronty | Skupiny podle `github.workflow` nechrání účet napříč různými workflow; hlavní workflow nemá skupinu vůbec. Nelze bez logů určit, které historické běhy čekaly a které pracovaly. | `CONCURRENCY-2`, `PUBLICATION-1`. |
| 403 způsobily nezpůsobilé placené kredity (§3) | Tuto historickou příčinu uvádí předávka. Kód opravdu nemá retry; není důvod přidávat retry 403. | 403 skončí po jediném pokusu o konkrétní rešerši; chyba zabrání místní simulaci publikování. Dva reporty mohou každý nezávisle selhat, nejde o retry. |
| Retry pokus byl zrušen (§4) | Souhlasí: vlastní Agent cesta nemá bounded retry 429/5xx/timeout. Neměníme to v testovacím PR. | `RESEARCH-3`: 429, 503 a timeout; budoucí backoff je v testech simulován. |
| Dlouhé běhy nejsou nutně nekonečná smyčka (§2.3) | Souhlasí. Čtyři serializované pomalé Agent odpovědi mohou zabrat téměř 12 minut na otázku. 180 s je socketový timeout, nikoli deadline celé otázky. Joby nemají vlastní `timeout-minutes`. | Zachování předávaných 180 s; `TIMEOUT-1` pro explicitní limit jobů; `TIMEOUT-2` pro dokončení otázky se zaseknutým simulovaným modelem. Celkový deadline dosud chybí. |
| Close vs. resolution oprava (§7) | Silné pravidlo v binary promptu existuje. MC/numeric mají oba časy, ale při dostupném `actual_close_time` používají plánovaný close, zatímco rešerše a binary používají helper. Date prompt nemá srovnatelnou timing sekci. | Prochází close=0 / resolution v budoucnu pro binary/MC/numeric; `TIMING-1` reprodukuje nesoulad effective-close u MC/numeric. |
| Hard bounds, 180/730, strict percentiles (§8) | Pravidla v numeric promptu existují. Nominální meze však helper může prezentovat jako skutečné tvrdé meze, i když CDF používá jiný rozsah. | Zachování kumulativních/max/min/0–100/−100..100 pravidel, elapsed time a jednotek; `BOUNDS-1`. Nejde o důkaz, že model vždy dodrží fyzikální meze. |
| FDA/WastewaterSCAN options (§6.4, §9) | Initial research je dostává. Fact-check nemá samostatné options ani fine print; možnosti se do něj mohou dostat jen náhodně z kritérií či původní rešerše. | `CONTEXT-1/2`; options test záměrně nepřipouští falešný průchod díky jejich opakování v kritériích. |
| CBO FY2027 7.772 vs FY2028 8.151 (§6.2) | Pravidla zachování páru rok–hodnota a exact metric v kódu existují. Druhý Agent pass nahrazuje původní rešerši opravenou. | Simulovaná oprava 7.772 → 8.151 projde do forecast kontextu; nedokazuje schopnost skutečného modelu chybu najít. |
| VVIX max/min fix má jen technical PASS (§6.9) | Souhlasí. Instrukce je přítomná; nový test chrání její zachování a předání relevantního případu. | Žádné tvrzení o semantic PASS historické analýzy oken ani o kvalitě prognózy. |
| Warning o MC sum=1 není sám o sobě chyba (§9.4) | Souhlasí. Validní rozdělení je normalizováno. Skutečná chyba je chybějící validace unikátnosti a přesné množiny labels. | Validní FDA rozdělení prochází; `MC-1/2/3` odhalují duplicity, chybějící i cizí možnosti. |
| Oba CLI režimy mohou publikovat (§1.5) | Souhlasí; nebyly spuštěny. Bezpečný CLI dry-run zatím neexistuje. | Všechny publikující testy mají čistě paměťový sink nebo fake HTTP. |
| Atomické publikování a ochrana před opakováním | Audit navíc prokázal částečný zápis: forecast uspěje, komentář selže. SDK opakuje POST po ztrátě potvrzení. | Charakterizační test částečného zápisu a `PUBLICATION-2` pro duplicitní komentář. |
| Předávání kontextu podmíněných otázek | Audit navíc zjistil, že `main.py:747–752` zahazuje rozšířený kontext rodiče. | `CONTEXT-3`. |
| Chyby všech otázek a exit status | Podezření, že samotné `return_exceptions=True` znamená zelený job, se nepotvrdilo. `log_report_summary` v uzamčeném SDK standardně vyvolá chybu. | Procházející regresní test; konečný banner za touto funkcí se při chybě nemusí vypsat. |

## Jak číst výsledek

Ověření v této instanci: **50 testů, 30 procházejících a 20 očekávaných selhání**
otevřených kontraktů. Výchozí režim končí 0; přísný režim končí 1 s těmito
20 selháními. Žádné další chyby, přeskočené testy ani nečekané pokusy o živé
API. Není to důkaz opravy těchto chyb ani semantic PASS skutečného modelu.

Výchozí runner odděluje procházející testy a známé nedostatky. Známá chyba není
opravená tím, že je označená jako očekávané selhání. Režim `--strict-known-bugs`
vrací pro současný stav nenulový návratový kód. Žádné testy nevyžadují reálné klíče,
API ani spuštění produkčního CLI. Číselná rozdělení a data se ověřují přes skutečné
lokální parsery/SDK; obsah modelů je deterministická simulace.

## Doporučené pořadí samostatných oprav

1. **Publikační bezpečnost:** společný zámek účtu/otázky, idempotence při nejasném
   výsledku HTTP, evidence částečných zápisů a možnost doplnění komentáře.
2. **Časové a kapacitní limity:** rozpočet celé otázky a běhu, korektní ukončení
   blokujícího HTTP workeru, souběžnost celých otázek a rate limit API. Změny
   workflow timeoutů patří do samostatně schváleného provozního PR.
3. **Rešerše:** odmítat neúspěšné/prázdné výsledky, rozlišovat 403 billing od 429,
   5xx a timeoutů. Opakovat pouze přechodné chyby, omezeně a uvnitř deadline.
4. **Deterministická validace MC:** přesná množina možností, bez duplicit, konečný
   payload se součtem 1. Zachovat aktuální validní normalizaci.
5. **Kontext a hranice:** fact-check options/fine print, effective-close konzistence,
   nominální versus tvrdé meze a kontext podmíněných otázek. Ověřit reprezentativní
   případy před jakýmkoli zásahem do prognostických promptů.

Další nálezy původního auditu (vstup `days` v review workflow, neúplný preflight
klíčů, alternativní `main_with_no_framework.py`) nejsou touto sadou kompletně
pokryty. Zůstávají otevřené; nepovažovat je za opravené. Stejně tak zůstávají mimo
ověření skutečné náklady, structured-data retrieval, tracker transitions,
kalibrace a historická příčina několikahodinových incidentů.
