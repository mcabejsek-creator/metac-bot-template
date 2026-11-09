# Integrace offline V3 po PR #41 – PRODUCTION DENY

**Offline integrace dokončena; produkční nasazení je stále NO-GO.**
Datum: 9. 11. 2026, Europe/Prague. Přesný rodič HEAD:
`a6db86d1194d26db312ff654e14fefe06bbf0e3f`.
Jeho tree je `e0eb70b3d1978a9097b8ee766519e1b074008882`.
Nový čistý detached worktree: `/workspace/vrchcab-v3-integration-a6db86d`.

## Původ vstupů, pořadí a PR #41

Oba původní patch soubory byly lokálně dostupné v /workspace/shared/downloads.
Před aplikací byla ověřena SHA-256 přesných bytes, bez regenerování obsahu:

| Vstup | Bytes | SHA-256 |
| --- | ---: | --- |
| metaculus-futureeval-v3-reserve-bind-85545c5.patch | 87922 | f90800a18d0fc7dcd120b99bc89a57d5986520072997646942731a65728448e1 |
| metaculus-futureeval-create-effect-85545c5.patch | 91624 | 830dc3c509307240046bc33601991f5e0f254896e6287b3c1c92040822fc2b8a |

Commit a6db86d nebyl v předchozích lokálních kopiích. Byl získán jedním
read-only Git fetch přesného SHA z repozitáře, nikoliv REST/API nebo workflow
operací. Ověřen commit object, HEAD, prázdný počáteční status a merge rodiče
85545c5 / 8195cbd; commit zpráva identifikuje merge PR #41.

PR #41 mění 15 workflowů, tests/test_github_workflows.py a svůj auditní dokument.
Žádná tato cesta se nekříží se vstupními V3 patchi. Ověřená posloupnost:

1. reserve/bind `git apply --check`: úspěch.
2. create-effect samotný před reserve/bind: očekávané odmítnutí
   `reserve_bind_offline.py: No such file or directory`. Check nic nezapsal.
3. Skutečně aplikován reserve/bind; následný create-effect check i aplikace
   prošly bez konfliktu, offset oprav nebo ruční úpravy kódu.

Všech deset výsledných cest z původních patchů je bytewise shodných s jejich
dříve ověřenou kompozicí. Nově vzniká jen tento integrační dokument. Původní
dokumenty V3 zůstávají historickými zprávami pro 85545c5: jejich stará SHA,
worktree cesty a počty testů nejsou výsledkem této integrace.

## Rozsah výsledného integračního patchu

Proti a6db86d se mění čtyři existující offline soubory:

- command_gateway_offline.py
- durable_effect_offline.py
- dynamodb_cas_offline.py
- tests/fake_dynamodb_control.py

Přidává se sedm cest:

- reserve_bind_offline.py
- create_effect_offline.py
- tests/test_reserve_bind_offline.py
- tests/test_create_effect_offline.py
- docs/v3_reserve_bind_offline_20261109.md
- docs/create_fixed_effect_offline_20261109.md
- docs/v3_integration_after_pr41_20261109.md

**Všech 15 workflowů je byteově shodných s rodičem.** Zachováno všech 49
připnutých uses včetně SHA checkout11d5960…, setup-python a26af69…,
upload-artifact ea165f8… a install-poetry a783c32…. Nemění se ani jejich
komentáře, permissions, triggers, paths, credentials bindings či nastavení.
PR #41 testy i audit zůstávají beze změny. Main/CLI, publisher/žurnál,
scheduler, výzkumný plán, limity a závislosti jsou nezměněné.

## Bezpečnostní audit rozdílu

Stejný autoritativní syntetický item obsahuje rezervaci, finance, quota,
approval epoch, run authorization, owner a effect record. Reserve a bind
zůstávají účetními potvrzeními. Create přidává jediný pevný NOT_STARTED slot;
claim, STOP a effect kernel používají whole-item podmínky a schema fences.
Generace se monotónně zvyšuje s headroom pro STOP, bez lease/reset/takeover.
Nezavádí se druhý ledger, endpoint, produkční factory nebo nový runtime hook.

CONTROLLER_STORE, CONTROLLER_VERIFIER, PRODUCTION_COMMAND_GATEWAY a
PRODUCTION_EFFECT_RECOVERY jsou stále **None**. Veřejné execution_allowed,
retry_allowed a external_retry_allowed zachovávají False. Nové offline moduly
neimportují main, requests/urllib/socket, cloud SDK ani publisher.
WORKER_CONFIG zůstává **2 reporty × 3 prognózy, 4 Agent POST > auto limit2**;
skutečné admission je odmítnuto před rezervací. Jediná fake operace není
katalog skutečných4 Agent +20 dalších LLM invokací ani dokončený fake plán.

| Závažnost / hranice | Stav a důkaz | Co zbývá |
| --- | --- | --- |
| P0-01 reálný plán a finance | 4>2 zůstává DENY, test bez rezervace | Skutečný katalog paid operací, provider hard caps/billing; neřešit změnou plánu nebo limitu |
| P0-02 ingress/OIDC/artifact | Syntetické klíče/autorita a úplné scope kontroly; produkční trust None | Skutečná serverová autentizace a důkaz vykonaného kódu/prompts/config/dependencies; samotný JWT/dataclass/flag nestačí |
| P0-03 backend a writeři | SQLite CAS a chráněné projekce otestovány | Reálné IAM/routing, atomické vlastnosti backendu, všichni writeři a externí incarnation/restore fence nejsou prokázány |
| P0-04 transport | Honest single-send/unique conditional ACK fungují v modelu | Replay/hidden retry mají zachované protipříklady se dvěma účinky; HTTP200/readback nejsou důkaz exactly-once |
| P0-05 legacy placení/publikování/OFF | Legacy runtime se zde nemění ani nespouští | Je mimo V3; None kotvy nejsou globální vypínač main.py, chybí bezpečný cutover a nezávislé živé settlement |
| P1-01 runtime/CI/settings evidence | Lokálně Python3.12, workflow SHA beze změny | Python3.11 runner, efektivní Actions allowlist a Settings se živě neověřovaly |
| P1-02 historické podklady | Původní zprávy/hash vstupů zachovány | Staré počty764/816 nepřebírat jako nové testování; platí výsledky níže |
| P1-03 neznámé schema atributy | Přesný ACK decoder fail-closed | Rovnosti známých polí neprokazují nepřítomnost libovolného dalšího atributu; strict writer schema je TCB pod P0-03 |
| P1-04 budoucí CI selection | Tento patch odpovídá čtyřem existujícím core paths a workflow volá plnou discovery | Čistě budoucí změny nových V3 modulů/testů nemusí filtrovaný PR workflow spustit; návrh samostatného doplnění níže |

P0 položky jsou produkční hard gates, nikoliv tvrzení, že tato čistě offline
integrace změnila dosavadní trust kontrakt nebo nově smí spouštět worker.
Known negative counterexamples zůstaly byteově zachované, nebyly přepsány na GO.

## Nové skutečné místní testy

| Běh | Passed | Failures/errors/skips | Unexpected network |
| --- | ---: | --- | ---: |
| Plný nezměněný tests.run --strict-known-bugs | **817/817 =718 main +47 reserve/bind +52 create-effect** | 0/0/0 | 0, také outer audit |
| Focused V3, překryv plného běhu | **99/99 =47+52** | 0/0/0 | 0 |
| Tools samostatně | **19/19** | 0/0/0 | 0 |
| Synthetic botocore lab samostatně | **6/6** | 0/0/0 | 0, fake HTTP/pool |

Také 0 known failures a 0 unexpected successes. Všech 718 main test ID je
zachováno; dalších99 ID přesně odpovídá dvěma V3 sadám. Focused99 se
nepřičítá znovu k817, tools/SDK nejsou GitHub CI. Nový živý CI běh se nespouštěl
ani nenačítal. Starší GitHub výsledky nejsou prohlášeny za nové ověření.

Python3.12.14; všech112 použitelných main pinů ověřeno proti stejnému
poetry.lock, včetně PyJWT2.10.1, cryptography45.0.4, forecasting-tools0.2.92,
requests2.32.5 a openai2.13.0. SDK venv zvlášť botocore1.43.126,
urllib3 2.8.0, jmespath1.1.0, python-dateutil2.9.0.post0. Žádná instalace.
Python3.11 nebyl v PATH, jeho runtime se netestoval. Outer guard doplňuje
nezměněný tests.run; jen lokální urllib3 IPv6 capability probe potlačena.
Skutečné IPv6/TLS, AWS nebo nová desetipoložková operace v cloudu nejsou ověřeny.

V3 stopa začíná nerezervovaným bootstrapem a skutečně provádí oba CAS,
create-effect, claim, latch, fake send, UNKNOWN, nezávislý syntetický proof a STOP.
Neobsahuje effect seed ani reset počítadel během celé vertikály.

| Celá stopa od bootstrapu | DB requests | Commity | Potvrzené ACK | Continuations | Entries | Fake wire | Fake účinky |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Pozitivní reserve→bind→create→claim→effect→proof→STOP | 34 | 8 | 8 | 1 | 1 | 1 | 1 |
| Reálný plán4>2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 |
| Neplatný provider se skrytým retry | 26 | 6 | 6 | 1 | 1 | 2 | 2 |
| Neplatný přehrávaný gate ACK | 29 | 6 | 7 | 2 | 2 | 2 | 2 |
| Privilegovaný rollback stejné incarnation | 37 | 9 | 9 | 2 | 2 | 2 | 2 |

Rollback řádek navíc obsahuje jeden explicitní admin restore mimo broker
počítadla. Poctivý single-send transport/neobnovená autorita jsou předpoklady
at-most-once admission; exactly-once externí efekt se netvrdí. Ztracený ACK,
restart, stale GET a OFF zachovávají permanentně spotřebovaný slot. OFF po
finálním checku může dovolit doběhnout jeden připuštěný fake request.
UNKNOWN nebo nejasný billing nic nerefunduje a nepovoluje automatický retry.

## CI návrh a předání

P1-04: samostatně navržené doplnění paths v offline_bounded_pagination.yaml:
reserve_bind_offline.py, create_effect_offline.py,
tests/test_reserve_bind_offline.py, tests/test_create_effect_offline.py,
docs/v3_reserve_bind_offline_20261109.md, docs/create_fixed_effect_offline_20261109.md
a tento integrační dokument. Tato integrace žádný workflow nemění.
Aktuální změna zasahuje command_gateway/durable_effect/dynamodb_cas/fake_dynamodb,
které už ve filtru jsou; plný runner nové testy objeví automaticky.

Výsledkem je **jeden samostatně aplikovatelný integrační patch přímo proti
a6db86d**, obsahující oba vstupy i tento dokument. Neaplikovat před něj znovu
původní patche. Manifest/protokoly uchovávají vstupní hashe, přesné cesty,
výsledky testů a apply/reverse/tree/file-hash ověření. Testy běžely na source
checkoutu; aplikovaná validační kopie se porovnává bytewise, bez opakovaného běhu.
Všech deset předchozích pracovních kopií je zachováno. Žádný commit, push, PR,
merge, Settings write, živá služba, placení nebo publikování nebylo provedeno.
**Ověřeno offline; pro produkci zůstávají všechny P0 a PRODUCTION DENY.**
