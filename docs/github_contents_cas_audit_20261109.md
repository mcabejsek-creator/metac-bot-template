# Offline bezpečnostní audit GitHub Contents CAS

**Verdikt: NE — GitHub Contents backend v současné podobě není vhodný pro
produkční CAS worker claim.**

Základ: `b0f5089b87b9e6e02674e80ec9ad26042705e32a` (PR #36).
Nový čistý detached worktree: `/workspace/vrchcab-cas-audit-b0f5089`.
Starší kopie, jejich změny, indexy a HEAD se zachovávají. Chybějící základ
byl získán jedním read-only Git fetchem do nové samostatné Git databáze.

Současný adapter je pouze V2 a používá blob SHA. Kontrakt V3 vyžaduje
autoritativní celý stav a **nerecyklovanou revizi každé změny**. Návrat stejných
bajtů obnoví stejné blob SHA, což tuto vlastnost neposkytuje. Syntetický model
umožňuje projít zastaralému zápisu po OFF→ON a při rollbacku vlastníka potvrdit
dva odlišné vlastníky téhož intentu. Nejde o kolizi SHA-1: bajty jsou totožné.

To je pro zamítnutí současné architektury dostačující. **Není tím prokázán
konkrétní průběh skutečného GitHub serveru**; jeho linearizovatelnost,
durability a cache konzistence zůstávají neprokázané. Nebyl proveden živý GET,
PUT ani souběžný experiment na GitHub REST/GraphQL API. Model zkoumá chování
blobové podmínky, které samotná zveřejněná smlouva nevylučuje. Neodvozuje se
záruka bezpečnosti ani provozní pozorování pouze z HTTP 200 nebo 409.

## Použité oficiální zdroje a jejich dosah

Požadované reference:

1. [REST Contents](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents).
   Parametr `sha` aktualizace je popsán jako „The blob SHA of the file being
   replaced“. Dokumentovány jsou 200/201, 404, 409 a 422; dokumentace upozorňuje
   na konflikty paralelních Contents operací a požaduje jejich serializaci.
   Neříká, že blob SHA je unikátní generací nebo že každý 409 dokazuje obecný
   linearizovatelný CAS. GET může pracovat s mutable branch ref.
2. [REST Git refs](https://docs.github.com/en/rest/git/refs#update-a-reference).
   `sha` je **nová cílová hodnota** reference. `force=false` požaduje
   fast-forward. Rozhraní PATCH nemá explicitní parametr očekávaného starého
   OID. Fast-forward není totéž jako libovolný CAS s `expected_revision`.
3. [GraphQL Git / RefUpdate](https://docs.github.com/en/graphql/reference/git#refupdate)
   a [mutation updateRefs](https://docs.github.com/en/graphql/reference/mutations#updaterefs).
   `RefUpdate.beforeOid` stanoví očekávaný původní OID; je **volitelný**.
   `afterOid` je nová hodnota, `force=true` dovoluje non-fast-forward.
   `updateRefs` výslovně popisuje atomické provedení všech požadovaných
   aktualizací referencí: při odmítnutí jedné se nezmění žádná další.
   Samotné `updateRef` toto pole `beforeOid` nemá. Nulový OID dovoluje
   vyjádřit vytvoření/smazání; i to musí budoucí ochrany reference řešit.

Přímý přístup na `docs.github.com` blokoval CONNECT proxy s 403. Pro ověření
byly čteny veřejné **statické oficiální zdroje**, s TLS ověřováním, nikoliv API:

- [OpenAPI github/rest-api-description](https://github.com/github/rest-api-description/blob/2f8f80e47ea4d3292cf4e37ba9d9b25475e06140/descriptions/api.github.com/api.github.com.json),
  revize `2f8f80e47ea4d3292cf4e37ba9d9b25475e06140`.
- [GraphQL schema github/docs](https://github.com/github/docs/blob/8cd1075179df75319912891d6620c9cb78eefb4d/src/graphql/data/fpt/schema.docs.graphql),
  revize `8cd1075179df75319912891d6620c9cb78eefb4d`.
- [Zdroj stránky GraphQL Git](https://github.com/github/docs/blob/8cd1075179df75319912891d6620c9cb78eefb4d/content/graphql/reference/git.md)
  potvrzuje generování reference z tohoto schématu.

Výňatky, URL, plné revize a SHA-256 přečtených zdrojů jsou v externím
`official-documentation-excerpts.json`. Nejde o živé ověření chování verze
API, kterou adapter volí hlavičkou `2022-11-28`. Z dostupné dokumentace
nepřisuzujeme API další nezveřejněné záruky obnovy, cache nebo write fence.

## Výchozí architektura

`dispatch_intent_controller.py` V2 rezervuje intent a nákladovou/denní kvótu
jedním CAS. V3 prototyp přidává vlastníka **do stejného celého dokumentu**,
nikoliv do druhého ledgeru. `worker_claim_offline.py` výslovně vyžaduje
autoritativní store, durable commit a nerecyklované revize včetně zápisu
totožného obsahu. Po CAS vyžaduje přesný readback a změnu revize. Zápis ani
recovery automaticky neopakuje, nárok nevyprší a vlastník se automaticky nemění.

`github_control_store.py` těmto požadavkům neodpovídá: jeho revize označuje
blob obsahu, nepotvrzuje historii změn a jeho bool úspěch popisuje odpověď
Contents PUT. V2 adapter neimplementuje V3 a nadále V3 vstup odmítá.

`worker_identity.py` má stále nepřipravenou produkční OIDC důvěru. Podpis
Actions kontextu není důkaz vykonávaného kódu a konfigurace. Původní handoff
v `autonomous_orchestrator.py` vždy odmítá spuštění. Nové modely tyto brány
neobcházejí: modeloví vlastníci jsou pouze syntetická data pro audit úložiště,
nikoliv autentizované objekty předávané skutečné claim funkci.

## Deterministický protipříklad ABA

Nechť A obsahuje aktivní controller, přesnou epochu, pending intent, kvótu
a prázdného vlastníka. Oba kandidáti přečtou `A, SHA(A)`.

| Pořadí | Celý dokument | Blob revize | Co nezjistí zastaralý kandidát |
| --- | --- | --- | --- |
| 1 | A bez vlastníka | SHA(A) | Výchozí snapshot |
| 2 | B s nouzovým OFF | SHA(B) | Aktivace byla zrušena |
| 3 | Znovu přesné A | SHA(A) | Historie OFF už není v revizi |
| 4 | Claim podle starého A | SHA(claim) | Blobová podmínka může projít |

Test `test_contents_off_on_aba_accepts_stale_expected_revision` projde celou
V2 cestou přes skutečný neaktivní adapter a sdílený fake opener, včetně
readbacku. Model neobsahuje žádný živý request.

Silnější protipříklad používá pouze offline V3 dokumenty a tentýž blobový
primitivní model: kandidát 1 uloží vlastníka a projde readbackem; jiný writer
obnoví původní A bez vlastníka; kandidát 2 zapíše proti svému starému SHA(A)
a také projde přesným readbackem se změněným SHA. Oba dostali kladné
**modelové potvrzení záznamu**, avšak `execution_allowed` zůstává False.
Tím je v modelu porušen předpoklad nejvýše jednoho potvrzeného vlastníka
pro daný intent. Neexistence takového jiného writeru nebo rollbacku není
Contents blobovou podmínkou vynucena.

Změna intentu, kvóty či OFF bez následného obnovení A v modelu naopak vede
ke konfliktu. To ilustruje užitečnou ochranu před odlišným současným blobem,
nikoliv důkaz ochrany proti ABA. Ani syntetický úspěch dvou no-op zápisů
se stejným SHA není tvrzením o tom, jak server vyřizuje skutečné no-op requesty;
ukazuje, že samotný blobový kontrakt nenese unikátní generaci každého zápisu.

## HTTP 200 a readback nestačí

Před opravou adapter po 200 akceptoval libovolné 40hex SHA správné cesty.
Nespojoval je s odeslanými bajty. GET také neověřoval soulad SHA s dekódovaným
obsahem a toleroval znaky ignorované permissivním base64 dekodérem.
`IncompleteRead` navíc nebyl převeden na doménovou chybu neověřeného přenosu.
U přerušení už před opravou neexistoval úspěch; oprava sjednocuje fail-closed
výsledek místo úniku jiné třídy výjimky.

Úzká oprava nyní:

- GET striktně dekóduje base64 s povoleným CR/LF zalomením a ověří Git blob
  SHA nad přesnými přijatými bajty před dekódováním control stavu.
- PUT kóduje navržený stav jednou a akceptuje jen 200 se správnou cestou,
  typem `file`, SHA přesně odeslaného blobu a přítomným commit acknowledgment
  s validním tvarem OID. Samotný tvar commit OID ještě nepotvrzuje jeho obsah,
  rodiče nebo očekávaný head.
- Neúplný HTTP přenos normalizuje na `ControlStoreUnavailable`; při PUT je
  výsledek považován za nejasný a nic se automaticky neopakuje.

To pouze **zpřísňuje přijetí odpovědi**. Nepřidává readback, generační CAS,
V3 podporu nebo oprávnění ke spuštění. Ani přesná shoda SHA sama neprokazuje
durable commit, aktuální stav reference nebo nepřerušenou historii.

Fault injection dále ukazuje hranice i správně vypadající odpovědi: správná
metadata 200 bez uloženého očekávaného stavu mohou oklamat samotný ack check;
následné autoritativní čtení by nesoulad odhalilo. Stará interně konzistentní
dvojice obsah/SHA z cache projde kontrolou integrity. Stará kopie claimu
z readbacku může odpovídat návrhu, zatímco současný vlastník už byl jiným
writerem přepsán. Jde o modelované poruchy, nikoliv pozorované chování GitHubu.

I kdyby bylo čtení linearizovatelné, potvrzuje stav pouze v určitém okamžiku.
Nezabrání dalšímu writeru následně odstranit vlastníka nebo změnit aktivaci.
Výhradnost vyžaduje monotónní povolené přechody a omezení **všech** writerů.
Readback není náhradou této vlastnosti ani nezávislou rekonciliací.

## Povinné chybové a souběžné scénáře

| Scénář | Co ukazuje offline sada a bezpečný postup |
| --- | --- |
| Dva kandidáti se stejnou revizí | Při různých nových blobech bez rollbacku model přijme první, druhý odmítne; není to měření reálného souběhu |
| Intent/kvóta změněné mezi GET a PUT | Konflikt zachová nový stav; žádný automatický rebase/retry |
| Nouzové OFF mezi čtením a zápisem | Bez ABA odmítnutí; po OFF→ON je blob revize znovu stejná a ochrana historie chybí |
| Odpověď ztracená po commitu, timeout, reset spojení | Stav může být zapsaný; neudělovat potvrzení, neopakovat zápis, nezávisle rekonciliovat |
| HTTP 409 | Odmítnutí bez retry; kód sám neurčuje, co přesně konflikt způsobilo nebo zda má backend všechny vlastnosti V3 |
| HTTP 422 | Odmítnutí bez retry; validace/spam či jiná příčina není důkazem generačního CAS |
| HTTP 404, 401, 403 | Nedostupný/neautorizovaný stav; neinitializovat, nezakládat branch, nevyvozovat bezpečnou absenci claimu |
| HTTP 5xx | Neúspěch/nejasný výsledek; žádný grant, automatický retry ani nulování rezervy |
| IncompleteRead, zkrácený JSON, nečekaná 201/204 | Nedostatečné potvrzení; zápis už mohl proběhnout |
| Stará cache nebo opožděné čtení | Konzistentní obsah+SHA neprokazují čerstvost; odlišný aktuální blob může způsobit konflikt, ABA však starou hodnotu obnoví |
| HTTP 200 s jiným blobem/cestou/typem | Po opravě odmítnuto; matching metadata stále neprokazují celý autoritativní stav a historii |
| Restart a úmyslně modelovaný opakovaný pokus | Bez ABA staré SHA neprojde; rollback jej může znovu učinit platným; knihovna proto sama neopakuje |
| Jiní writeři, přepsání vlastníka a návrat starého dokumentu | File API nevynucuje přechod pouze `null → owner`; tento kontrakt potřebuje vynucení nad všemi writery |
| Ref force push / návrat starého OID | I beforeOid se může po návratu starého headu opět shodovat; nutné zabránit rewind/delete/recreate a obcházení pravidel |

## Porovnání alternativ A–D

### A. Současné Contents API s blob SHA

**Zdokumentováno:** podmínka očekávaného blobu pro nahrazení souboru,
stavové kódy, volba branche. Není zdokumentována nerecyklovaná generační
revize ani kontrola očekávaného branch headu prostřednictvím parametru `sha`.

**Model:** odlišný blob konfliktuje, ABA obnoví starou podmínku; rollback
nebo jiný writer může dovolit dvě potvrzení vlastnictví. ACK/readback nejsou
ochranou historie. Původní fake s pevnými `a… → b…` tyto vlastnosti nemodeloval.

**Neprokázáno / podmínky:** skutečná atomická a durable implementace, cache,
všechny souběžné interleavingy, branch ochrany a všichni writeři. Oprava
obálky odpovědi tuto smlouvu nezmění. **Nyní NE**; bez změny architektury
a vynucených generačních přechodů se tento adapter k V3 nesmí připojit.

### B. Jeden úplný dokument s nerecyklovanou generací

**Požadovaná smlouva, nikoliv záruka pojmenovaného API:** jediná atomická
autorita pro enabled/epoch/intent/quota/owner i generaci, podmíněný zápis
proti očekávané generaci a nová generace při každé změně. Všichni writeři
musejí zachovat vlastníka daného intentu, monotonii a rezervu; žádné lease
nebo automatické vyčištění nejistého stavu.

**Model:** znovu použitý `SQLiteControlStore` z existujících claim testů
ukládá dokument a rostoucí čítač do jednoho řádku. Po A→OFF→A má jiné opaque
revize a starý CAS odmítne. Nevznikla druhá testovací implementace generací.

**Neprokázáno / podmínky:** konkrétní produkční služba a její durability,
izolace, failover, oprávnění a obnova. Čítač či nonce přidaný jen klientem
do JSON **nestačí**, dovolí-li jiný writer vrátit starý dokument včetně
čítače. Přidání nonce do commit message nezmění blob SHA vůbec. Nutné
serverové vynucení všech přechodů nebo jinak doložená výhradní správa všech
writerů včetně obnovy záloh. Nyní **NEPROKÁZÁNO**.

### C. Git ref CAS pomocí GraphQL updateRefs / beforeOid

**Zdokumentováno:** explicitní předchozí OID v `beforeOid`, nové OID
v `afterOid` a atomické all-or-nothing aktualizace uvedených referencí.
To je silnější a pro tento návrh vhodnější primitivum než samotný blob SHA
nebo REST PATCH s pouhým cílovým `sha`. Nenahrazovat je `updateRef`.

**Model:** A→OFF→A uložené jako nové následné commity mají jiný head OID,
takže starý beforeOid neprojde. S povoleným force rewindem na původní head
se ale vrací i ABA. Opuštění `beforeOid` nebo no-op `afterOid=beforeOid`
neposkytuje požadovanou generační změnu.

**Neprokázáno / podmínky:** ověřená dostupnost a oprávnění konkrétní mutace,
trvalost a správné potvrzení, integrita celého stromu/commitů a skutečně
vynucená pravidla pro všechny účty a admin bypass. Každý přechod musí vytvářet
nový child commit přesně přečteného headu s celým control dokumentem, vždy
posílat beforeOid a odmítat no-op, force, rewind a delete/recreate reference.
Nepřijímat callerem dodané OID jako důkaz schváleného obsahu. Validovat
GraphQL `errors` i při HTTP 200; `clientMutationId` není dokumentovaná
deduplikace vykonání. Ztracená odpověď vyžaduje rekonciliaci, nikoliv retry.
Atomická změna referencí neznamená transakci s billingem či publikováním.
**Možný kandidát, nyní NEPROKÁZÁNO**; žádný takový adapter není implementován.

### D. Samostatný transakční backend

**Možná smlouva, nikoliv univerzální vlastnost každé databáze:** vybraná
služba musí doložit podmíněný atomický update celého autoritativního stavu,
vhodnou izolaci, durable commit a nerollbackovanou generaci. Transakce
kontroluje enabled, epoch, pending intent, revizi a nepřítomnost vlastníka
a současně uloží vlastníka při zachování kvóty. OFF používá stejnou autoritu.

**Model:** existující dočasné SQLite testy a nová ABA diferenciální kontrola
ověřují lokální syntetický kontrakt. Neprokazují provoz cloudové služby,
repliky, failover, obnovu záloh nebo produkční oprávnění.

**Neprokázáno / podmínky:** volba backendu, jeho dokumentované záruky,
omezení zapisujících rolí, konzistentní autoritativní čtení, fencing při
obnově a nezávislé řešení nejasných operací. Musí nahradit autoritu **celého
control stavu**, ne přidat pouze claim tabulku vedle samostatné GitHub
aktivace/rezervace. Tím by znovu vznikly dva rozcházející se ledgery.
**Preferovaný směr pro další oddělený návrh, nyní NEPROKÁZÁNO.**

Žádná alternativa zde neodstraňuje chybějící OIDC trust, runtime binding
kódu/konfigurace, per-request stop/budget kontroly nebo publikační podmínky.
Do jejich prokázání zůstává skutečné zpracování a publikování zakázané.

## Implementace a skutečné lokální výsledky

Sdílený `tests/fake_github_control.py` je vyjmutí stávajícího `FakeGitHub`
a `Reply` z controller testů. Místo pevných `a…/b…` revizí používá skutečný
Git blob digest; původní testy jej importují a zachovávají testované případy.
Očekávání revize nyní porovnává vypočtený digest a konflikt se vyvolává
změnou stavu, nikoliv přiřazením libovolného SHA.
Nová sada používá tento fake, existující SQLite fixture a malý symbolický
ref model. Neobsahuje V3 HTTP/GraphQL adapter ani backend provisioning.

Proti původnímu adapteru vytaženému přesně z výchozího commitu byla stejná
nová sada spuštěna pod síťovým guardem: **27 metod, 7 assertion failures
a 2 errors v subtestech**, 0 síťových pokusů. Konkrétně chyběl binding
ACK/GET SHA a typu, validace commit ACK, striktní base64 a normalizace
IncompleteRead. Původní adapter byl načten jen z externí kopie, staré
pracovní kopie se nepřepisovaly. Po opravě:

| Kontrola, Python 3.12.14 | Výsledek |
| --- | --- |
| Nová stdlib CAS auditní sada | **27/27 prošlo** |
| Existující autonomní runtime offline sada | **37/37 prošlo** |
| Existující `tools/test_*.py` | **19/19 prošlo** |
| Společný aktuální běh | **83 prošlo, 0 failures/errors/skips/expected failures a síťových pokusů** |
| Plný SDK runner a na SDK navázané původní controller testy | **Nespouštěny: nejsou správné zamčené závislosti** |
| Existující kryptografické standalone sady | **V tomto auditu znovu nespouštěny; zamčené crypto verze nejsou dostupné** |

Chybí `requests==2.32.5`, `forecasting-tools==0.2.92`, `openai==2.13.0`.
Jsou zde jiné PyJWT/cryptography (2.13.0/50.0.0 místo 2.10.1/45.0.4).
Nová auditní sada tyto knihovny **nepoužívá**. Nebyla provedena instalace,
SDK suite ani tvrzení o kompatibilitě zamčených verzí. Metadata jsou
v `sdk-eligibility.json`. Čísla nejsou převzata z historických výsledků.
Statická gramatika Pythonu 3.11 není vykonáním testů jeho interpreterem.

```bash
# Z nového checkoutu, bez síťových pokusů:
python -B tests/test_github_contents_cas_offline.py
python -B tests/test_autonomous_runtime_offline.py
# Plný tests.run až ve vybaveném prostředí se správnými poetry.lock závislostmi.
```

Samostatná auditní sada používá existující síťový audit guard; společný
externí harness navíc blokuje Internet socket sends, sanitizuje credentials
pouze v testovacím procesu a selže i na zachyceném síťovém pokusu. Žádná
produkční CLI se nespouští. Před revizí je potřebná plná SDK regrese ve
správně vybaveném prostředí; tato práce ji nenahrazuje.

## Návrh offline CI filtru — bez aplikace

Existující filtr `.github/workflows/offline_bounded_pagination.yaml` už
obsahuje `github_control_store.py` a `tests/test_dispatch_intent_controller.py`,
takže tyto změny by jej při samostatně provedeném PR aktivovaly. Pro budoucí
změny pouze nových souborů navrhuji přidat následující cesty:

```yaml
- "tests/fake_github_control.py"
- "tests/test_github_contents_cas_offline.py"
- "docs/github_contents_cas_audit_20261109.md"
```

Standardní unittest discovery novou sadu zahrne. **Workflow se zde nemění**,
žádný PR ani CI run se nezakládá. Nezměněné zůstávají všechny workflowy,
oprávnění, control branch, V2/V3 schéma, aktivace, scheduler, publisher,
produkční žurnál, secrets, lockfile a skutečný výzkumný plán **2×3 / 4 Agent
POST proti automatickému limitu 2**. Handoff i úspěšný syntetický claim
nadále vždy odmítají skutečné spuštění a publikování.

Kompletní patch proti zadanému základu, kontrolní součty, manifest,
protokoly a výňatky oficiálních zdrojů patří k externím podkladům pro
nezávislou revizi. Kontroluje se aplikace, reverze, výsledný Git strom
a hashe; žádné zápisy na GitHub, workflow dispatch, push, PR, merge,
placený výzkum ani publikování neproběhly.
