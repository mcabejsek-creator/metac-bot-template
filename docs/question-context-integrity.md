# Zachování kontextu otázky

Lokální větev `fix/question-context-integrity` navazuje na `816c171`.
Žádný workflow, Secret, model, teplota, počet vzorků, práh úspěšnosti, medián,
interpolace, pravděpodobnostní clamp, runtime limit ani publikační transakce se nemění.
Nebylo spuštěno produkční CLI ani živé API.

## Proč šest selhání a pět identifikátorů

Výchozí offline běh skutečně obsahoval 214 testů: 208 prošlo, 6 bylo očekávaných.
Identifikátor označuje problém, nikoli jednotlivý test:

| Identifikátor | Testy | Příčina |
| --- | ---: | --- |
| TIMING-1 | 2 | Numeric a MC používaly scheduled close, rešerše actual close. |
| BOUNDS-1 | 1 | Nominal range přepsal skutečné uzavřené meze. |
| CONTEXT-1 | 1 | Fact-check nedostával explicitní možnosti odpovědí. |
| CONTEXT-2 | 1 | Fact-check nedostával fine print. |
| CONTEXT-3 | 1 | Výsledek parent se zahodil před child a dalšími větvemi. |

Všech šest testů bylo opraveno a převedeno na běžné regrese, bez výjimek/skipů.

## Data, validace a dopady na chování

`question_context.py` vytváří úplný kontrakt z dostupného snapshotu: text otázky,
background, přesná kritéria, fine print, options, jednotky, označení skupinové
podotázky, skutečné i nominal meze a jejich otevřenost/scaling. Zvlášť označuje
open, scheduled close, effective close, scheduled resolution a actual resolution.
Období výsledku se nepřepisuje termínem uzávěrky či vyhodnocení; zůstává ve
zdrojovém znění otázky a podmínek. Neprovádí se heuristické vytahování roku,
hodnoty nebo nového numerického omezení z přirozeného jazyka.

Actual close má přednost, včetně epochy `0`. Neplatný explicitní actual close
zastaví zpracování; nesmí se tiše použít scheduled close. Chybějící/None actual
close stále používá scheduled close. ISO offset se normalizuje na UTC, API ISO
bez zóny zachovává SDK konvenci UTC. Naivní datetime v již vytvořeném modelu
se odmítá. Každá fáze používá jeden UTC časový okamžik a UTC datum i pro rozdíly
dnů. Kalendářní rozdíl dnů není nový časový limit ani přesný počet zbývajících hodin.

Closed numeric bound používá skutečný `lower_bound`/`upper_bound`; nominal hint
zůstává pouze na odpovídající otevřené straně. Date bounds zachovávají celý
UTC čas včetně hodin/sekund. Validace percentilů a CDF z předchozí opravy zůstává.

Zdrojové řetězce se před SDK `clean_indents` nebo formátováním Markdownu dočasně
nahradí neprůhlednými značkami a obnoví před skutečným vstupem do modelu/reportu.
Není zde ořezávání, filtrování řádků ani rekonstrukce tabulek. Tím se zachovají
vnořené podmínky, prázdné řádky, code fences i přesné dvojice rok–hodnota.

Původní SDK navíc při rozbalení skupiny přepisuje description/criteria/fine_print
člena společným textem skupiny. Přenášíme obě původní sady textů z již dostupného
API snapshotu, odděleně označené, včetně původního label/unit. Neměníme SDK
rozbalování ani neodhadujeme prioritu případných protichůdných pravidel.
Nejednoznačná identita původního člena se odmítne. Komunitní odhady ani
`my_forecasts` se do nových metadat nekopírují.

U conditional všechny čtyři větve dostávají úplné původní kontrakty komponent,
protože SDK branch šablony také upravují whitespace původních podmínek. Parent
reasoning pokračuje přes child, yes i no; známá potvrzená parent/child hodnota
se přidává do kontextu také při `PredictionAffirmed`. Expirace/force reforecast
nadále rozhodují stejně a přidání kontextu nevytváří nové modelové volání.

## Jednotlivé nezbytné změny vstupů/promptů

Následující změny jsou datové doplnění nebo oprava významu časů/mezí. Původní
instrukce pro hledání důkazů, práci s base rates, kalibraci, percentily, finální
formát, schéma parseru a počet jeho validačních vzorků zůstávají zachovány.

### 1. První rešerše: jednotky, meze, skupinové údaje a období

Před: title, options, background, časové údaje, criteria a fine print.
Po: stejné položky a navíc `Measurement metadata (unabridged)` s jednotkami,
hard/open/nominal mezemi, scalingem a dostupnými původními skupinovými texty.
Přidán přesný datový popisek:

```text
Target period: specified by the question text and resolution criteria/fine print; not inferred from submission close or resolution schedule.
```

Důvod: ani rešeršér nesmí odvozovat jednotku, matematicky možný rozsah či období
ze scheduled resolution nebo pravděpodobného rozsahu tvůrce.

### 2. Fact-check: celý kontrakt místo title a criteria

Před: `Forecast question: {question_text}` a `Resolution criteria: {criteria}`.
Po: `Question contract (unabridged source data)` se všemi položkami výše, včetně
explicitních options, fine print, background, časů a jednotek. `Original research`
se předává celé, beze změn whitespace. Ostatní kontrolní instrukce jsou původní.
Důvod: samostatná druhá fáze nemůže spoléhat, že první rešerše zopakovala pravidla.

### 3. Prognózy binary/MC/numeric/date: data a UTC

Do všech čtyř vstupů přibyla stejná measurement metadata a úplná UTC timing
metadata (včetně scheduled/effective close a actual resolution); `Today is ...` nyní
vychází z UTC, stejně jako rozdíly dnů. MC a numeric používají effective close
ve všech výrazech pro close, zbývající čas i numeric total open duration.
MC a numeric dostávají samostatný popisek cílového období z bodu 1; stávající
binary instrukce již odlišuje deadline výsledku od uzávěrky. Podmínky, evidence
i výstupní formát se zachovávají celé.
Důvod: oprava TIMING-1 a jednotný význam vstupních dat, bez změny prognostické metody.

Samostatná nutná změna původní MC instrukce:

```diff
- Do not calculate or infer a different remaining time horizon.
+ Do not infer the target period from the close time or the scheduled resolution time; use the question text, resolution criteria and fine print.
```

Původní zákaz mohl zakazovat i použití správného cílového období, liší-li se od
naplánovaného vyhodnocení. Nová věta vyjasňuje význam dat; žádný termín ani
pravděpodobnostní postup nepředepisuje. Zbytek původní MC věty zůstává zachován.

### 4. Numeric/date: konkrétní bound hodnoty

Před při closed 0–100, nominal 20–80: `The outcome can not be ... 20/80`.
Po: stejná věta obsahuje `0/100`. Open strana dál používá původní `likely ...`
s nominal hintem. Date dříve `2026-12-01`, nyní např.
`2026-12-01T13:20:30+00:00`, pokud to je skutečný bound.
Důvod: původní vazba dat vytvářela neexistující tvrdé podmínky a zahazovala hodiny.

### 5. Date: chybějící časové údaje

Před: pouze dnešní datum a date bounds. Po navíc:

```text
Question timing from Metaculus (authoritative):
- Open time: ...
- Close time: ...
- Scheduled resolution time: ...
- Days until close: ...
- Days until scheduled resolution: ...
Target period: specified by the question text and resolution criteria/fine print; not inferred from submission close or resolution schedule.
```

Důvod: date fáze jinak neměla explicitní informaci o třech různých časových rolích.

### 6. Souhrn: úplné podklady a kontrakt

Původní SDK instrukce „summarize ... in 1-2 paragraphs“ zůstává. Předání evidence
chráníme před `clean_indents` a na konec vstupu přidáváme celý kontrakt otázky.
Výchozí `use_research_summary_to_forecast=False` stále posílá prognostikovi
původní celou rešerši. Je-li volba explicitně True, vstupem je souhrn **plus**
celá ověřená rešerše a kontrakt. Prázdný souhrn se v tomto režimu odmítne.
Důvod: nelze automaticky prokázat, že modelový souhrn uchoval každý rok/hodnotu
a každou podmínku; zde je dopad proti původnímu summary-only chování záměrný.
Nastavení volby ani počet modelových volání se nemění. Obsah nemůže kontaminovat
jinou souběžnou otázku: kontext je lokální pro danou coroutine.

### 7. Parser: celý kontrakt a nezkrácené reasoning

Před: SDK instrukce, schéma, reasoning a existující dodatečné instrukce pro typ.
Po: stejné části, reasoning/dodatečné instrukce uchované beze ztráty whitespace,
a na konci celý kontrakt včetně fine print, options, jednotek a časů.
Důvod: parser potřebuje jednotku a přesné možnosti stejně jako prognostik;
nesmí se řídit pouze jejich případným opisem v reasoning. Neměníme instrukce
pro konverzi jednotek, pravděpodobnostní normalizaci ani validaci vzorků.

### 8. Conditional: oprava předávaného argumentu

Před: child dostal původní research, parent reasoning se zahodil.
Po: child dostává `full_research` po parent; další větve jej dále doplňují.
Na začátku je dostupný úplný kontrakt všech komponent a při affirm přibývá známá
hodnota. Text původních conditional instrukcí se nemění. Důvod: CONTEXT-3 a
zachování přesných původních podmínek přes SDK branch šablony.

## Report, publikační žurnál a GitHub Actions

Nalezen další problém: SDK komentář nad 150 000 znaků tiše zkracovalo na prvních
2 000 znaků. Nově se plná explanation sestaví bez ztrát a nad stejným SDK limitem
se odmítne **před uložením plánu a prvním zápisem**. Stejně se odmítne již
uložený nedokončený komentář s původním SDK truncation suffixem. Neprobíhá jeho
regenerace, automatické oříznutí ani opakování již potvrzené předpovědi.

Report má stejné tři hlavní SDK sekce SUMMARY/RESEARCH/FORECASTS. Zdrojové texty
jsou zobrazeny jako odsazené literal bloky, aby jejich vlastní Markdown nadpisy
nerozbíjely strukturu reportu; odstranění čtyř přidaných úvodních mezer obnoví
přesný zdrojový řetězec. Celý kontrakt je uložen uvnitř RESEARCH. Změní se tedy
vzhled těchto částí komentáře, nikoli jeho číselná prognóza.

Porovnání právě dostupného a zmrazeného snapshotu nyní kromě dosavadních polí
zahrnuje timing, background, group label a measurement/source metadata. Jejich
změna zablokuje nedokončený plán ještě před claim. Úplně confirmed plán se stále
neodesílá; sending/uncertain zůstává v karanténě. Staré plány nepřepisujeme.
Čas dotazu ani proměnlivé počty účastníků se do porovnání nevážou. Potvrzený
forecast + odmítnutý komentář tedy může při změně závazných dat vyžadovat ruční
kontrolu; tato blokace je záměrná.

GitHub CAS/encryption, společný workflow lock, run intent, jednopokusové POSTy,
deadliny, cancel handling a research retry politika jsou beze změny. Offline
testy zahrnují nezávislé instance store/runnerů se sdíleným simulovaným GitHubem,
změnu termínů po částečném úspěchu a starý oříznutý plán bez nového claim/zápisu.

## Ověření a omezení

Úplná sada: **242 prošlo, 0 očekávaných či jiných selhání, 0 skipů a 0
neočekávaných pokusů o živé volání**. Totéž se ověřuje v přísném režimu i se
simulovanými GitHub Actions proměnnými. Nových 28 testů pokrývá UTC/epochu 0,
časové role a období, closed/open/nominal meze, přesné intraday date bounds,
multiline/pozdní podmínky, skupinové zdroje, summary-only ochranu, parser,
conditional návaznost/affirm/expiraci, celý report a obnovu žurnálu.

Reprodukce bez klíčů a s aktivními zábranami živé sítě:

```bash
.venv/bin/python -B -m tests.run --strict-known-bugs
GITHUB_ACTIONS=true GITHUB_REPOSITORY=offline-owner/offline-repo \
  GITHUB_RUN_ID=4242 GITHUB_RUN_ATTEMPT=2 METACULUS_PUBLICATION_BACKEND=github \
  .venv/bin/python -B -m tests.run
```

AST porovnání s `816c171` potvrdilo stejné CLI modely/ensemble i prognostické
konverze/clamp/CDF volání po odečtení ochrany předávaných řetězců. Runtime,
research retry, publikační backend, závislosti a workflow soubory jsou vůči
tomuto commitu bajtově totožné.

Síťové i modelové zábrany zůstávají aktivní. Výsledky ověřují přenos dat a
odmítnutí technicky neplatných vstupů, nikoli pravdivost rešerše nebo úsudek LLM.
Neodhalí obecně rozporné přirozenojazyčné podmínky ani nedokážou, že model
pochopil jednotku, rozlišení observed/projected či správně převzal řádek tabulky.
Explicitní numerické API meze se validují, odvozené věcné meze musí správně
vyhodnotit prognostik. Větší úplný kontext může zvýšit náklady nebo vyčerpat
kontext modelu; bot jej automaticky neořezává. Při nedostatku validních dílčích
prognóz skončí původními chybovými cestami bez publikování.

Live API kontrakt nebyl znovu ověřen. Bez serverové idempotence/zjištění výsledku
se nejasný publikační zápis stále nesmí automaticky opakovat. Změnu otázky na
serveru po načtení snapshotu nelze atomicky vyloučit. SDK log summary nadále
zkracuje pouze zobrazení prvního reasoning na 10 000 znaků a chybové bannery;
pipeline, uložený report ani komentář se tím nezkracují. Legacy
`main_with_no_framework.py` není používán workflow a nebyl měněn. Lokální
Python 3.12 se simulací CI nenahrazuje skutečný GitHub runner s Pythonem 3.11.
