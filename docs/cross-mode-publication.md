# PUB-MODE-1: obnova mezi režimy

Navazující [příprava nasazení](rollout-runbook.md) doplnila skutečný Python
3.11.17: 273/273 v strict režimu i simulovaných Actions. Níže uvedený původní
protokol a aplikační kód se nemění; skutečný GitHub běh zatím nebyl spuštěn.

Větev `fix/cross-mode-publication` navazuje na `0a98f97`. `main.py` je bajtově
totožný s tímto commitem: zachována je převzatá produkční změna MC promptu z
`91bf94b`, všechny ostatní prompty, modely, parametry a prognostická metodika.
Workflow, časové limity, research pravidla ani Secrets se nemění. Neproběhla
živá API volání, publikování, fetch, push, merge ani nasazení.

## Chování po opravě

Před opravou Cup uložil předpověď, komentář byl odmítnutý a tournament při
obnově komentáře vytvořil novou `initial` prognózu. Stejná chyba byla možná
v opačném směru: po dokončení cizího plánu následoval nový Cup intent.

Nově je rozhodnutí obnovit původní plán trvalé **před** jeho dokončením:

1. SQLite transakce vybere rozpracovaný plán a přiřadí k němu aktuální intent
   v tabulce `publication_aliases`. Při nezávislých runnerech se vazba nejdříve
   potvrdí přes stávající šifrovaný GitHub CAS.
2. Runner načte původní report a dokončí pouze nepotvrzené kroky původního plánu.
   Confirmed předpověď se neposílá a žádný model ani rešerše se znovu nespouští.
3. Rerun stejného intentu nadále odkazuje na stejný plán, i když se runner
   přerušil až po posledním confirmed checkpointu. Vznik vazby není odložený
   až za publikování komentáře.
4. Tournament/skip=True zohlední i dříve dokončený plán z Cup/test. Pozitivní
   žurnálový receipt brání nové prognóze i při zastaralém `already_forecasted=False`.
5. Po novém výpočtu se pod stejnou transakční/CAS ochranou znovu ověří, zda
   mezitím jiný runner nepřipravil či nedokončil použitelný plán. Pokud ano,
   nový výsledek se nepublikuje a použije se existující plán.

Vyřešen je také dodatečně reprodukovaný souběh: runner B může dokončit komentář
právě během ukládání vazby runneru A. Prosté nové hledání pending po CAS konfliktu
by znovu povolilo nový Cup forecast. CAS retry proto zachová již pozorovaný cíl
obnovy; nevysvětlí jeho dokončení jako povolení nového výpočtu. Trvale uložená
vazba stejného intentu má přednost a nikdy se nepřepisuje.

Vazby odkazují přímo na původní kanonické publikace stejného resource. Nekopírují
reporty, payloady, operation hashes ani stavy a netvoří řetězce aliasů. Stejný
scope/endpoint/type/question/post zůstává hranicí koordinace. Více nedokončených
legacy plánů nebo chybná vazba na jiný resource se odmítne.

**Zachované záměrné přeforecastování:** nový samostatný Cup/test run ID při absenci
rozpracovaného plánu stále smí vytvořit novou prognózu. Je to původní skip=False
chování. Rerun stejného run ID nový intent netvoří; běh, který sloužil k obnově,
si svou vazbu pamatuje. Převzetí pending se řeší po jednotlivých otázkách, ne
globálním zákazem prognóz ostatních nových otázek v dávce.

**Konzervativní pravidlo pro reruny Actions:** Cup/test s `GITHUB_RUN_ATTEMPT > 1`
smí obnovit uložený plán/vazbu nebo převzít dosud rozpracovaný plán. Pokud pro
aktuální intent nic takového není doloženo, zastaví se před modelem. První pokus
mohl selhat před uložením vazby a jiný runner mezitím dokončit starou publikaci;
absence záznamu nesmí rerunu povolit nový forecast. To může zablokovat i otázku,
na kterou první pokus ještě vůbec nedošel. Po kontrole je pro nový záměr potřeba
samostatné nové workflow spuštění, nikoli slepé opakování původního. Tournament
nadále může zpracovat skutečně nové otázky pod svým stabilním `initial` klíčem.
Číslo pokusu není součástí identity a neodemyká sending/uncertain; GitHub metadata
poskytuje automaticky, nový Secret ani repository variable nejsou potřeba.

Sending/uncertain zůstává zablokovaný ve všech režimech. Změna režimu ani nový
run ID nic neodemykají. Obnova ready plánu smí odeslat jeho původní dosud
neodeslanou předpověď; nikdy k ní nespouští nové modely. Reconcile stále vyžaduje
ověřený výsledek, zastavené původní workery a přesný operation hash. Ztracená ACK
vazby bez potvrzení nedovolí v tomto pokusu žádný Metaculus POST.

## Kompatibilita a provozní rizika

SQLite schema přechází z v1 na **v2** přidáním tabulky vazeb. Staré publikace,
jejich wire operace a výsledky se nepřepisují. U GitHubu migrace běží nad lokálním
snapshotem a trvale se uloží spolu s další změnou přes CAS; inicializátor, větev,
index, šifrovací envelope/AAD, klíč a pojmenování částí zůstávají stejné. Žádná
reinicializace již existujícího žurnálu není nutná ani přípustná.

Starší kód při otevření v2 schématu selže na kontrole verze. To je záměrná
ochrana: `0a98f97` ani starší verze vazbám nerozumějí a nejsou aktivní rollback
target. Neodstraňovat tabulku ani nesnižovat `user_version` kvůli rollbacku.
Již běžící staré procesy se tím zpětně neopraví; před přechodem je nutné je zastavit.
Nejnovější stav se při rollbacku kódu nikdy nevrací zpět; při problému vypnout
writery a připravit opravu vpřed s podporou v2.

Vazby zvětšují žurnál, zejména při mnoha nových bězích nad nevyjasněným zápisem.
Stávající velikostní/GitHub limity dál zastaví zápis bezpečně, nikoli resetem.
Pokud pending vznikne až během již rozběhnutého výpočtu jiného runneru, může být
tento výpočet zbytečný; finální transakce zabrání publikování jeho nadbytečného
plánu. Garance není univerzální exactly-once doručení: nejasný Metaculus výsledek
může vyžadovat ruční vyjasnění a do té doby omezí dostupnost.

## Ověření a připravenost

**273 testů prošlo, 0 selhání/expected failures/errors/skipů**, v přísném režimu
i se simulovanými GitHub Actions a opakovaným run attempt. Síťové a modelové
zábrany zůstaly aktivní; neočekávaných živých pokusů bylo 0. Původní PUB-MODE-1
prochází jako běžná regrese, nikoli xfail. Přibylo 30 testů včetně matice šesti
přechodů, obou typů CAS souběhu, všech existujících typů reportů, zastaralé history,
rerunů, cancel/lost ACK před i po checkpointu, v1 migrace, frozen payloadů
a karantény nejasného zápisu. Starý test zrušení uložení plánu nyní zachycuje
novou atomickou hranici `prepare_for_run`; jeho požadované chování zůstalo stejné.
Každá nová testovací fixture má vlastní první syntetický pokus; scénáře skutečného
rerunu nastavují attempt 2+ explicitně. Vnější CI attempt tak nepřeznačuje čerstvé
testovací databáze jako druhý pokus neexistujícího předchozího běhu.

Z hlediska lokálně ověřeného kódu je verze připravena k **řízenému nasazení**.
Před aktivací však nadále platí [plán cutover a ověření](deployment-readiness.md):
zastavit staré writery, vyjasnit jejich zápisy, ověřit oprávnění/stabilní token,
stavovou větev inicializovat pouze pokud dosud nikdy neexistovala, projít offline
gate na skutečném Actions Pythonu 3.11 a schválit jeden ruční tournament běh.
Nový Secret ani externí úložiště se nezavádí.

Skutečné GitHub oprávnění/rulesets, aktivní staré běhy, poslední stav main/state
větve a služby nebyly v tomto kroku online ověřovány. Poslední read-only pozorování
z předchozího auditu bylo main `91bf94b`, stavová větev chyběla. Před nasazením
ověřit aktuální stav znovu a zachovat případné další změny main. Simulované Actions
na Pythonu 3.12 nenahrazují budoucí skutečný Python 3.11 gate ani důkaz přijetí API.
