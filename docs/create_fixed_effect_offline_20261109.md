# Create-effect: úplná syntetická vertikála, produkce stále DENY

**PRODUCTION DENY. Offline integrační BLOCKER create-effect je odstraněn.**
Datum: 9. 11. 2026. Rodič HEAD:
`85545c531c9725661cc4c4346912e393a743fb77`.
Povinný reserve/bind patch byl ověřen bytewise, LF SHA-256:
`f90800a18d0fc7dcd120b99bc89a57d5986520072997646942731a65728448e1`.
Augmentovaná výchozí báze je Git **tree**
`eaadae44baf9ed89c4f4ba53a276c302b38d4c2e`, nikoliv nový commit.
Nový detached worktree: `/workspace/vrchcab-create-effect-85545c5`.

Před vstupním patchem bylo HEAD přesné a git status prázdný. Po aplikaci
patchu byly ověřeny všechny Git blob SHA a 163 souborů augmentované báze.
Původní reserve/bind worktree i sedm starších kopií (osm celkem) zůstávají nedotčeny.
Samostatný integrovaný auditní patch nebyl aplikován. Nevznikl commit/branch,
push, PR, merge, produkční změna ani skutečný API GET/POST.

## Audit před implementací a nezbytný rozsah

Externí `design-before-implementation.json` vznikl před kódem. AdmissionSnapshot
a RecoverySnapshot jsou přesně osmipoložkové, ale mají různá metadata. Prosté
odstranění reservation při čtení by ztratilo autoritu a při CAS ji nemrazilo.
Naopak pouhé spojení do desetipoložkového itemu by nestačilo: starý writer
se skrytými novými poli může podmíněný zápis provést a teprve jeho ACK odmítnout.

Řešení znovu používá stávající reserve/bind/claim/STOP/effect kernel. Nový
controller-only `create_fixed_effect` je jediná cesta rozšíření 8→10. Žádný
effect seed nebyl použit v pozitivních integračních testech. Stará schémata
se automaticky nemigrují, staré decodery novou fyzickou obálku odmítají.

| Soubor oproti augmentované bázi | Přesný důvod změny |
| --- | --- |
| **nový** create_effect_offline.py | Přísný desetipoložkový model, úzký create command, chráněná 10→8 projekce a kompozice původních handlerů |
| **nový** tests/test_create_effect_offline.py | 52 skutečných integračních regresí, stejné SQLite/Dynamo/RSA/ingress/provider fixtures; žádná nová infrastruktura |
| **nový** tento dokument | Kontrakt, GO/NO-GO, skutečné počty a limity |
| dynamodb_cas_offline.py | Privátní schema fence/pin helpery; core writer vyžaduje nepřítomnost známých vyšších metadat; explicitní pk equality navíc k request Key |
| command_gateway_offline.py | Projection nahradí absence command_schema/run_authorization jejich úplnou rovností; původní immutable authorization podmínky zachovány |
| durable_effect_offline.py | Recovery projection nahradí absence recovery_schema/effect úplnými piny; přímý effect writer hlídá nepřítomné admission metadata |
| reserve_bind_offline.py | Admission projection stejně pinuje admission_schema/reservation; přímý reserve/bind writer odmítá recovery metadata při CAS |
| tests/fake_dynamodb_control.py | Původní fake parser nově vyhodnocuje attribute_not_exists; ostatní atomic SQLite chování nezměněno |

Bez těchto malých změn offline writerů by staré schéma nebylo atomicky
odříznuto od nového; nejde o změny produkčních importů nebo write cest.
Původních 764 testů zůstalo zachováno. Žádný starý negativní test nebyl odstraněn
ani přepsán. Workflowy, main/publisher/žurnál, research/worker/budget konfigurace,
pyproject a lockfile jsou bytewise stejné.

## Autorita, jediný item a pevná operace

Po explicitním create má fyzický item právě deset atributů:

```text
pk, incarnation, generation, document,
command_schema, run_authorization, admission_schema, reservation,
recovery_schema, effect
```

Uzavřený desetipoložkový tvar + přesná dvojice `offline-admission-v1` /
`offline-durable-effect-v1` definují nové kombinované schéma. Nezavádí se
jedenáctý version atribut a původní osm atributů nemění své hodnoty, kromě
očekávaného generation+1. CreatedEffectSnapshot validuje obě původní projekce
i jejich totožný command core; jediný effect musí odpovídat pevné definici.

| Hranice důvěry | Modelová autorita | Co zůstává neprokázané |
| --- | --- | --- |
| Bootstrap | Harness seeduje jen ON/epoch/daily quota, bez pending/run/owner/effect | Žádné produkční ON/re-enable/reset |
| Controller a schvalovací evidence | Stejný registrovaný opaque ingress a SyntheticAdmissionAuthority, rozšířený o nezávislé read-only effect approval facts | Název role, bool, Python dataclass nebo stejný proces nejsou skutečná autentizace |
| Identita create | Existující RSA OIDC verifier proti stored run policy, důvěryhodným syntetickým keys a trust generation, attempt=1 | JWT neprokazuje vykonaný kód/prompty/dependencies; artifact provenance není produkční |
| Fyzický item | Externě určené table/pk/incarnation/authority_id a jediný SQLite row | Reálné IAM, routing, restore fence, všichni writeři a cloud atomicita nejsou ověřeny |
| Effect/send | Původní monotónní kernel a NonIdempotentSyntheticProvider | Jeden fake send není exactly-once external effect, provider hard cap nebo skutečný katalog volání |

Read-only registrace schválení nemají ownership, quota ani consumed/replay bit.
Počítadla pouze pozorují. Jediná mutable autorita je tentýž item; žádný druhý
ledger, DB, cache oprávnění, lease, TTL, takeover, refund nebo automatic rollover.

Pevný EffectDefinition odvozuje controller z již rezervovaného a navázaného
`SYNTHETIC-ATOMICITY-ONLY`: intent/epoch, authorization_id, run_id, config digest,
přesný run_reserve USD. Pevná operace má provider
`synthetic://create-effect-provider`, kind `paid_model`, semantic key
`atomicity.one`, payload `SYNTHETIC:one-fixed-operation-v1`, cap `0.10`.
Kind je pouze syntetický štítek; žádný placený provider neexistuje. Musí platit
`0 < cap ≤ run_reserve`; cap nesmí vybírat worker. Canonical definition se
znovu kontroluje při dekódování kombinovaného itemu, nejen v create.

Jde o **jednu izolovanou fake operaci**, nikoliv dokončení plánu dvou fake
Agent requestů. Skutečný WORKER_CONFIG zůstává 2×3, **4 Agent POST > auto cap2**
a nominálně dalších 20 LLM invokací. Skutečná větev nedostane ani rezervaci.
Žádný supervised přepínač nebo změna plánu se nepřidává.

## Create, projekce a stavový automat

Payload create obsahuje pouze `expected_revision`, `intent_id`, `approval_epoch`.
Scope, signature-checked identity, definition a všechny ceny/ID pocházejí z
injektované nezávislé syntetické autority mimo payload. Owner musí být null,
controller ON, reservation a run binding durable, neexpirované a konzistentní.
Před sendem se znovu ověří ingress, clock, run/trust/config/artifact/definition.

CAS porovnává **všech osm** původních atributů včetně úplného document,
reservation a run_authorization, pk/incarnation/generation, konkrétní pending,
epoch/ON/owner-null. Navíc vyžaduje **attribute_not_exists** obou recovery polí.
Jediné SET přidá recovery_schema, pevný NOT_STARTED record a generation+1.
Žádný upsert a žádné jiné přepsání či navýšení denní kvóty/rezervace.
Poslední increment zůstává vyhrazen STOP.

Každá vrstva starého schématu vyžaduje nepřítomnost známých vyšších metadat.
Projekce smí odstranit takovou podmínku **jen když ji nahradí úplnou rovností
daného atributu v témže CAS**. Chybějící fence je chyba. Vnořené 10→8→6→4
projekce tak chrání všech deset atributů; claim neoslabuje immutable binding.
Regrese podvrhne starým 4/6/oběma8 writerům aktuální generaci a skryje nová
pole z GET: jejich zápisy se odmítnou před commitem. ACK decoding sám by nestačil.

```text
unreserved ON -> reserve -> bind -> create NOT_STARTED -> claim
 -> ATTEMPT_COMMITTED -> one local continuation -> <=1 fake send_once
 -> OUTCOME_UNKNOWN -> independent positive proof -> STOP
```

Claim/STOP fungují před i po create. Claim před create úlohu přidělí; následný
create je správně DENY, protože požaduje unowned stav. Po create používá kernel
record ve stejném itemu; STOP vždy zachová evidence, ownera, finance a effect.
Žádná fáze nevrací veřejné execution_allowed=True.

Strong GET / ALL_NEW / HTTP200 / RetryAttempts=0 / strong readback jsou nutné,
nikoliv samostatně postačující. Každý write má jeden send bez automatického
retry. Timeout, konflikt, rozpojení, chybný ACK nebo readback znamená zastavit a
nezávisle rekonciliovat. Readback, restart nebo staré potvrzení nevydá pokračování.
Při neúspěšném advisory UNKNOWN zápisu může zůstat ATTEMPT_COMMITTED; i to je
permanentně spotřebovaný slot s neznámým výsledkem, nikoliv retry oprávnění.

OFF před CAS konflikt vyřeší konzervativně. OFF před final check zabrání fake
sendu; po final check může doběhnout jeden již připuštěný požadavek. Atomický
DB+provider stop neexistuje. Expirace nic neobnovuje a nejsou serverové atomické
hodiny během přenosu; účetní ACK není aktuální ON ani oprávnění k placení.

## Skutečné testy a měření bez resetu

52 nových testů pokrývá celou vertikálu, create/claim/gate souběh, replay/rerun,
JWT/source/workflow/repo/owner/config/artifact/trust/plan mutation, expiraci a
revokaci, všechny původní atributy bez generation incrementu, stale GET,
timeout před commitem, lost ACK, crash před/po efektu a nový subprocess,
incomplete ALL_NEW/retry metadata, readback/OFF závody, cap/headroom, staré
schéma/writery, neznámý devátý/jedenáctý atribut a raw writer/restore bypass.
Přesná test ID jsou v JSON protokolech, nikoliv odhadnutý počet testů.

DB requests jsou GetItem/UpdateItem na syntetickém broker rozhraní. Commity
měří původní MeteredSyntheticDynamo. ACK měří diagnostické wrappers skutečných
potvrzených write výsledků, včetně advisory UNKNOWN/proof, ne kladných GET.
Continuation měří původní gate_confirmed hook. Entry/wire/effect počítá přímo
původní fake provider. Nic z toho nepřiděluje autoritu. SQLite inspekce a
privilegované admin_replace fault injection nejsou broker API; jejich zásahy
jsou u protipříkladů uvedeny zvlášť, nejsou skrytě započteny jako broker commity.

| Jeden průchod, žádný reset | DB requests | Commity | Potvrzené ACK | Continuations | Entries | Fake wire | Fake účinky |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Po reserve | 4 | 1 | 1 | 0 | 0 | 0 | 0 |
| Po bind | 8 | 2 | 2 | 0 | 0 | 0 | 0 |
| Po create | 11 | 3 | 3 | 0 | 0 | 0 | 0 |
| Po claim | 17 | 4 | 4 | 0 | 0 | 0 | 0 |
| Po jediném fake sendu a UNKNOWN | 24 | 6 | 6 | 1 | 1 | 1 | 1 |
| Po nezávislém pozitivním proof | 28 | 7 | 7 | 1 | 1 | 1 | 1 |
| Po STOP | **34** | **8** | **8** | **1** | **1** | **1** | **1** |

Ve stejném testu generation1→9, attempts0→1, conservative quota0.70 zůstává
po bind/create/claim/send/proof/STOP stejná; žádný refund. Jedna fake cena0.10
je účetní fixture, nikoliv skutečná provider sazba nebo spend cap.

| Samostatný scénář od bootstrapu | DB | Commity | ACK | Continuations | Entries | Wire | Účinky |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Reálné 4>2 odmítnutí | 2 | 0 | 0 | 0 | 0 | 0 | 0 |
| Create commit/lost ACK | 10 | 3 | 2 | 0 | 0 | 0 | 0 |
| Gate commit/lost ACK před restartem | 20 | 5 | 4 | 0 | 0 | 0 | 0 |
| Tentýž stav po restartu a opakovaném pokusu | 22 | 5 | 4 | 0 | 0 | 0 | 0 |
| Dva poctiví gate kandidáti | 27 | 6 | 6 | 1 | 1 | 1 | 1 |

### Nevyřešené negativní protipříklady TCB

| Porušený předpoklad | DB | Commity | ACK | Continuations | Entries | Wire | Účinky |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Skryté DB retry po create commitu | 11 | 3 | 2 | 0 | 0 | 0 | 0 |
| Přehraný create ACK dvěma kandidátům | 13 | 3 | 4 | 0 | 0 | 0 | 0 |
| Přehraný gate ACK dvěma kandidátům | 29 | 6 | 7 | 2 | 2 | 2 | 2 |
| Skryté provider retry | 26 | 6 | 6 | 1 | 1 | 2 | 2 |
| Privilegovaný rollback stejné incarnation | 37 | 9 | 9 | 2 | 2 | 2 | 2 |

Rollback řádek navíc obsahuje **jeden explicitní admin restore** mimo broker
počítadla. Je to porušení TCB, ne navržená recovery. Chránící nová incarnation
odmítne starý item, ale skutečná kontrola restore/cutover nebyla ověřena.
Přehrávaný transport demonstruje, proč ACK/readback samy nestačí: může vzniknout
druhé pokračování i účinek bez druhého gate commitu. Poctivý single-send
transport a neobnovená autorita jsou nutné předpoklady at-most-once admission.

Známé schema fences hlídají nová známá metadata; nepřítomnost libovolného
neznámého dalšího atributu se nevyjadřuje. Privilegované přidání devátého nebo
jedenáctého atributu může dovolit commit, ale úplný decoder odmítne ACK a
nevydá continuation. Ochrana všech writerů a uzavřeného schématu je součást
TCB, nikoliv dokázaná vlastnost IAM. Raw UpdateItem privilegovaného writera
klientské kontroly nevynutí.

## Nové místní výsledky a GO/NO-GO

| Skutečný běh | Passed | Failures/errors/skips | Unexpected network |
| --- | ---: | --- | ---: |
| Plný nezměněný tests.run --strict-known-bugs | **816/816** =764 původních +52 nových | 0/0/0, také 0 known failures/unexpected successes | 0 včetně outer audit |
| Focused nových testů, překryv plného běhu | **52/52** | 0/0/0 | 0 |
| Tools samostatně | **19/19** | 0/0/0 | 0 |
| Botocore lab samostatně | **6/6** | 0/0/0 | 0, synthetic HTTP/pool |

Python3.12.14, ověřeno všech112 použitelných main pinů poetry.lock:
PyJWT2.10.1, cryptography45.0.4, forecasting-tools0.2.92, requests2.32.5,
openai2.13.0. SDK venv zvlášť botocore1.43.126/urllib3 2.8.0/jmespath1.1.0/
python-dateutil2.9.0.post0. Žádná instalace nebo resolution ze sítě.
python3.11 nebyl v PATH dostupný; provedena jen grammar kontrola, ne runtime test.
Síťové guardy zůstaly aktivní; potlačena jen lokální urllib3 IPv6 capability
probe před SDK importy. Skutečné IPv6/TLS nebo provider cloud nejsou ověřeny.

SDK lab zahrnuje změněný core engine a skutečný botocore serializer/retry vrstvy
pouze proti fake HTTP/pool; není validací nového desetipoložkového requestu na
AWS. Předchozích764 byl místní výsledek; historické GitHub CI717/717 je údaj
zadání, zde nově neověřený. Focused52 je podmnožina816, tools/SDK se nesčítají
do GitHub CI. Manifest uchovává skutečné protokoly a úplná test ID.

| Stupeň | Verdikt | Zbývající hranice |
| --- | --- | --- |
| Izolovaný reserve→bind→create→claim→fake effect→proof→STOP | **GO pouze synteticky** | Jedna pevná operace, stejná test autorita, podmínky poctivého transportu/TCB |
| Skutečný automatický worker 4>2 | **NO-GO** | Admission odmítne před rezervací; plán ani limit se nemění |
| Dokončený plán dvou fake Agent nebo skutečných4+20 volání | **NO-GO** | Neexistuje reálný schválený katalog operací/finančních gate |
| Produkční CAS/identity/artifact/restore/cutover | **NEPROKÁZÁNO / NO-GO** | Skutečná ingress, deployment/IAM, keys, provenance, restore fencing, writer restrictions a nezávislá revize |
| Credential GET, cloud test, dispatch, paid pilot/publikování | **NO-GO v tomto zadání** | Nový konkrétní výslovný souhlas a uzavření relevantních P0 |

Všechny None kotvy a execution DENY zůstávají. Legacy ruční publisher je
oddělený a tento offline model ho nechrání. Náklady, skutečná identita,
linearizovatelnost AWS ani rollback protection nejsou prohlášeny za prokázané.

Výstupní patch je **delta proti 85545c5 + ověřenému reserve/bind patchu**.
ZIP obsahuje i přesný prerequisite patch, manifest, apply/reverse/tree/blob
ověření a source git status. HEAD zůstává85545c5; tři soubory prerequisite jsou
bez commitu, proto samotný git status ukazuje také tyto očekávané untracked
cesty vůči historickému HEAD. Nová delta mění5 offline souborů a přidává3.
Žádný zápis na GitHub nebo produkční operace nebyly provedeny.
**Výsledný provozní verdikt: PRODUCTION DENY.**
