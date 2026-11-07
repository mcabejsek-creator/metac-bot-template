# Bezpečné publikování: první skupina oprav

Tento dokument popisuje lokální základ `d44239a`. Navazující řešení pro nezávislé
Actions runnery je v [samostatném popisu](github-actions-publication.md); historická
omezení tohoto lokálního základu se nemají zaměňovat za stav nového backendu.

Navazuje na `fd407db` ve větvi `fix/idempotent-publication`. Řeší pouze hranici
zápisu předpovědi a komentáře. Prognostické metody a prompty třídy
`SummerTemplateBot2026`, modely, 2×3 parametry, CLI konfigurace, časování Actions,
secrets a závislosti zůstávají beze změny. Neproběhlo žádné živé API volání.

## Co je opravené

`SafePublicationMixin` ponechává generování reportu původnímu SDK. Jeho klient
nejdřív zachytí všechny plánované zápisy daného reportu, včetně přesných payloadů,
textu komentáře a nastavení soukromí. Celý plán i původní report se atomicky uloží
do SQLite **před prvním HTTP zápisem**. Při soupeření o stejnou identitu vyhrává
první uložený report; nevznikne předpověď jednoho workeru s komentářem druhého.
Samotný žurnál a koordinátor v `publication_journal.py` používají jen standardní
knihovnu; jejich načtení neimportuje SDK ani modely.

Každý krok se před HTTP atomicky označí jako `sending` a commitne. Jiný proces
se stejným žurnálem tento krok neodešle. Transakce není otevřená během HTTP.
Potvrzené kroky se při obnově přeskočí. Nejde o zámek s expirací: automatické
vypršení by umožnilo opakování požadavku, který ještě může běžet na serveru.

| Stav | Význam a obnova |
| --- | --- |
| `ready` | Plán je trvale uložený; odeslání zatím nezačalo. |
| `sending` | Pokus byl rezervován; může běžet nebo proces mohl zaniknout. Další odeslání se blokuje. |
| `confirmed` | Terminální úspěšná odpověď HTTP a potvrzení trvale zapsané v žurnálu. Znovu se neposílá. |
| `rejected` | Jednoznačné odmítnutí HTTP, například 403/429. Aktuální běh skončí chybou; pozdější běh může zkusit pouze tento krok. |
| `uncertain` | Timeout, přerušené spojení, 5xx, redirect, neterminální 202 nebo jiný nejasný výsledek. Automaticky se neopakuje. |

Pro skutečný SDK klient jsou POSTy odesílány přímo přes `requests.post`, jednotlivě,
se stejnými payloady, auth/proxy/CA nastavením, 30s timeoutem klienta a původním
throttlingem. Tím se obcházejí právě SDK retry dekorátory zápisů. Redirecty se
nenásledují, aby další POST nevznikl mimo žurnál. Nezasahuje se do retry čtení,
výzkumných API ani dalších problémových oblastí.

Explicitně odmítnuté statusy: 400, 401, 403, 404, 405, 413, 415, 422 a 429.
200/201/204 jsou terminální potvrzení podle standardního HTTP kontraktu.
Ostatní odpovědi se konzervativně považují za nejasné. Není přidán automatický
retry v témže běhu ani předpoklad, že Metaculus podporuje `Idempotency-Key`.

Když je předpověď potvrzená a komentář odmítnutý, nový běh použije původní report
a odešle pouze komentář, i když načtená otázka již má `already_forecasted=True`.
Nevolá znovu modely a nemění předpověď. Plně dokončená již forecastovaná otázka
se dále běžně přeskočí. Nejasný výsledek nezmizí ani po restartu procesu.
Při obnovení uloženého reportu se po strukturální validaci zachovají původní
číselné hodnoty: SDK after-validator jinak může znovu normalizovat MC rozdělení.
Odesílané payloady vždy pocházejí z původního uloženého plánu.

Nový záměr přeforecastování je zablokován, dokud není dokončen nebo vyjasněn
předchozí rozpracovaný záměr téže otázky. Jednoznačně odmítnutý starý krok se
nejprve dokončí. Tím se starý nedokončený komentář nemíchá s novou předpovědí.

## Identita a uložení

- Výchozí soubor: `.cache/metaculus-publication.sqlite3` vedle `main.py`, již
  ignorovaný existujícím `.gitignore`. Nový soubor má práva 0600; reporty a
  komentáře jsou soukromá data. Neukládají se auth hlavičky ani přístupové klíče.
- `METACULUS_PUBLICATION_DB`: volitelná cesta k trvalému žurnálu.
- `METACULUS_PUBLICATION_SCOPE`: stabilní nesekretní jméno bot účtu, výchozí
  `default-bot`. Více účtů musí mít různá jména; všichni workeři stejného účtu
  stejné jméno a stejný žurnál. Identita se neodvozuje z tokenu, který se může
  měnit nebo být proxy placeholder. Scope je konfigurace, ne ověření účtu API.
- Klíč zahrnuje scope, endpoint API, typ otázky a její question/post ID.
- Při `skip_previously_forecasted_questions=True` jde o stabilní počáteční
  publikaci. Změna vygenerovaných čísel nebo komentáře nevytvoří nový záměr.
- Při záměrném přeforecastování (`False`, například Cup/test mode) je záměr
  identifikován `METACULUS_PUBLICATION_INTENT`, jinak `GITHUB_RUN_ID`, jinak UUID
  dané instance bota. Rerun stejného GitHub runu zachová záměr. Nový skutečný
  run může vytvořit novou předpověď, jak dosavadní nastavení vyžaduje.
- V testech se explicitně injektuje dočasný žurnál; žádná produkční data se
  nepoužívají. Vlastní klient musí deklarovat `publication_single_attempt=True`
  a skutečně garantovat jednopokusové zápisy; jinak je odmítnut před publikací.

Žurnál musí přežít restart a být společný pro spolupracující procesy. Nemazat ho
jako běžnou cache, nepřepisovat starým snapshotem a nevytvářet nezávislé kopie pro
stejné workery. Chyba nebo poškození žurnálu zastaví publikování. SQLite je zde
ověřeno na lokálním filesystemu; pro distribuované síťové filesystémy není
deklarovaná garance. Sledujte místo na disku a chraňte zálohy reportů.

## Nejasný výsledek vyžaduje důkaz

Timeout po přijetí zápisu a timeout před přijetím jsou pro klienta nerozlišitelné.
Proto se neslibuje „exactly once“. Návrat k dostupnosti vyžaduje ověření na
serveru a zastavení původních workerů; žádné takové ověření nebylo nyní provedeno.

`PublicationStore.steps(key)` poskytne pořadí, stav a hash konkrétní operace.
`reconcile(key, position, operation_hash, accepted=True)` označí externě ověřený
již provedený zápis. `accepted=False` uvolní nový pokus **jen po důkazu, že starý
zápis nebyl proveden a už nemůže doběhnout**. Vyžaduje se správný hash a nejasný
stav; nelze tím přepsat potvrzené operace. Není tu hromadný „force retry“ ani TTL.
Neověřený nebo opožděný prázdný výsledek čtení není důkaz neprovedení zápisu.

Obnova odmítnutého komentáře je automatická, pokud se otázka dostane do dalšího
volání bota. Uzavřená otázka, která už není ve výběru otevřených otázek, zůstane
evidovaná v žurnálu; vyžaduje cílené dokončení uloženého plánu přes `Publisher`.
V této změně není přidán nový síťový vyhledávač ani samostatný provozní příkaz.

## Limity mezi GitHub runnery a mimo tento bot

**Nezávislé GitHub Actions runnery výchozí lokální soubor nesdílejí. Tato změna
proto sama spolehlivě nezabrání duplicitám mezi nimi.** Workflow nebyla upravena;
test `CONCURRENCY-2` zůstává otevřený. Je zapotřebí společná koordinace a trvalé
úložiště, nebo serverem skutečně podporovaná idempotence. Pouhé přidání hlavičky,
GitHub cache se starými snapshoty nebo kopírování SQLite souboru mezi běhy není
důkazem bezpečnosti. Bez podpory serveru nelze současně garantovat bezduplicitní
zápis a automatický retry po každé ztracené odpovědi.

Ochrana se vztahuje na publikace koordinované `SummerTemplateBot2026`, se stejnou
identitou reportu a záměru. Přímé zápisy přes původní SDK nebo alternativní
`main_with_no_framework.py` ji obcházejí. Libovolné další skripty/účty bez stejné
koordinace nebo jiné záměry nejsou automaticky globálně deduplikované. Podmíněné
reporty zachovávají původní SDK pořadí jednotlivých yes/no zápisů.

## Ověření

Původní `PUBLICATION-1/2` jsou povýšeny na procházející testy. Nové lokální testy
pokrývají odmítnutý komentář, ztracené potvrzení předpovědi i komentáře, restart,
selhání checkpointu po úspěšném zápisu, atomické claims mezi dvěma nezávislými
procesy, první vítězný report, oddělení kontextů, různé účty/záměry a zachování
payloadů binary/MC/numeric/date/conditional. Ostatní známé chyby zůstávají označené.

Spouštění: `.venv/bin/python -B -m tests.run`. Všechny transporty a modely jsou
simulované; žádné tajné hodnoty, skutečné API, změna Secrets, push ani nasazení.

Výsledek v této instanci: **74 testů, 56 procházejících, 18 očekávaných selhání**,
bez dalších chyb, přeskočených testů a nečekaných pokusů o živé API. Původní
`PUBLICATION-1/2` jsou odstraněny z očekávaných selhání; všech zbývajících
18 případů přesně odpovídá původní sadě. Přísný režim skončí 1 právě s těmito
18 selháními. Patří mezi ně i nevyřešené propojení nezávislých Actions workflow
(`CONCURRENCY-2`), které tento lokální žurnál sám neřeší.
