# Offline ověřování OIDC kontextu workeru — spuštění vždy odmítnuto

Základ: `69d8d889a7b193f58eae9e379a8d29fac950decf`, 2026-11-09.
Původní `/workspace/vrchcab-worker-identity-69d8d88` v připojeném prostředí
neexistuje; read-only kontrola našla jediný čistý checkout na tomto commitu.
Jde o **rekonstrukci podle specifikace**, nikoliv o obnovené původní soubory
nebo potvrzení jejich totožnosti. Předchozí kopie se nepřepisuje.

**Produkční klíče, jejich důvěryhodný původ, politika a nezávislý zdroj
očekávaného run ID nejsou zajištěny. Handoff vždy odmítá spuštění.**
Kompatibilita s PyJWT **2.10.1** a cryptography **45.0.4** z nezměněného
`poetry.lock` zde **není ověřena**: tyto verze nejsou lokálně dostupné.
Úspěšné kryptografické testy níže jsou pouze průzkumný běh na jiných verzích.

## Hranice důvěry

`worker_identity.GitHubActionsOIDCVerifier` ověřuje kompaktní podepsaný JWT.
Při vytvoření potřebuje `ControllerOIDCPolicy` a neměnnou sadu veřejných
klíčů. Tyto vstupy musí nezávisle určit důvěryhodný controller, nikoliv worker,
JWT, proměnné prostředí, lokální checkout ani volající předávající své SHA.
Samotné vytvoření třídy politiky nebo vložení klíče jeho důvěryhodný původ
nedokazuje. Totéž platí pro návratový objekt `ActionsOIDCIdentity`.

Produkční vstup `verify_controller_worker_token(token, now=...)` nepřijímá
politiku, klíče, expected SHA, objekt identity ani `verified=True` od volajícího.
Používá výhradně modulový `CONTROLLER_VERIFIER`, který v této verzi zůstává
`None`. Není zde loader z prostředí, konfigurace workeru či claims tokenu.
Neexistuje cesta získání tokenu, síťové discovery, JWKS ani jiný transport.

`frozen=True`, tuple a read-only mapa klíčů brání náhodným změnám. Nejsou
bezpečnostní hranicí proti kódu ovládajícímu stejný Python proces. Nahrazení
této konfigurace v testech je výhradně syntetická dependency injection;
nezakládá produkční důvěru nebo oprávnění k automatizaci.

## Co se ověřuje

1. Jediný algoritmus je **RS256**. Podpis kontroluje PyJWT s veřejným RSA
   klíčem načteným přes cryptography. Klíče musejí být veřejné PEM, RSA
   2048–8192 bitů, s unikátními `kid`. Privátní, EC, slabé a neplatné klíče
   se odmítají. Vstupní velikosti a počet klíčů jsou omezené.
2. Neověřená hlavička slouží pouze k výběru `kid` z již důvěryhodné lokální
   mapy. Přijímají se jen `alg`, `typ=JWT`, `kid`; `jku`, `jwk`, `x5u`,
   `x5c`, `crit`, `b64` a ostatní rozšíření se odmítají. Neznámé `kid` není
   důvodem stáhnout nové klíče. `none`, HMAC a jiné algoritmy se odmítají.
3. Claims se používají až po ověření podpisu. Issuer musí být přesně
   `https://token.actions.githubusercontent.com`. Audience je přesně jeden
   očekávaný řetězec; ani jednoprvkový seznam audience se nepřijímá.
   Vyžaduje se i přesně očekávaný `sub` a název `repository`.
4. Všechny povinné claims musejí existovat. `iat`, `nbf`, `exp` jsou skutečné
   nezáporné integery, nikoli bool, float, string či null. Pro nezávislé
   timezone-aware `now` platí `iat <= nbf <= now < exp`; délka tokenu je
   kladná a nejvýše controllerem stanovených 600 sekund. Bez časové tolerance.
   PyJWT ověřuje podpis/issuer/audience; jeho wall-clock časové kontroly jsou
   výslovně nahrazeny těmito kontrolami, aby šel testovat injektovaný čas.
5. `repository_id`, `repository_owner_id`, `sha`, `workflow_ref`,
   `workflow_sha`, `run_id` a `run_attempt` se porovnávají jako přesné
   řetězce proti politice. Politika vyžaduje kanonická kladná ID a plná
   lowercase 40hex SHA. První attempt je výhradně řetězec `"1"`.
6. Source SHA a workflow SHA jsou nezávisle připnuté. `workflow_ref` se
   porovnává celý, včetně cesty a ref; může obsahovat přesný branch/tag ref
   GitHubu, ale samostatný `workflow_sha` vždy připíná commit workflowu.
   Pokud ref obsahuje přímo commit, musí souhlasit s `workflow_sha`.
7. Duplicitní JSON klíče, chybné struktury, neplatný formát, nekonečné JSON
   konstanty a příliš velké tokeny se odmítají. Chybové zprávy neobsahují JWT,
   klíče nebo jednotlivé přijaté claims.

Syntetické klíče se v testech generují pouze v paměti, nic se neukládá.
Neexistují produkční defaulty politiky ani předpřipravený veřejný klíč.
Získání, stáří, odvolání a rotace důvěryhodných klíčů vyžadují budoucí
samostatně posouzený návrh; úspěch se syntetickým klíčem je neřeší.

## Integrace do handoff

`evaluate_offline_worker_handoff(..., worker_oidc_token=None)` zachovává
existující kontroly aktivace, intentu, epochy, fronty, kreditů, kvóty, nákladů,
času a opakovaného čtení control stavu a konfigurace. Nový argument je pouze
podepsaný token, nikdy volba politiky nebo potvrzovací příznak identity.

- Při nepředání tokenu zůstává konečné odmítnutí
  `worker_execution_identity_unverified`.
- Po předání tokenu se na konci preflightu zavolá skutečný ověřovač proti
  controllerově konfiguraci a času vyhodnocenému po čtení zdrojů.
  Chybějící důvěra, neplatný podpis, expirovaný token či jakýkoliv nesoulad
  znamená `worker_oidc_identity_unverified`.
- I při skutečně úspěšném kryptografickém ověření syntetického tokenu je
  konečný výsledek **`allowed=False`**,
  `worker_execution_identity_unverified`, `durably_reserved=False`.
  Neprovádí se CAS, claim, spotřeba intentu, spuštění nebo publikování.

OIDC podpis potvrzuje, co issuer uvedl o Actions kontextu. **Neprokazuje,
jaký kód a konfigurace se skutečně vykonávají**: například další checkout,
změna souborů, závislostí, importů nebo již vytvořené instance může změnit
chování bez změny těchto claims. Token sám není attestation zdrojového nebo
sestaveného artefaktu ani propojení s neměnnou konfigurací skutečného procesu.
Není také jednorázovým claimem intentu ani důkazem billing/kill-switch stavu.
Opakovaný preview zůstává read-only DENY. Nová kladná větev zde není.

## Zachovaná kvalita a rozpočet

`worker_configuration.py`, `main.py`, `perplexity_run_budget.py`, závislosti,
lockfile, prompty, modely, CLI i workflows se nemění:

| Nastavení | Hodnota |
| --- | --- |
| Research reporty na otázku | **2** |
| Prognózy na report | **3** |
| Agent fáze na report | `initial`, `fact-check` |
| Agent požadavky na celou otázku bez retry | **4** |
| Automatický limit Agent požadavků | **2** |

Skutečný plán se nadále odmítá ještě před zdrojovými readery a OIDC
ověřováním: `worker_research_plan_exceeds_automatic_attempt_cap`.
Hypotetická jednoreportová testovací konfigurace izoluje ostatní kontroly;
ani v ní platný podepsaný token nevede ke spuštění. Není produkční změnou.

## Nové lokální důkazy a jejich omezení

Python **3.12.14**, bez síťových požadavků a bez instalace/stahování balíčků.
Lokální vyhledání wheelů nic odpovídajícího nenašlo. Oba samostatné příkazy
`pip install --dry-run --no-index --no-deps` pro zamčené verze skončily
`No matching distribution found`; závislosti se nezměnily.

| Kontrola v nové rekonstrukci | Výsledek |
| --- | --- |
| Existující autonomní offline integrace | **37/37 prošlo** |
| Existující `tools/test_*.py` | **19/19 prošlo** |
| Nových syntetických testů identity a handoff | **20/20 prošlo, pouze průzkumně** |
| Jeden společný aktuální běh | **76 prošlo, 0 failures/errors/skips** |
| Audit sítě společného běhu | **0 síťových pokusů** |
| Požadované PyJWT 2.10.1 + cryptography 45.0.4 | **nedostupné; kompatibilita neověřena** |
| Skutečně použité crypto knihovny průzkumného běhu | PyJWT **2.13.0**, cryptography **50.0.0** |
| Kompletní `tests.run --strict-known-bugs` | **blokován před discovery: chybí `requests`; 0 testů** |

Chybí také `forecasting-tools` a `openai`. Plná SDK sada ani interpretace
za běhu na Pythonu 3.11 se netvrdí jako ověřené. Samostatná kontrola gramatiky
Pythonu 3.11 a whitespace kontrola patchů jsou pouze statické kontroly.
Historických 76 testů z předchozího prostředí není důkazem této rekonstrukce;
zdejší stejné číslo pochází z nového společného běhu s uvedenými omezeními.

Samostatné příkazy z kořene checkoutu:

```bash
python -B tests/test_autonomous_runtime_offline.py
python -B tests/test_worker_identity_offline.py
python -B -m tests.run --strict-known-bugs
```

První dvě sady při přímém spuštění aktivují auditní zákaz sítě a selžou i při
zachyceném síťovém pokusu. Poslední příkaz má vlastní striktní offline runner,
vyžaduje ale SDK závislosti. V externích artefaktech je navíc lokální harness
`run_offline_checks.py` pro společné spuštění těchto 37 + 20 + 19 testů pod
auditním zákazem sítě, sanitizací credential proměnných testovacího procesu
a zákazem Internet socket sends. Neimportuje SDK ani balík `tests`, jehož
inicializace bez `requests` selhává. To nenahrazuje kompletní SDK regresi.

## Předání změny

Jediný kompletní `metaculus-vrchcab-oidc-offline-69d8d88.patch` se vztahuje
přesně k uvedenému výchozímu commitu a zahrnuje tyto čtyři soubory:
`worker_identity.py`, `autonomous_orchestrator.py`,
`tests/test_worker_identity_offline.py` a tento dokument.
Samostatné kontrolní součty SHA-256 a manifest patří k externím artefaktům,
aby patch ani dokument neobsahovaly vlastní rekurzivní hash.
Při předání se kontroluje čistá aplikace na export základního Git stromu,
shoda výsledného stromu a souborů, reverzní aplikace a samostatný offline
běh z aplikované kopie. Podrobnosti a skutečné výsledky jsou v manifestu
a přiložených lokálních protokolech.

Neproběhl push, PR, merge, změna workflow, živý API požadavek ani placená
operace. Nevznikl skutečný control claim, funkční worker ani oprávnění ke
spuštění. Připravenost produkční automatizace ani plně vybaveného SDK
prostředí se netvrdí.
