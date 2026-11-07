# Vrchcab: bezpečná inicializace nové publikační epochy bez opakování starých pokusů
*Příprava, nikoli povolení skutečné inicializace; 7. listopadu 2026.*

## Rozhodnutí operátora

Původní nejasné publikace zůstávají historicky **nezrekoncilované**; nebudou automaticky znovu odesílány ani jinak vyhodnocovány na základě pouhých shodných forecastů nebo prázdné historie. Uživatel 7. 11. výslovně schválil **konzervativní uzavření** starých pokusů bez jejich opakování. Tím nedal svolení ke spuštění první živé předpovědi ani ke spuštění samotné inicializace.

Prokázané poznatky jsou v [forenzní zprávě](https://github.com/mcabejsek-creator/metac-bot-template/blob/diagnostics/open-questions-offline-20261107/docs/vrchcab_publication_reconciliation_2026-11-07.md):
- Token `METACULUS_TOKEN_V2` autentizuje bota Vrchcab; původní `METACULUS_TOKEN` server po rotaci odmítl HTTP 403.
- Starý GitHub run `34463156990` je stále `queued`, ale má 0 jobs; GitHub Support ticket `4966035` je nadále otevřený bez věcné odpovědi k odstranění běhu.
- Čtení čtyř známých projektů evidovalo 111 záznamů FutureEval, 60 MiniBench, 780 opakovaných testovacích záznamů a 0 v Fall Metaculus Cup. Čísla **nepředstavují globálně deduplikovaný a kompletní seznam operací**.
- Opravený kód byl nasazen do `main` [PR #3](https://github.com/mcabejsek-creator/metac-bot-template/pull/3). Zatím nebyl inicializován žurnál ani povoleno automatické publikování.

## Technická změna navržená v tomto PR

1. Inicializační workflow zůstává **pouze ruční**. Místo `all-prior-runs-reconciled` akceptuje **pouze** `legacy-frozen-no-retries`, výchozí možnost je `not-confirmed`.
2. Python CLI vyžaduje současně `--initialize --confirm-legacy-frozen-no-retries`; starý příznak už neakceptuje. Chybějící nebo staré potvrzení blokuje dříve, než se vytvoří GitHub klient nebo provede operace se stavem.
3. Prázdný manifest žurnálu nově obsahuje autentizované šifrované metadata `legacy_cutover`, jež **výslovně** uvádějí `historic_outcomes: not-fully-reconciled` a `disposition: abandoned-without-replay`. Neimportuje dřívější forecasty či komentáře, nepřepisuje jejich hodnoty a nic neposílá na Metaculus.
4. Existující protokol vytvoření orphan větve `metaculus-publication-state` funguje jen `create-if-absent`, po chybě/nejistém ACK nikdy neprovádí reset. Šifrovací klíč je odvozen z nového stabilního tokenu `METACULUS_TOKEN_V2`; po inicializaci ho **nerotovat bez migračního plánu**.
5. Všechny dostupné regresní testy musí projít offline na čistém GitHub runneru. Nejsou poskytována API tajemství a neprovádějí se žádné forecasty.

## Podmínky před samostatně schválenou inicializací

- Zkontrolovat, že produkční `main` obsahuje otestovaný změnový balíček a že hlavní publikační workflow, Cup a Test Bot zůstávají bez automatických triggerů, nikdo je právě nespustil a externí cron-job.org je vypnutý.
- Potvrdit, že staré GitHub run ID `34463156990` nedisponuje platným tokenem (ověřili jsme odmítnutí starého API klíče) a stále má 0 aktivních jobs. Není nutné čekat na administrativní smazání příspěvku z fronty, **pokud** všechny jeho oprávněné publikační prostředky byly zneplatněny a nic neběží.
- Ověřit, že `metaculus-publication-state` dosud neexistuje. Když už existuje nebo existence není ověřitelná, **zastavit a neprovádět opětovnou inicializaci**.
- Potvrdit výslovně uživateli jednu separátní operaci: **Vytvořit novou šifrovanou větev na GitHubu, bez publikování na Metaculus.** Není to obsaženo v aktuálním souhlasu s úpravou kódu.
- Při ručním provedení vybrat `Initialize publication state` → `legacy-frozen-no-retries`; nikdy nepředstírat, že byly všechny předchozí operace potvrzené. Zkontrolovat úspěch a přítomnost šifrovaného manifestu. Neprozrazovat token ani v logu.

## Po případné inicializaci (samostatně schvalovaný krok)

Spustit nanejvýš **jeden** předem schválený a sledovaný běh `tournament` (FutureEval 33121 a MiniBench; `skip_previously_forecasted_questions=True`). Tento běh **může vyvolat reálné placené AI volání a publikovat forecasty i komentáře**. Proto je to jiné oprávnění než vytvoření GitHub žurnálu. Metaculus Cup, Test Bot a externí cron-job.org ponechat vypnuté, dokud nejsou samostatně schválené. Historické runy nikdy nere-runovat, historické komentáře automaticky nedoplňovat.

## Pokud selže jakákoliv fáze

Fail closed. Žádné retry starého runu, reset žurnálu, změna scope/path ani vytvoření náhradní stavové větve. Zaznamenat faktický stav a vyžádat cílenou kontrolu.
