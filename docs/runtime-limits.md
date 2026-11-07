# Časové limity a ukončení bota

Následná větev `fix/research-validation-retries` doplňuje
[validaci rešerší a retry](research-safety.md), bez změny zde nastavených rozpočtů.
Počty testů níže popisují stav opravy `c71e79f`.

Navazuje na `8929cac`; změny jsou pouze ve větvi `fix/bounded-runtime`.
Žádné živé GitHub/Metaculus/modelové volání, push, merge, změna Secrets či nasazení
neproběhly. Produkční větev `main` se neměnila.

## Rozsah a limity

| Hranice | Limit | Co zahrnuje |
| --- | --- | --- |
| Publikující Actions job | 210 min | Instalaci, offline testy, bota a rezervu na ukončení. |
| Procesový dohled `bounded_run` | 180 min + nejvýše 60 s na ukončení | Celý podproces včetně importů a výpisu výsledků; TERM, poté KILL celé procesové skupiny. |
| Kooperativní běh bota | 180 min | Načítání otázek, fronty, oba turnaje v tournament módu a publikování. Vnořená volání rozpočet neresetují. |
| Přijatá běžná otázka | 60 min | Rešerše, souhrny, prognózy, parsery, agregaci, obnovu reportu i publikování. |
| Přijatá conditional otázka | 120 min | Totéž s až čtyřmi navazujícími subotázkami. |
| Jedno modelové `invoke` | 240 s | Celé SDK volání včetně jeho existujícího retry a backoffu. Platí i pro typed parser a summarizer. |
| Jedno volání Perplexity Agent | 210 s | Absolutní čekání na celé volání, včetně čtení těla; původní socket timeout 180 s zůstává. |
| Jedna synchronní operace žurnálu/publikování | 300 s | Celé čekání na operaci; nezastavuje ani neopakuje HTTP, který už probíhá. |
| Načtení turnaje | 600 s | SDK stránkování a jeho stávající retry; také pod celkovým rozpočtem. |

Limity jsou soustředěné v `RuntimeLimits`, bez dalších Secrets či závislostí.
Stávající transportní timeouty zůstaly: hlavní GPT-4o 40 s, parser/summarizer
podle SDK (zde 60 s), Metaculus 30 s, GitHub connect/read 10/30 s, Agent 180 s.
Nové absolutní deadliny pokrývají mezery, které samotný socket/read timeout
neřeší, například dlouhé čtení po částech nebo čekání mezi pokusy.

Původní 2 rešerše × 2 Agent průchody dávají při 180 s asi 12 minut. Šest
prognóz po 40 s, 12 parserových vzorků po 60 s a dva souhrny po 60 s znamenají
dalších 18 minut. Hodinová hranice ponechává zhruba dvojnásobek tohoto času pro
stávající retry a režii. Conditional může místo 6 vyžadovat až 24 dílčích prognóz;
proto má samostatný dvouhodinový rozpočet. Jde o strukturální odhad, nikoli měření
živé produkce. Maximální kombinace všech retry nemusí do limitu vejít; dlouhá
legitimní dávka může překročit celkový tříhodinový rozpočet. Pozdější ladění má
vycházet z produkčních časů, ne ze změny prognostických parametrů.

## Souběžnost a vlastnictví práce

Výchozí `_max_concurrent_questions = 1` je zachován. Nový limit přijetí otázky
platí od rešerše až po publikování. Samostatný sdílený limit náročných operací
omezuje dohromady Agent, všechny modelové role, načítání a publikační IO.
Šest prognóz uvnitř jedné otázky tak už neobejde nastavenou souběžnost.
Fronta spotřebovává čas běhu, ale vlastní deadline otázky začíná až po přijetí.
Semafory patří botovi a event loopu; nejsou globální zámek přes více procesů.
Mezi runnery nadále platí společná Actions skupina a trvalý GitHub CAS žurnál.

SDK sourozenecké úlohy se při chybě dávky, timeoutu či zrušení zruší a dočekají.
Notepady se uklidí i při selhání. Modely se obalují typově kompatibilní kopií;
prompty, modely, teplota, počet prognóz/vzorků, validace a SDK retry zůstávají.
Původní modelové objekty a metadata konfigurace se nemění.

Synchronní IO běží po jednotlivých operacích v daemon vláknech s kopií contextu.
Zrušený waiter nikdy nepokračuje dalším krokem pipeline. Permit IO se uvolní až po
skutečném návratu vlákna, nikoli při zrušení jeho awaitu. Zůstane-li worker živý,
bot nesmí přejít do jiného event loopu a použít novou sadu permitů. Daemon vlákna
neblokují ukončení defaultního executor poolu. Kontrola zrušení před novým GitHub
požadavkem a po Metaculus throttle brání odloženému zahájení dalšího požadavku.

## Konzistence publikování

Pořadí zůstává: trvalý plán → trvale potvrzené `sending` → jednopokusový POST →
trvale potvrzený výsledek. Klíče, šifrování, CAS, společný scope, run intent a
zákaz fallbacku v Actions jsou zachovány.

- Zrušení při ukládání plánu může nechat `ready`; příští běh obnoví stejný report.
- Zrušené čekání na rezervaci nemůže spustit POST v odpojeném workeru: rezervace
  a POST jsou dvě samostatné awaitované operace.
- Timeout/zrušení při POSTu ponechá `sending`. Worker může ještě dokončit právě
  probíhající požadavek, ale nezahájí další komentář ani jiný krok.
- Pokud je předpověď potvrzená a komentář nejistý, zůstává první `confirmed` a
  druhý blokovaný. Předpověď se neopakuje.
- Při zrušení během checkpointu může další runner najít nový potvrzený stav,
  nebo původní `sending`; oba případy brání duplicitnímu POSTu.

Timeout není důkaz odmítnutí požadavku. Blokace se automaticky neodemykají,
nevytváří se nová identita a nepřidávají se retry publikování. Požadavek, který
server již zpracovává, nelze klientským zrušením spolehlivě odvolat. Nadále platí
[podmínky sdíleného GitHub žurnálu](github-actions-publication.md), včetně zákazu
historických workflow rerunů a resetu stavové větve.

## Spouštění a ověření

Publikující workflow nyní používají např.:

```text
poetry run python -B -m bounded_run -- python main.py --mode metaculus_cup
```

Vnitřní CLI argumenty, turnaje a nastavení se neměnily; wrapper dodává tvrdou
procesovou hranici. Samotné `python main.py` či knihovní async volání mají
kooperativní deadliny. Pro tvrdou hranici na POSIX použijte wrapper. SIGTERM/SIGINT
zruší CLI task; procesový dohled odklidí celou skupinu i při nereagujícím SDK.
Crony, triggery, concurrency, GitHub oprávnění a Secrets vazby se nezměnily.

Offline sada: **148 testů, 134 prošlo, 14 očekávaných selhání** ostatních známých
chyb. Opravené kontrakty `TIMEOUT-1`, `TIMEOUT-2` a `CONCURRENCY-1` už jsou běžné
procházející testy. Stejně dopadl běh se simulovanými Actions proměnnými.
Přísný režim vrací kód 1 právě kvůli zbývajícím 14 známým chybám; žádná nová chyba,
skip ani živé volání nevznikly. Testy pokrývají fronty, modelové role a typed parser, zrušení
sourozenců, skutečné blokující lokální workery, všechny publikační hranice,
nezávislý runner, pozdní návrat IO a procesový TERM/KILL. Jediný nový spouštěný
podproces je čistě stdlib sleeper bez importu bota, klíčů či sítě.

Testovací event loop má lokální heartbeat, protože sandbox může zakázat i Unix
self-pipe zápis mezi vlákny. To se týká pouze test harnessu; síťové zábrany a
watchdog zůstaly. Produkční asyncio wakeup se nepatchuje. V reálných službách ani
na Actions Pythonu 3.11 nebyly změny spuštěny; offline prostředí používá 3.12.
Zůstává 14 známých selhání v oblasti rešerší, MC validace a předávání pravidel.
