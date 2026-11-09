# Serverová command brána V3: offline návrh a bezpečnostní kontrakt

Přesný základ: `1b090e2f75178aa7fec6e9b070943f0a5874db21` (PR #38).
Datum: 2026-11-09. Nový čistý detached worktree:
`/workspace/vrchcab-command-1b090e2`. Předchozí kopie se nemění.

Tento dokument vznikl nejprve jako návrhový audit před implementací. Níže
odděluje model aplikace od neověřeného nasazení. Neexistuje síťový server,
Lambda, cloudový prostředek, reálná invoke role ani produkční autentizace.

## Threat model před implementací

| Aktér | Předpokládané možnosti | Povolená cesta a riziko |
| --- | --- | --- |
| Nedůvěryhodný worker | Mění payload, token, lokální SHA, prompt, závislosti, pořadí a počet požadavků; může být kompromitovaný | Jen úzké invoke commands. Nemůže vybrat store, policy, owner, request ID, expressions nebo oprávnění. OIDC neprokazuje vykonávaný kód |
| Controller | Navrhuje rezervaci, schválení běhu a účetní operace | Budoucí samostatně autorizované serverové commands nad stejným itemem. Nesmí zachovat raw write bypass ani svévolně měnit potvrzeného vlastníka |
| Provozovatel brány | Provozuje a nasazuje serverový artifact a konfiguraci | Je součástí TCB; worker nesmí měnit jeho kód, roli, trust ani physical binding. Kompromitace této autority není vyřešena klientským CAS |
| Operátor STOP | Nezávislá krátkodobá autentizace, oddělená od worker role | Smí jen OFF; nesmí ON, release, takeover ani settlement. Dostupnost jeho ověření nemá záviset na worker JWT klíčích |
| Administrátor | Mění IAM, table, deployment, restore, trust nebo control plane | Chráněná oddělená control plane, audit a break-glass. Není kryptograficky omezen tímto Python prototypem |
| Útočník s platným odcizeným tokenem | Může závodit s legitimním během v rámci stejné identity | Kontrola přesného schváleného běhu a jednoho owner slotu; token není důkaz držení schváleného kódu. Útok může způsobit odepření služby |
| Privilegovaný raw writer | Obchází bránu, obnoví starý dokument/generaci nebo přepíše vlastníka | Mimo bezpečnostní předpoklady modelu. Musí být vyloučen z aplikačních rolí; při zachování této schopnosti nelze slíbit invarianty |

TCB tvoří serverový artifact a konfigurace, ověřený ingress, autentizační
autorita, nezávislé hodiny, jediný store a jeho implementace a chráněná
control plane. Python privátní atribut nebo typ objektu nejsou sandbox.
Worker nesmí vykonávat svůj kód uvnitř procesu brány.

| Vrstva | Co má vynucovat | Co zde není důkaz |
| --- | --- | --- |
| Aplikace brány | Pevná schémata, serverový owner/request ID, povolené přechody, žádný ON/retry/takeover, všechna rozhodnutí DENY pro execution | Lokální test nenahrazuje deployment a jeho integritu |
| Databáze | Jeden item, atomický conditional update, úplný precondition a +1 generace | Syntetický SQLite model není skutečné AWS |
| AWS IAM/ingress | Worker pouze invoke; jen serverová role smí zapisovat, oddělený STOP a controller | Nejsou vytvořeny nebo simulovány skutečné policy/SigV4/STS |
| Oddělená control plane | Ochrana deploy/role/trust/restore, nový physical binding a incarnation při cutoveru | Správce ovládající data i konfiguraci může porušit jakýkoliv lokální fence |

## Rozhraní a rozhodnutí návrhového auditu

Veřejná data příkazu:

```text
read_control           {}
claim_reserved_intent  {intent_id, approval_epoch, expected_revision}
emergency_stop         {expected_revision}
```

Chybějící i neznámá pole se odmítají. Není pole pro owner, request ID,
verified, token claims, tabulku, key, incarnation, roli, konfiguraci,
UpdateExpression, nový dokument nebo execution_allowed. Autentizovaný
invocation context přichází z jiného, důvěryhodného vstupu než command data.
V modelu jde výslovně o testovací objekty registrované mimo požadavek workeru;
žádné `operator_stop_authorized=True` ani běžně zkonstruovaný claims objekt
nejsou autentizací. Výchozí chybějící autentizace znamená DENY.

Úzký návrhový audit dovoluje pouze syntetický handler. **Úplná produkční
identita, oprávnění a provozní zákaz redelivery nejsou vyřešeny.** Nelze proto
vytvořit produkční instanci, route nebo skutečný claim. Produkční vazby zůstávají
None. Úspěch modelu znamená záznam vlastníka v testovací databázi, nikdy právo
spustit výzkum, forecasting nebo publikování.

Zásadní mez: stateless proces nemůže po restartu z nezměněného itemu poznat,
zda předchozí nejasný zápis vůbec dorazil. Prázdný owner a nové GET proto nejsou
povolením k automatickému opakování. Handler nedělá retry a každá odpověď má
`retry_allowed=False`. Budoucí invoker nesmí po timeoutu automaticky znovu
doručit command. Trvalá globální záruka nejvýše jednoho **odeslaného pokusu**
při bezstopém selhání není tímto modelem prokázána. Nevytváří se pro ni druhý
ledger nebo falešný lokální replay flag. Dva úmyslně nové požadavky při
opakovaném selhání před commitem mohou oba skončit bez zápisu; žádný tím
nezíská owner ani execution. Pokud další explicitní invokace už uspěje,
může jako první uložit owner; brána nemá z čeho odvodit chybějící historii.
Test tento silnější protipříklad výslovně zachovává. Jakýkoliv provozní
retry/reconciliation návrh vyžaduje další revizi a do té doby je produkční
cesta DENY. Nejde o dvě potvrzení owneru: protipříklad rozlišuje počet
odeslaných pokusů, počet commitů a počet potvrzení.

## Jeden item a izolovaný návrh schválení běhu

Současný OfflineControlStateV3 neumí uložit schválené run ID. Nemění se jeho
codec ani DynamoConditionalStorePrototype. Nový **offline-command-v1 draft**
rozšiřuje pouze experimentální obálku téhož itemu:

```text
pk, incarnation, generation, document = celý stávající offline V3
command_schema = "offline-command-v1"
run_authorization = null nebo {
  authorization_id, intent_id, approval_epoch,
  approved_at, valid_until, trust_generation,
  policy: {audience, subject, repository, repository_id,
           repository_owner_id, source_sha, workflow_ref, workflow_sha,
           run_id, max_token_lifetime_seconds}
}
```

Všechna aktivace, epochy, pending, kvóta, přesná USD rezervace, vlastník,
generace a run authorization leží v jednom itemu. Není cache vlastníků nebo
druhý ledger. `valid_until` omezuje přijetí autentizace; není lease vlastníka
a jeho uplynutí nikdy nic nemaže nebo neuvolňuje. Testovací data seeduje
nezávislá fixture; nevzniká migrační nebo inicializační command.

Serverová korelace request ID se odvozuje deterministicky z domény protokolu,
physical bindingu, authorization ID, intentu, epochy a schváleného běhu.
Nemá význam credential. Vlastník vzniká pouze z výsledku nezávislého ověření
tokenu proti uložené policy a lokálně důvěryhodným klíčům. Při restartu se
existující owner nikdy znovu nepotvrzuje ani nepřepisuje.

Tenká privátní projekce dovoluje znovu použít stávající engine bez změny jeho
formátu. Předá mu základní čtyři atributy, ale skutečné čtení i ALL_NEW musí
validovat **celý rozšířený item**. Každý skutečný UpdateItem dostane navíc
podmínku rovnosti command_schema a úplného run_authorization. Přidání
schválení nesmí proběhnout jako nezávislá autorizační tabulka nebo jen GET
před PUT. Změna schválení mezi čtením a zápisem musí konfliktovat atomicky.

## Návrh nezávislé identity a vazby run ID

Budoucí cesta je GitHub OIDC → STS krátkodobá **invoke-only** role →
ověřený SigV4 ingress → úzká brána. Pouhý caller ARN/session name nebo
neověřené identity hlavičky nestačí. Role nemá DynamoDB write, změnu brány,
assume/pass-role na její roli, změnu trust ani restore. STOP má oddělený
principal a autentizační kořen; controller další oddělený principal.

STS role kontext sám nepřenáší automaticky všechny GitHub claims. Broker
potřebuje nezávisle ověřený GitHub JWT a jeho přesný run binding, případně
samostatný ověřený control-plane důkaz. Token pro STS `aud=sts.amazonaws.com`
nelze bez dalšího znovu označit za broker token s jiným audience. Návrh musí
prokázat vazbu invoke principalu a broker identity; session name ji nedokazuje.

Existující verifier kontroluje RS256, trusted key IDs, issuer, přesné string
audience/sub/repository IDs, source/workflow commits, workflow ref, run ID,
run_attempt=1 a čas. Policy ani klíče nesmí dodávat worker. Klíčový snapshot
má vlastní serverovou trust generation, dobu platnosti a revokační stav;
unknown kid nebo nedostupnost/neshoda snapshotu znamená DENY, bez síťového
JWKS fallbacku. Rotace musí být nezávisle schválena, starý snapshot nelze
automaticky oživit z backupu. Tento úkol žádný skutečný JWT ani JWKS nezískává.

Oficiální GitHub JWT příklad obsahuje header `x5t`, zatímco existující
striktní verifier jej odmítá. Reálná tokenová kompatibilita nebyla ověřena;
tento fail-closed rozdíl neopravujeme oslabením existujícího verifieru.
Pro reusable workflows je nutné nezávisle posoudit také job_workflow_ref/sha
a vztah volajícího/volaného workflow; současná policy je tímto důkazem automaticky
nedoplňuje. Skutečná trust konfigurace zůstává nepřipravená.

Aktuálně připnuté oficiální zdroje vyžadují obezřetné čtení: GitHub reusable
uvádí nedostupnost custom OIDC claims v AWS, kdežto AWS action README popisuje
rozšířenou podporu dalších claims a sub customization v roce 2026. Nejde
o zde ověřenou úplnou matici IAM condition keys; rozdíl custom/additional
claims a konkrétní podporu je nutné vyjasnit před trust policy. README dále
popisuje immutable subject pro nové/opt-in/přejmenované repozitáře od července
2026. Skutečný subject projektu se zde nezjišťoval a neodvozuje se z názvu.

Run ID není znám v okamžiku rezervace. Budoucí interní command může jednou
navázat na stále aktivní, neclaimnutý intent přesně jeden schválený běh,
po nezávislém ověření důvěryhodného GitHub control-plane záznamu, zdrojového
commitu, workflow definice/commitu a prvního attemptu. Musí změnit celý item
s očekávanou generací a +1. API response od workera, workflow display name,
branch name nebo GITHUB_SHA nejsou tímto důkazem. Command bind/run, GitHub
readback ani dispatch se zde neimplementují. Fixture není jejich náhradou.

Platný JWT a schválené run ID stále **neprokazují skutečně vykonávaný kód,
konfiguraci, závislosti a prompty**. Podmínky artifact/configuration bindingu
a produkční trust provenance chybí. Proto execution_allowed vždy False.
Kompromitovaný platný token může soutěžit o svůj schválený owner slot; ani
úspěch tohoto útoku nesmí být považován za schválené vykonávání.

## Budoucí rezervace, kvóta a settlement — pouze návrh

Všechny další writeři musí přejít na tutéž serverovou autoritu; ponechání
starého raw controller write grantu by nový návrh znehodnotilo.

| Budoucí command | Povinné serverové podmínky a atomická změna |
| --- | --- |
| reserve_intent | Jen interní controller, důvěryhodný clock a nezávislé čerstvé preflight evidence; aktivace/epocha/scheduler/finanční strop i skutečný výzkumný plán; žádný pending nebo nevyřešený owner. Stejný item zvýší attempts, konzervativní reserved_usd, nastaví intent a prázdné run authorization, +1 generaci |
| bind_approved_run | Jen interní controller po nezávislé kontrole běhu; stále stejné pending/epocha, žádný owner ani dřívější binding. Jednou nastaví run authorization a +1 generaci; bez druhé finanční rezervace |
| record_settlement | Jen nezávisle autorizovaná účetní služba s doloženým výsledkem providerů, ne worker flag success. Vazba na intent/owner a expected generation; marker a přesná účetní evidence ve stejném itemu. Kumulativní rezervaci nikdy automaticky nesníží a nejistý výsledek ponechá uzavřený |
| quota_day_review | Jen nezávisle schválená rekonciliace v OFF, bez nevyřešených operací; explicitní schema pro denní přechod, zachované replay watermarks/tombstones a +1 generace. Žádný reset podle hodin nebo TTL |

Terminální historie a bezpečné přijetí dalšího intentu potřebují další
navržené schéma: zachovat owner/replay tombstones a monotonní watermark,
nejen smazat worker_claim/pending. Omezenou velikost itemu ani retention nelze
řešit automatickým mazáním nejasných operací. Tyto commands jsou v handleru
nepodporované a DENY; model je nespouští přes runtime. Dosavadní preflight
ani konzervativní quota se neoslabují. Dva reporty × tři prognózy potřebují
čtyři Agent POST, automatický limit zůstává dva; návrh tento rozpor neřeší
zvýšením rozpočtu ani změnou počtu reportů.

## Obnova, nouzové OFF a nejasný výsledek

STOP zachová intent, run authorization, owner i kvótu a zvýší generaci.
Nikdy nevrací ON. Worker credential nesmí STOP autorizovat. Konflikt nebo
timeout nelze vydávat za úspěšné trvalé zastavení; žádný automatický retry.
OFF během claimu je zahrnuto v atomickém precondition. OFF po claimu neodstraní
vlastníka. Zrušení skutečného běžícího procesu by bylo další samostatné opatření;
tento handler žádný proces nespouští.

Restore staré authority pod stejným bindingem zůstává privilegovaný bypass.
Bezpečný cutover vyžaduje samostatné schválení, OFF, nezávislou rekonciliaci,
nový physical cíl a incarnation, zrušení starých invokací a ochranu konfigurace
mimo obnovovaný snapshot. Samotná incarnation ve stejném backupu nic neřeší.
Brána neposkytuje restore/init/re-enable command. Při nedostupné DB, hodinách,
trust nebo autorizaci se úspěch netvrdí a nic se nespouští.

## Ověření a předání

Modelový kontrakt má explicitní předpoklady: důvěryhodný testovací ingress,
auth autorita a hodiny, jediný platný physical binding, všichni aplikační
writeři procházejí bránou a transport atomicky vynucuje emitovaný precondition.
Za těchto předpokladů handler vynucuje úzké commands, nejvýše jedno potvrzené
převzetí, neměnnost owner/rezervace/kvóty, +1 generaci, OFF a trvalé execution
DENY. Neopakuje zápis uvnitř obsluhy. Auth provider z fixture není reálná AWS
autentizace, její registry není druhý ledger a neřeší durable replay historii.

**Verdikt úzkého modelu pevné command brány: SPLNĚN.**
**Verdikt úplného provozního kontraktu včetně globálního zákazu redelivery
po bezstopé nejistotě: NEPROKÁZÁNO.**
**Produkční připravenost: NEPROKÁZÁNA; skutečné commands a execution DENY.**

Toto rozlišení není schválení k nasazení. Privilegovaný rollback a bezstopý
timeout jsou zachované negativní protipříklady, nikoliv přepsané na tvrzení
o vyřešené bezpečnosti. Nejasná odpověď musí vést k zastavení invokeru,
nezávislému čtení/rekonciliaci a případnému OFF, nikoliv nové automatické invokaci.

Nových 39 testových metod zahrnuje i další adversariální subtesty. Plná sada
se spouští existujícím **nezměněným** `python -B -m tests.run --strict-known-bugs`
s `--report`. V odděleném venv byly instalovány pouze hlavní závislosti podle
nezměněného poetry.lock, s původními SHA-256 a TLS ověřením. Nepoužívá se
nový dependency resolver pro volbu verzí: export pinů/markerů/hashů z locku,
`uv pip install --no-deps --require-hashes` a kontrola všech 112 použitelných
main záznamů i `uv pip check`. `crontab` je sestaven z lockem hashovaného
sdistu v izolovaném buildu. Hlavní projekt, jeho lockfile a stará prostředí
se nemění. PyJWT **2.10.1**, cryptography **45.0.4**, forecasting-tools
**0.2.92**, requests **2.32.5**, openai **2.13.0** jsou skutečně použité verze.

| Sada | Skutečný výsledek |
| --- | --- |
| Nová command brána samostatně | 39/39 prošlo, 0 síťových pokusů |
| Celá application sada nezměněným runnerem | 673/673 = 634 původních + 39 nových; 0 failures/errors/skips/known failures/unexpected successes |
| Pomocné tools testy | 19/19, 0 failures/errors/skips/síťových pokusů |
| Oddělená skutečná botocore retry laboratoř | 6/6, pouze falešné HTTP odpovědi/pool, 0 síťových pokusů |
| Celkem unikátních aktuálních testů | 698 = 673 + 19 + 6; samostatný a opakované běhy se nezapočítávají znovu |

Historických 634/634 z PR #38 se nepoužívá jako náhrada nového lokálního běhu.
SDK laboratoř skutečně potvrzuje v botocore 1.43.126 legacy 10 a standard 3
simulované HTTP pokusy při 500, po explicitním total_max_attempts=1 pouze 1,
včetně urllib3 bez transportních retry. Ztracený ACK po commitu může při
defaultu vést k druhému pokusu a zavádějícímu konfliktu. Samotný RetryAttempts
v odpovědi není důkaz konfigurace; všechny další retry vrstvy zůstávají
produkční podmínkou. STS action má jinou retry vrstvu (default 12,
disable-retry; special-characters-workaround může limit obejít). Žádná
změna workflowu nebo použití této action zde neproběhly.

Plný běh má navíc vnější Python audit síťových operací před SDK importy.
Potlačen je pouze importní lokální urllib3 IPv6 capability probe pomocí
socket.has_ipv6=False; síťové guardy a záznamy zůstávají zapnuté. Netestuje se
skutečné IPv6/TLS nebo cloud. Vlastní offline runner hlídá i zachycené pokusy;
záměrné self-testy guardu odlišuje od neočekávaných síťových požadavků.
Subprocess testy čtou uloženou SQLite autoritu v novém interpreteru a instalují
své guardy. Skutečný interpreter je Python 3.12.14; Python 3.11 je pouze
staticky kontrolovaná gramatika, nikoliv nově spuštěná CI platforma.

Skutečná AWS autentizační integrace IAM/STS/SigV4, API/Lambda ingress,
autorizační policy, cloudové race/restore/PITR a důkaz vykonaného artifactu
nebyly ověřeny. Kryptografické testy používají jen syntetické klíče a JWT.
Controller reserve/bind/settlement/denní review jsou jen návrhy s DENY API.
Bezpečný budoucí postup: nezávislá revize tohoto modelu, vyřešení invoker
reconciliation protokolu, úplná matice writer/deploy/restore oprávnění,
serverová trust provenance a runtime/artifact podmínky, připnutý serverový
deployment a všechny retry vrstvy. Teprve další explicitně autorizovaný úkol
může navrhnout neprodukční cloudovou validaci. Žádná migrace nebo aktivace
se zde nepřipravuje k automatickému spuštění.

## Oficiální zdroje a reprodukce

Oficiální veřejné dokumenty byly čteny jako data, v přesných neměnných revizích.
Některé snapshoty se znovu použily s kontrolou SHA-256 z předchozího auditu;
nová čtení se týkají jen statických raw zdrojů, ne AWS/GitHub control API.
URL, revize, hashe a neúspěšný přesunutý odkaz jsou v externím
`reference-manifest.json` a v revizním ZIP.

- [GitHub OIDC koncepty](https://github.com/github/docs/blob/8cd1075179df75319912891d6620c9cb78eefb4d/content/actions/concepts/security/openid-connect.md)
  a [přehled claims](https://github.com/github/docs/blob/8cd1075179df75319912891d6620c9cb78eefb4d/content/actions/reference/security/oidc.md).
- [GitHub OIDC v AWS](https://github.com/github/docs/blob/8cd1075179df75319912891d6620c9cb78eefb4d/content/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws.md)
  a [reusable k custom claims](https://github.com/github/docs/blob/8cd1075179df75319912891d6620c9cb78eefb4d/data/reusables/actions/oidc-custom-claims-aws-restriction.md).
- [AWS action README](https://github.com/aws-actions/configure-aws-credentials/blob/f95a14875b75259d2a3105bff6460ac2e653eea1/README.md).
- [Oficiální STS API model](https://github.com/boto/botocore/blob/135910e22c937b9ff03b0948a633a2913d62af87/botocore/data/sts/2011-06-15/service-2.json):
  odpověď obsahuje subject, audience, assumed role a případně SourceIdentity,
  nikoliv obecnou ověřenou kopii všech GitHub claims. RoleSessionName je vstup
  volajícího, ne nezávislý run-ID důkaz. SourceIdentity vyžaduje doložené IdP
  mapování; nepředpokládá se, že je dostupné ze standardního GitHub tokenu.
- [DynamoDB model](https://github.com/boto/botocore/blob/135910e22c937b9ff03b0948a633a2913d62af87/botocore/data/dynamodb/2012-08-10/service-2.json),
  [botocore Config](https://github.com/boto/botocore/blob/135910e22c937b9ff03b0948a633a2913d62af87/botocore/config.py)
  a [Boto3 retry průvodce](https://github.com/boto/boto3/blob/ed37b7307d34ec755afb45f9db9a7b5eb6c1bb4d/docs/source/guide/retries.rst).

```bash
# V odděleném prostředí se správnými zamčenými závislostmi:
python -B tests/test_command_gateway_offline.py
python -B -m tests.run --strict-known-bugs --report /tmp/vrchcab-command-results.json
# Volitelně v odděleném hash-pinned botocore labu:
python -B tools/verify_dynamodb_sdk_offline.py --report /tmp/vrchcab-sdk-results.json
```

Nové testy nalezne standardní discovery; rozšíření CI path filtru o nový
modul, dvě testovací cesty a dokumentaci je **pouze návrh**. Workflowy a jejich
oprávnění se nemění. Kompletní patch proti přesnému základu, manifest,
SHA-256, revizní ZIP a protokoly obsahují ověření aplikace, reverze, Git stromů
a souborových hashů i opakované testování aplikované kopie.

Všechny původní soubory, workflowy, žurnály, secrets, scheduler,
CONTROLLER_STORE=None, CONTROLLER_VERIFIER=None a execution DENY se zachovávají.
Neproběhl živý cloud API call, provisioning, GitHub zápis, push, PR, merge,
dispatch, V2 → V3 migrace, placené API, skutečný forecast nebo publikování.
