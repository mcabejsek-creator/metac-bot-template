# Izolovaný offline CAS claim rezervovaného intentu

Základ: `73af35d18c1fe09b8fa31248685fe425dbe55e15` po PR #35.
Nový čistý detached worktree: `/workspace/vrchcab-worker-claim-73af35d`.
Chybějící commit byl získán jedním read-only Git fetchem do samostatné
Git databáze. Staré pracovní kopie ani jejich index, HEAD a změny se nemění.

**Jde pouze o offline model budoucího schématu V3. Produkční V2, jeho store,
handoff a všechny cesty skutečného zpracování zůstávají beze změny.**
Úspěšný syntetický claim nikdy nepovoluje worker, výzkum ani publikování.
Skutečný backend, produkční OIDC důvěra a identita vykonávaného kódu nejsou
ověřeny. Pro produkční spuštění zůstává výsledek vždy DENY.

## Výchozí architektura a hranice změny

- `dispatch_intent_controller.py` používá `ControllerState` V2. Rezervace
  zapisuje pending intent, denní počet a konzervativní nákladovou rezervu
  stejným CAS. Již rezervovaná úloha nesmí být znovu rezervována při claimu.
- `autonomous_orchestrator.py` opakovaně čte aktivaci a intent, kontroluje
  zdroje, kvótu a náklady. Handoff vždy odmítá skutečné spuštění.
- `github_control_store.py` je neaktivní Contents adapter V2. Nezakládá
  branch a není připojen k tomuto prototypu. Jeho blob SHA není důkazem
  nerecyklované generační revize při návratu stejného obsahu (ABA).
- `worker_identity.py` ověřuje podepsaný Actions kontext proti nezávisle
  určeným klíčům a politice. Produkční `CONTROLLER_VERIFIER` je stále `None`.
  OIDC podpis nepotvrzuje skutečně vykonávaný kód a konfiguraci procesu.
- `worker_configuration.py` a `perplexity_run_budget.py` zachovávají dva
  reporty, tři prognózy na report a dvě Agent fáze na report: **4 POST**
  proti automatickému limitu **2**. Skutečný plán zůstává zablokovaný.

Přidány jsou pouze `worker_claim_offline.py`, jeho syntetické testy a tento
dokument. Žádný existující sledovaný soubor se nemění. Není přidán nový
production entrypoint, scheduler, workflow, credential ani síťový transport.

## Jediný autoritativní dokument V3

`OfflineControlStateV3` serializuje celý budoucí control dokument: všechny
dosavadní položky V2, `version=3` a povinnou položku `worker_claim`, která je
buď `null`, nebo jeden neměnný záznam vlastníka. Python atribut `control`
pouze znovu používá stávající validátor V2 v paměti; není druhým uloženým
stavem. Serializované položky aktivace, epochy, intentu, kvóty i claimu
jsou v jediném dokumentu a mění se jedním atomickým zápisem.

Claim obsahuje intent ID, approval epoch, request ID, čas přidělení a
ověřený Actions kontext: repository/owner ID, source SHA, workflow ref/SHA,
run ID a první attempt. Raw JWT, veřejné klíče ani secrets se do něj neukládají.
Request ID je 32hex korelační nonce volajícího, nikoliv důkaz identity.

Validátor odmítá jiné verze, neznámé a duplicitní JSON položky, nesprávné
typy, neplatného vlastníka, nesoulad s pending intentem nebo epochou a čas
claimu před rezervací. V3 codec nepřijímá uloženou V2 a V2 codec odmítá V3.
Není aktivní ani implicitní migrace.

## Protokol převzetí

`claim_reserved_intent_offline` přijímá pouze identifikaci požadavku,
podepsaný token a controllerem dodaný čas. Neumožňuje argument `store`,
politiku, klíče, owner objekt, source SHA nebo `verified=True`.
Používá controllerem nakonfigurovaný `CONTROLLER_STORE`; výchozí hodnota
je `None` a neexistuje loader ani produkční binding.

1. Načte celý V3 dokument a neprůhlednou revizi z autoritativního store.
   Chybějící store, revize, neplatné schéma nebo nečitelný stav znamenají DENY.
2. Vyžaduje zapnutý controller, přesnou epochu a přesný již rezervovaný
   intent. Existující claim vždy odmítne, včetně stejného request ID/běhu.
3. Pouze pro první přidělení vyžaduje čerstvou rezervaci v dosavadním
   pětiminutovém okně a stejný UTC den kvóty. To není lease již přiděleného
   vlastníka: jeho nárok nikdy automaticky nevyprší.
4. Po čtení vyhodnotí controllerem dodaný nezávislý čas a skutečně ověří
   token přes existující `verify_controller_worker_token`. Nepřijímá
   neověřené claims, flag nebo lokální GitHub metadata jako důkaz.
5. Navrhne přesně jednu změnu `worker_claim: null → owner`. Původní
   aktivace, epochy, rezervace, počet pokusů a částka se kopírují beze změny.
6. Provede nejvýše **jeden** `compare_and_swap(revision, celý_nový_dokument)`.
   Přesný bool `True` musí dle kontraktu znamenat potvrzený durable commit.
   Konflikt, jiný výsledek nebo výjimka znamenají zastavení a rekonciliaci.
7. Po potvrzení znovu načte autoritativní stav. Musí přesně odpovídat návrhu
   a revize se musí změnit. Nedostupný či změněný readback, nejasný vlastník
   nebo OFF znamenají odmítnutí bez opravného zápisu.
8. Jen tento případ vrátí `claim_confirmed=True`. Vlastnost
   **`execution_allowed` vždy vrací `False`**, včetně úspěšného claimu.

Claim nevolá rezervaci intentu, forecastovací kód, provider, publisher ani
workflow dispatch. Nemá callback ke spuštění workeru. Výsledkový objekt je
účetní pozorování syntetického zápisu, nikoliv přenositelné oprávnění.

## Výhradnost a opakování

Za předpokladu autoritativního linearizovatelného CAS a nerecyklovaných
revizí mohou dva kandidáti číst stejný stav bez vlastníka s revizí R.
První úspěšný zápis uloží vlastníka a posune revizi. Druhý CAS proti R
selže; nové čtení již vidí vlastníka a nepřipustí další přidělení.
Čtení aktivace a intentu i zápis vlastníka se tedy vztahují ke stejnému
dokumentu; samostatný claim ledger by tuto vlastnost neměl.

Stejný token, request ID, nový request ID ani restart nevrátí další kladné
potvrzení. Po úspěšném, ale nepotvrzeném zápisu zůstane vlastník uložený
a další pokus je blokovaný. Neimplementuje se release, lease, heartbeat,
automatické převzetí, vymazání pending intentu nebo vrácení rozpočtu.

Po timeoutu či konfliktu se **zápis automaticky neopakuje**. Opětovné
načtení je nutné, ale není nezávislou rekonciliací. Ani zjištění prázdného
claimu není oprávněním k retry: zápis mohl selhat před commitem, být zpožděný
nebo přetrvává chyba backendu. Prototyp nemá retry/recovery smyčku ani
rekonciliátor, který by novou operaci schválil. Budoucí externí orchestrátor
musí po nejasném výsledku zastavit a vyžádat nezávislou rekonciliaci; modul
neprokazuje správnost cizího restart/retry orchestrátoru. Bez spolehlivého
backendu nelze absenci zápisu nebo bezpečnost nového pokusu tvrdit.

Toto je výhradní přidělení záznamu, nikoliv důkaz exactly-once vykonání.
Kód ovládající stejný Python proces může podvrhnout libovolné objekty;
`frozen=True` ani strukturální store protokol nenahrazují původ důvěry.
Zápis schopného store či ověřovače v testech je explicitní syntetická
dependency injection, nikdy produkční provisioning.

## Nouzové zastavení

`persist_offline_emergency_stop(operator_stop_authorized=True)` přečte
stejný dokument, provede jediný CAS na OFF a ověří celý readback.
Vlastník, pending intent, kvóta a rezervované náklady zůstávají zachované.
Je-li již OFF, neprovádí se další zápis; trvalý OFF se ověřuje čtením.
Při konfliktu, timeoutu nebo neověřitelném stavu se úspěch zastavení netvrdí.

OFF před claimem blokuje převzetí. Souběh OFF se zápisem claimu rozhodne
stejný autoritativní CAS. OFF mezi commitem claimu a readbackem znemožní
kladné potvrzení. OFF po potvrzení se uloží se zachovaným vlastníkem;
ani pozdější ruční ON nesmí vlastníka přidělit znovu. Automatický ON zde
neexistuje. Poškozený stav se nepřepisuje naslepo, zastavení vyžaduje
nezávislou opravu. Rušení již běžící skutečné práce zůstává mimo prototyp;
zde se žádná taková práce nespouští.

## Co je ověřeno pouze synteticky a co zůstává neověřené

Testovací `SQLiteControlStore` je pouze v testovém souboru. Ukládá celý V3
dokument a generační čítač v **jediném řádku** nové dočasné databáze.
Podmíněný SQL UPDATE atomicky porovnává revizi a současně mění dokument
i generaci, commit předchází odpovědi. Revize je pro klienta opaque;
jen backend ji odvozuje z rostoucího čítače, který se nevrací zpět.
Kontrola integer typu zastaví i přetečení místo recyklace revize.
Čítač diagnostických volání a Python lock nejsou autoritou pro admission.
Souběžní kandidáti používají nezávislá SQLite spojení; restart je navíc
ověřen samostatným Python procesem nad stejnou dočasnou databází.

Skutečné GitHub Contents CAS, durable commit/readback, ochrana branch,
cache konzistence, oprávnění všech writerů a bezpečnost jejich přechodů
**nebyly ověřeny**. Pouhé porovnání content blob SHA neprokazuje zachycení
OFF→ON se stejným obsahem. Budoucí backend musí doložit nerecyklované revize
celého stavu, atomickou konkurenci a zachování vlastníka všemi writery.
Nemůže povolit vymazání či přepsání vlastníka pro stejný pending intent.
Testy neprokazují odolnost vůči administrátorovi, který tento kontrakt obchází.

Produkční OIDC klíče/politika, jejich provenance/rotace a nezávislá vazba
očekávaného run ID na aktuální intent stále chybějí. Test soutěží dvou
kandidátů ve stejném schváleném Actions kontextu; jejich request ID pouze
odlišují CAS operace. OIDC není identitou unikátního vykonávajícího procesu,
ochranou před sdíleným/odcizeným tokenem nebo attestation kódu a konfigurace.
Proto ani vyřešená výhradnost syntetického záznamu neotevírá skutečný worker.
Stále chybí nezávislé per-request kill-switch, billing a publikační podmínky.

## V2 → V3: pouze návrh budoucího postupu

1. Samostatně schválit návrh schématu, backendu a všech writerů. Zastavit
   controller a externí workery; autoritativně ověřit OFF, přesnou V2 revizi
   a výsledek dřívějších pokusů. Nejasný pending intent nesmí být považován
   za nepřevzatý jen proto, že V2 neměla položku claimu.
2. Prokázat, že neexistuje nepotvrzený vlastník nebo nevypořádané vykonání.
   Pokud to nelze, ponechat OFF a stav nezměněný do nezávislé rekonciliace.
3. Pouze jedním samostatně schváleným CAS stejného úplného control dokumentu
   změnit verzi na 3 a vložit explicitní prázdný claim, zachovat všechny
   dosavadní hodnoty. Ověřit durable readback a ponechat OFF.
4. Zajistit, že starší V2 writer V3 odmítne a nikdy neodstraní nový claim.
   Doložit CAS/ABA vlastnosti a všechny další přechody včetně zastavení.
   Aktivace by byla až další samostatně autorizovaný krok se všemi dosud
   chybějícími identitními, finančními a runtime podmínkami.
5. Rollback nesmí odhodit vlastníka nebo nejasný výsledek při převodu zpět
   na V2. Automatický downgrade či rekonciliace nejsou bezpečným fallbackem.

Žádný migrační skript, live V3 adapter, branch nebo aktivace zde nevznikly.

## Aktuální lokální ověření

Python 3.12.14; nové i dostupné původní testy běžely pod auditním zákazem
sítě, bez stahování/instalace závislostí. Nová sada má **29 testů**:
souběh, opakování, restart v novém procesu, konflikty, změnu intentu,
timeout před/po commitu, ztracenou odpověď, přerušení po commitu, nejasného
vlastníka, readback, OFF před/po claimu, OFF→ON ABA, identitu, žádné lease,
žádnou další rezervaci a trvalý zákaz spuštění včetně původního handoff.

| Kontrola | Výsledek |
| --- | --- |
| Nový claim prototyp | **29/29 prošlo** |
| Původní autonomní runtime | **37/37 prošlo** |
| Původní OIDC sada | **20/20 prošlo průzkumně** |
| Původní `tools/test_*.py` | **19/19 prošlo** |
| Společný aktuální běh | **105 prošlo; 0 failures/errors/skips a síťových pokusů** |
| Plný existující `tests.run --strict-known-bugs` | **před discovery blokován: chybí `requests`; 0 testů** |
| Runtime závislosti crypto části | PyJWT **2.13.0**, cryptography **50.0.0** |
| Zamčené crypto verze | PyJWT **2.10.1**, cryptography **45.0.4** zde nejsou dostupné; **kompatibilita neověřena** |

Chybí také `forecasting-tools` a `openai`. Nejde o plnou SDK regresi ani
o ověření backendu. Samostatná gramatika Pythonu 3.11 je jen statická
kontrola, nikoliv spuštění jeho interpreterem. Dřívější historické výsledky
nejsou použity jako potvrzení této změny.

```bash
# Z nového worktree, bez sítě:
python -B tests/test_worker_claim_offline.py
python -B tests/test_autonomous_runtime_offline.py
python -B tests/test_worker_identity_offline.py
# Plná sada vyžaduje už připravené SDK závislosti:
python -B -m tests.run --strict-known-bugs
```

Nová sada patří i do standardního discovery. Pro společný běh bez chybějící
SDK inicializace je v externích artefaktech `run_offline_checks.py`; blokuje
síť před importy, sanitizuje credential proměnné pouze v procesu testů,
kontroluje i zachycené síťové pokusy a ukládá strojový výsledek. Potomek pro
restart má vlastní auditní zákaz sítě a dědí pouze sanitizované prostředí.

Jediný kompletní patch proti výchozímu commitu, SHA-256 patchu a manifest
hashů souborů se ukládají mimo worktree do sdílených souborů ke stažení.
Kontroluje se čistá aplikace, výsledný Git strom, reverzní aplikace a testy
z aplikované kopie. Konkrétní hashe a protokoly jsou externí, aby dokument
neobsahoval rekurzivní hash sebe sama. Výsledek je určen k nezávislé revizi.

Neproběhl živý API požadavek, GitHub zápis, změna workflow/oprávnění,
publisheru, žurnálu, scheduleru, secrets ani skutečného control branche.
Neproběhl placený výzkum, publikování, dispatch, push, PR nebo merge.
