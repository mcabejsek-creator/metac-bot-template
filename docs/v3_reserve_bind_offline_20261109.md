# V3 reserve_intent a bind_approved_run: izolovaný offline kontrakt

**PRODUCTION DENY. Offline reserve → bind je implementován a otestován.**
Základ: `85545c531c9725661cc4c4346912e393a743fb77` (PR #40).
Datum: 9. 11. 2026. Nový detached worktree:
`/workspace/vrchcab-reserve-bind-85545c5`. Žádný commit, push, PR ani merge.

Pozitivní test začíná synteticky schváleným ON/epoch/quota bootstrapem **bez
pending, ownera, rezervace i run authorization**. Oba nové přechody skutečně
projdou stejnou autoritou a jedním SQLite itemem. Účetní ACK není dispatch,
execution grant, spend cap ani důkaz vykonaného artifactu.

Celá vertikála k effectu má nadále **BLOCKER**: stávající RecoverySnapshot
vyžaduje od konstrukce již navázaný run a pevný nenulový effect. Neexistuje
schválený create-effect přechod z tohoto počátečního stavu. Ten zde nevzniká,
effect se neseeduje a nepředstírá se celý reserve→effect end-to-end. Samostatný
test skutečně propojuje nové reserve/bind se **stávajícím claim a STOP**.

## Návrhový audit před implementací

Před prvním zásahem do kódu byl uložen externí
`design-before-implementation.json`. Lokální integrovaný audit byl dostupný;
jeho původní patch má požadovaný SHA-256
`3a5ef247275c79f21a5118539c0405f8f3ed9025f0bf4d565ee132a4d31091ae`.
Auditní patch nebyl aplikován na tento checkout, jeho zdrojová kopie se nemění.

| Existující komponenta | Kontrakt a rozhodnutí |
| --- | --- |
| V2 controller | read→(ControllerState, revision), bool CAS. Použity nezměněné build_dispatch_snapshot a propose_reservation, nikoliv záměna store objektů |
| OfflineControlStateV3 | Vložený V2 control, navíc nullable worker_claim; schéma nezměněno |
| DynamoConditionalStorePrototype | Snapshot/WriteObservation, pouze claim/OFF. Beze změny; nevydává oprávnění pro rezervaci |
| CommandSnapshotDraft | Core + nullable RunAuthorizationDraft; claim transport zmrazuje celé authorization. Beze změny |
| Nový AdmissionSnapshot | Explicitní offline-admission-v1 obálka existujícího command core + immutable reservation record v TOMTÉŽ itemu; žádná automatická migrace |
| SQLite fake | Původní SyntheticDynamoTransport + MeteredSyntheticDynamo. Atomický UPDATE s očekávanou revizí, samostatné connection, deterministické barriers/fault hooks; žádná nová DB infrastruktura |

Jediná nová implementace je `reserve_bind_offline.py`. Úzký OfflineAdmissionGateway
nemá endpoint, cloud SDK, credentials ani produkční factory/import hook.
Vnější caller může poslat jen schématovaný příkaz, ne state/owner/plan/policy,
SQL/Dynamo expressions, table/tenant, generaci, role nebo verified flag.
Původní gateway stále reserve/bind nepodporuje; jeho negativní testy zůstaly
beze změny. Nový handler je obsluhuje pouze nad novou explicitní obálkou.

## Threat model a zdroj důvěry

| Hranice | Co ji v testu představuje | Co z toho NEPLYNE |
| --- | --- | --- |
| Bootstrap | Privilegovaný harness seeduje jen aktivaci, epochu a počáteční daily quota | Není implementované ON, re-enable, reset nebo produkční schválení |
| Controller ingress | Existující SyntheticCommandAuthentication registruje opaque handle mimo payload; jen registrovaný controller může reserve/bind | Jméno role, bool nebo Python dataclass nejsou autentizace. Stejný proces není sandbox proti útočníkovi s přístupem k objektům |
| Preflight evidence | Samostatně registrovaná SyntheticReservationEvidence pro přesnou revizi; server znovu ověří před jediným sendem | Datové flags samy nic neověřují. Registry jsou autorizační fixture, ne účetní/owner/replay ledger; nemají consumed bit |
| Code/config scope | Immutable synthetic source/workflow/repo/owner scope + artifact/config digest v reservation record | Nejde o produkční provenance promptů, dependencies ani skutečně vykonaného kódu |
| Run approval | Nezávislé registration facts a RS256 JWT ověřený existujícím GitHubActionsOIDCVerifier proti pinned policy a test keys | Samotný JWT prokazuje pouze Actions kontext, nikoliv vykonaný artifact; produkční trust stále chybí |
| Fyzický binding | AuthorityBinding table/pk/incarnation/authority_id z konfigurace harnessu mimo obnovenou DB, znovu svázaný s ingress | Model neověřuje skutečné IAM, routing, restore ani správu externího incarnation fence |
| Write/ACK transport | Důvěryhodný jednorázový synthetic UpdateItem, přesný ALL_NEW, RetryAttempts=0 a strong readback | HTTP 200, readback a metadata samy neprokazují jedinečný ACK, linearizovatelnost nebo poctivost adapteru |

TCB zahrnuje všechny writery, ingress/evidence autority, clock, transport,
schema enforcement a fyzickou konfiguraci. Skutečná služba toto musí vynutit
mimo worker proces; raw UpdateItem IAM grant samotný klientské kontroly
nevynucuje. Takové nasazení v tomto úkolu není provedeno ani prokázáno.

## Jediný item, revize a přechody

Kompletní wire item má přesně osm atributů (Dynamo typed values):

```text
pk, incarnation, generation,
document                 # celý OfflineControlStateV3: ON, epoch, pending,
                         # last intent/slot, daily quota/finance, worker_claim
command_schema           # offline-command-v1
run_authorization        # null -> pevný RunAuthorizationDraft
admission_schema         # offline-admission-v1
reservation              # null -> pevný ReservationRecord
```

ReservationRecord uchovává evidence ID, intent/epoch/reserved_at, vydání a konec
platnosti approval, trust generation, run reserve USD, **přesnou výslednou
daily quota**, digest preflightu, code/config/artifact scope a jméno pevného
syntetického plánu. Nejsou v něm tokeny. Celý item má lokální limit 16 KiB;
existující command core dál respektuje vlastní 4 KiB limit.

Opaque revision obsahuje existující binding/generation revision a digests run
authorization a reservation. Není důkazem identity. Každý platný writer musí
generaci monotónně zvýšit; pouhá revize nechrání proti privilegovanému restore.

| Příkaz | Povolený payload | Přesný přechod |
| --- | --- | --- |
| reserve_intent | expected_revision, approval_epoch | Unreserved, enabled, epoch souhlasí, owner/auth null → nový pending + last slot/time + attempts+1 + konzervativní USD floor + immutable reservation; generation+1 |
| bind_approved_run | expected_revision, approval_epoch, intent_id | Durable pending/reservation, enabled, owner/auth null → pevný run authorization; jen generation+1, ostatní stav přesně zachován |
| claim_reserved_intent | Původní intent/epoch/revision | Stávající gateway/engine při fixním reservation i authorization, žádná další rezervace nebo navýšení rozpočtu |
| emergency_stop | Původní expected_revision | Stávající STOP, zachová reservation/auth/pending/owner/finance |
| read_control | Prázdný payload | Pouze úplné pozorování; nevrací oprávnění, continuation ani znovu potvrzený write ACK |

Reserve znovu používá **všechny přísnější V2 kontroly**: samostatné čerstvé
hodiny GitHub/funding/Perplexity/žurnálu/spendu, explicitní schválení a first
attempt, nulové active/queued runs, canonical USD/datum, úplnost auditu,
shodu durable počtu pokusů, denní quota/cap, spacing i persistentní slot.
Finanční návrh je `max(durable_reserved, observed_charged) + run_reserve`;
nižší další charge čtení nesmaže původní floor. Žádný rollover/refund/release.

Při proof.plan=None se čte skutečný nezměněný WORKER_CONFIG: **2×3, 4 Agent
POST > automatic limit 2 → DENY před jakýmkoliv zápisem rezervace**. Ani
budoucí menší reálný plán zde nezíská GO: produkční authority není implementována.
Pozitivní atomika dovoluje pouze pevný `SYNTHETIC-ATOMICITY-ONLY`, 2 fake Agent
počty, z nezávislé test autority; payload ho nemůže vybrat a žádný skutečný
worker/caller není zapojen. Není to nové runtime nastavení ani supervised režim.

Bind ověřuje nezávisle schválený run a jeho signature-checked identity:
přesná repo/owner ID, source SHA, workflow ref/SHA, run ID, attempt='1',
audience/issuer/subject, platnost a trust generation. Scope/config/artifact musí
odpovídat uložené rezervaci. Run approval musí začínat po rezervaci a jeho konec
nesmí prodloužit její pětiminutovou approval platnost. Expirace nic neuvolňuje;
další run, rerun či přepsání bindingu nejsou implementované.

## CAS, nejasné výsledky a STOP

Podmínka každého nového zápisu obsahuje rovnost **všech osmi původních
schema atributů**, včetně celého document, reservation a run authorization.
Navíc vyžaduje existující pk, konkrétní pk/incarnation, generation/headroom,
enabled/epoch, owner null, authorization null a podle příkazu pending null
nebo přesný intent. Aktualizace mění jen definované části a dělá serverové
`generation = generation + 1`; nikdy upsert. Poslední increment je vyhrazen STOP.
Při claim/STOP projekce přidává celou reservation do původní CAS podmínky;
původní zmrazení run authorization se neoslabuje.

V rámci každého příkazu: strong GET → ověření/recheck autority → nejvýše jeden
UpdateItem → přesný ALL_NEW/HTTP200/RetryAttempts=0 → strong readback. Timeout,
konflikt, 5xx/access error, rozpojení, neúplný ACK, odlišný readback nebo neznámý
výsledek znamenají false + reconciliation. Žádný automatický druhý send,
ClientRequestToken success replay, lease, takeover nebo readback-auth.

Po ztraceném ACK stav může obsahovat skutečný commit. Restart ani fresh GET
nevydají druhé kladné potvrzení stejného přechodu. Nová navazující autorizační
fakta smí připravit pouze nezávislá autorita po samostatném posouzení; handler
nepřevádí GET na automatické schválení. Žádná nezávislá produkční rekonciliace
zde není implementována. Schválení/clock se kontrolují před sendem, nikoli
atomicky se serverovým časem během přenosu. Uložená expirace se nikdy neobnoví.

STOP před CAS zneplatní celou podmínku; STOP po potvrzení uchová finance i
binding. OFF po posledním čtení může přijít současně s účetním ACK předchozího
commitu. Ten nepotvrzuje aktuální ON a nikdy nedává execution grant. Atomické
zastavení DB+externí provider se netvrdí; tento modul provider vůbec nemá.

## Testy a sedm oddělených počtů

Nový soubor `tests/test_reserve_bind_offline.py` má **47 testů**. Všechny
původní testy i fixtures zůstaly bytewise stejné. Úplná test ID a skutečné
výsledky jsou v revizním ZIPu; focused run navíc ukládá seven-count-traces.json.

| Skupina skutečných regresí | Testy / důkaz |
| --- | --- |
| Skutečný reserve/bind z unreserved bootstrapu | positive_reserve_bind_exact_after_state_and_seven_counts; přesný quota floor 0.20/observed0.30 +0.40 →0.70, attempts0→1, generation1→2→3, owner null |
| Reálné admission a V2 | real_worker_plan; stricter clocks/flags; spend counts/ages/caps/active runs; persisted floor/spacing; expired/future/malformed evidence |
| Souběh a replay | two_controllers_racing_reservation; two_differently_approved_runs; repeat reserve/bind/fresh revision/restart; stale GET po reserve i bind |
| Identita a autorita | caller payload overrides; unregistered/wrong role/missing trust; signed wrong run/repo/owner/source/workflow/attempt a forged tokens; config/artifact/trust mutations; approval revocation; no renewal |
| Úplný item | all-field mutation bez generation increment; bind quota/pending/epoch/auth/reservation mutations; explicit strong GET/ALL_NEW/metadata; incomplete ACK/readback |
| Nejasný zápis | timeout před reserve i bind commitem; lost reserve ACK; lost bind ACK/disconnect; BaseException po commitu; nový subprocess restart; synthetic error codes/hidden retry |
| STOP/TCB | OFF před reserve/bind, mezi GET/CAS obou, po posledním GET, headroom obou; claim/STOP preservation; incarnation/raw worker deny; ABA, rollback, unknown attribute, replayed success counterexamples |

Jedna stopa nemá reset počítadel mezi reserve a bind. DB request je volání
syntetického GetItem/UpdateItem; diagnostická přímá inspekce SQLite není nový
API požadavek ani autorita. Kladný ACK znamená jen potvrzení mutačního příkazu,
nikoliv kladné read_control. Provider fixture není handleru vůbec předána.

| Skutečně naměřená stopa | DB requests | Commity | Kladné ACK | Lokální pokračování | Připuštěné transport entries | Fyzické fake requests | Fake účinky |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Unreserved → reserve → bind | 6 | 2 | 2 | 0 | 0 | 0 | 0 |
| Reálný plán 4>2 odmítnut | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| Reserve commit, ACK ztracen | 2 | 1 | 0 | 0 | 0 | 0 | 0 |
| Reserve potvrzen, bind timeout před commitem | 5 | 1 | 1 | 0 | 0 | 0 | 0 |

### Zachované negativní protipříklady

- Privilegovaný rollback celé původní obálky se starou generací a stejnou
  incarnation dovolí další reserve/bind pro jiný run: test vykáže 4 commity.
  To je **porušení TCB**, nikoli garantovaná prevence restore. Nová chráněná
  incarnation odmítne starý item; její skutečná správa není prokázána.
- Neplatný transport, který dvěma volajícím přehraje stejný ALL_NEW ACK,
  může vykázat **2 kladné ACK na 1 commit**. Metadata/readback samy neprokazují
  jedinečný conditional ACK; žádné continuation ani síť se přesto nevytvoří.
- Chybně retryující DB wrapper provede 2 fyzické UpdateItem pokusy, ale pouze
  1 commit a 0 kladných ACK. Výrobní transport musí zakázat vnitřní retry;
  samotný počet Python volání není důkazem počtu fyzických requestů.
- Privilegovaný writer může mezi GET/PUT přidat **neznámý top-level atribut**.
  Rovnosti osmi známých atributů nevyjadřují nepřítomnost libovolného devátého:
  commit může nastat, ale úplný ACK decoder jej odmítne. Přísně omezení writeři
  a uzavřené schema jsou nutná součást TCB, nikoliv vlastnost IAM samotného.

## Skutečné ověření a omezení

| Nový místní běh | Passed | Failures/errors/skips | Unexpected network |
| --- | ---: | --- | ---: |
| Původní tests.run --strict-known-bugs včetně nových testů | **764/764** (717 původních +47 nových) | 0/0/0; žádné known failures/unexpected successes | 0, včetně outer audit guardu |
| Focused nové testy, samostatný překrývající běh | **47/47** | 0/0/0 | 0 |
| Tools zvlášť | **19/19** | 0/0/0 | 0 |
| Botocore lab zvlášť | **6/6** | 0/0/0 | 0, pouze synthetic HTTP/pool |

Vše běželo na Python 3.12.14. Aplikační prostředí má ověřeno všech 112
použitelných main pinů poetry.lock: PyJWT2.10.1, cryptography45.0.4,
forecasting-tools0.2.92, requests2.32.5, openai2.13.0. SDK venv zvlášť:
botocore1.43.126, urllib3 2.8.0, jmespath1.1.0, python-dateutil2.9.0.post0.
Žádná instalace ze sítě. Python3.11 runtime nebyl testován, jen grammar check.
Outer socket/HTTP audit doplňuje nezměněný runner; pouze lokální urllib3 IPv6
capability probe je potlačena. Skutečné IPv6/TLS není ověřeno. Záměrné guard
self-probes původní sady nejsou neočekávané živé požadavky.

SDK lab ověřuje dosavadní engine/retry policy se synthetic send/pool; není
důkazem nasazeného DynamoDB ani serializace/atomicity nového admission requestu
v AWS. Historické GitHub CI 717/717 je údaj zadání, zde nebylo načteno ani
znovu ověřeno. Focused47 jsou podmnožinou764, tools19/SDK6 samostatná evidence.

Reálný plán **2×3/4 Agent POST/auto cap2** i nominálních 20 dalších LLM
invokací zůstává beze změny. Neproběhl živý GET/POST, cloud test, dispatch,
forecast ani placené volání. Ceny, billing hard caps a provozní práva se zde
neověřují. Legacy main/publisher/žurnál jsou jiná neintegrovaná write cesta;
nový offline obal je zpětně nechrání.

Patch přidává právě tři soubory: tento dokument, reserve_bind_offline.py
a tests/test_reserve_bind_offline.py. Původních 160 souborů, workflows, CLI,
lockfile, main a sedm starých pracovních kopií zůstává nedotčeno. V manifestu
jsou hashe, git status, apply/reverse/tree evidence a protokoly. Produkční kotvy
CONTROLLER_STORE, CONTROLLER_VERIFIER, PRODUCTION_COMMAND_GATEWAY a
PRODUCTION_EFFECT_RECOVERY zůstávají None; CommandResult.execution_allowed=False.

Další krok je nezávislá revize tohoto izolovaného kontraktu. Případný návrh
create-effect/katalogu operací, produkční autority/IAM/provenance, migrace,
credential GET a jakýkoliv placený pilot vyžadují nové samostatné zadání a
výslovný souhlas pro externí zásahy. Tento patch nic takového nepovoluje.
**Výsledný stav: PRODUCTION DENY.**
