# Bezpečnostní aktualizace 7. 11. 2026

Uživatelský souhlas z 7. listopadu uzavřel staré nejasné operace metodou **legacy-frozen-no-retries**, nikoli retrospektivním potvrzením každého POST. [Podrobný plán](legacy-frozen-bootstrap-2026-11-07.md) má v otázce inicializace přednost před staršími historickými pokyny níže. Kód v `main` již používá `METACULUS_TOKEN_V2` a opravy z PR #3. **Žurnál dosud nebyl založen; živé publikování není povoleno.**

# Bezpečné zavedení do GitHub Actions

Příprava dne 2026-11-06 (Europe/Prague), větev `prep/github-actions-rollout`,
aplikační základ `a53bc60`. Žádná změna aplikačního kódu, aktivních workflow,
prognostiky, modelů, parametrů ani Secrets. Živý bot nebyl vypnutý. Neproběhl
push, merge, dispatch Actions, inicializace ani publikace prognóz.

## Co je ověřeno

| Oblast | Důkaz / omezení |
| --- | --- |
| Produkční main | Při autorizovaném čtení GitHubu stále `91bf94b167b5d96f5e96616b613d757173208f5f`; proti předchozímu auditu žádná další změna. Jeho novější MC odstavec zůstává v kandidátovi přesně zachovaný. Před budoucím přepnutím ověřit main znovu. |
| Stavová větev | `git ls-remote` při témže čtení nevrátil `metaculus-publication-state`. Zde nebyla vytvořena. Před inicializací znovu ověřit; dříve existující ztracený stav se nesmí nahradit prázdným. |
| Python | Skutečný CPython **3.11.17**, samostatné prostředí `/workspace/scratch/metac-python311`; původní `.venv` s Pythonem 3.12 zůstalo zachováno. |
| Závislosti | Poetry **2.4.1**, `poetry check --lock` bez chyby, instalace 136 balíčků z nezměněného lockfilu. `uv pip check` bez chyb; ověřené verze SDK, cryptography, requests, NumPy, OpenAI a PyYAML odpovídají locku. |
| Strict testy na 3.11 | **273/273**, 0 failures/errors/expected failures/skipů, 0 neočekávaných živých pokusů. |
| Simulované Actions na 3.11 | **273/273** v strict režimu, stejný výsledek. Obsahuje nezávislé fake runnery, CAS, reruny, cancel, žurnál v1→v2 a PUB-MODE-1. |
| Kandidátská workflow | Tři publishery a inicializátor mají stejný repo-wide lock, scope, cestu a větev; `cancel-in-progress: false`, explicitní limity, jednopokusové POSTy, žádný lokální fallback v Actions. Tyto soubory se v tomto kroku nemění. |
| Reálná oprávnění a běžící joby | **Neověřeno:** GitHub CLI se nedokáže přihlásit dostupným `GH_TOKEN`. Z YAML nelze potvrdit povolení organizace, rulesets, skutečné Secrets ani stav starých běhů. Ověří vlastník v přihlášeném UI. Tokeny není potřeba posílat do chatu. |

Po posledním pokynu k pokračování nebyla provedena žádná další živá API volání;
jen kontrola lokálních souborů a dokončených výsledků. Předchozí autorizovaná
síťová práce v tomto úkolu byla Git fetch/ls-remote a stažení Pythonu/balíčků,
nikoli volání Metaculus, Perplexity či OpenRouter.

Lokální výsledky: `/tmp/metac-python311-strict.json`,
`/tmp/metac-python311-actions.json`; souhrn s hashem lockfilu je v
[python311-verification.json](python311-verification.json). Reprodukce:

```bash
/workspace/scratch/metac-python311/bin/python -B -m tests.run --strict-known-bugs --report /tmp/metac-python311-strict.json
GITHUB_ACTIONS=true GITHUB_REPOSITORY=offline-owner/offline-repo \
  GITHUB_RUN_ID=4242 GITHUB_RUN_ATTEMPT=2 METACULUS_PUBLICATION_BACKEND=github \
  /workspace/scratch/metac-python311/bin/python -B -m tests.run --strict-known-bugs --report /tmp/metac-python311-actions.json
```

## První ověření na GitHubu bez publikování

[offline-release-check.yaml](offline-release-check.yaml) je **neaktivní lokální
šablona v docs/**. Současný bot ani jeho plánovač ji nepoužívají. Po samostatném
schválení může být jako jediný CI soubor přidána do defaultního main pod cestou
`.github/workflows/offline_release_check.yaml`. V tomto malém kroku se nemerguje
celá větev s botem a nemění se žádný stávající publisher.

Nový manual-only job má pouze `contents: read`, nepřebírá žádné Metaculus/model
Secrets, nesdílí publikační concurrency lock a nespouští produkční CLI. Checkout
je připnutý na povinnou plnou 40znakovou SHA schváleného kandidáta. Spustí na
Pythonu 3.11 jen `tests.run --strict-known-bugs` a uloží neobsahující klíče
`offline-results.json` jako artefakt. Výsledky mají 273 passed a všechny chybové
seznamy prázdné. Validace SHA před checkoutem odmítá názvy větví a shellové vstupy.

Workflow s `workflow_dispatch` musí být přítomné na defaultní větvi, aby bylo
možné spolehlivě spustit jeho dispatch. Kandidátská SHA musí být na GitHubu dostupná
po zvlášť schváleném pushi pracovní větve. Tento postup lze provést ještě při
běžícím starém botovi: ověřovací workflow se účtuje pouze jako CI a nepoužívá
produkční lock ani backend. Dispatch i upload artefaktu jsou ovšem skutečné
operace na GitHubu, proto je zde bez souhlasu neprovádíme.

Plná SHA zde ověřeného aplikačního základu je
`a53bc607ef170efeb5912434b53b9a99d22770e1`. Pro každou novější kandidátskou SHA
znovu ověřit diff a testy; volbu automaticky nepřesměrovávat na pohyblivý main.

Zelený job dokazuje offline chování skutečného kandidáta na GitHub runneru;
**nedokazuje** přijetí předpovědi Metaculus ani funkčnost živých modelových klíčů,
kreditů nebo soukromých API. Inicializátor později ověří skutečné GitHub zápisy
bez publikování na Metaculus. Skutečné přijetí forecast/comment vyžaduje až
samostatně schválený živý běh.

Existující `Test Bot`, `--mode test_questions`, tournament i Cup **publikují**.
Nesmějí být použity jako tento nepublikující smoke test. Produkční CLI nemá
ověřený přepínač `--dry-run`; žádný nový režim se nepřidává. `publish=False` by
navíc obešlo publikační žurnál a samo by neověřilo jeho chování.

## Kdo provede jednotlivé kroky a kdy je potřeba souhlas

„Ano“ znamená další výslovný souhlas před mým provedením; při práci vlastníka
v UI je souhlasem jeho vědomá akce. Žádný z budoucích kroků se neprovádí automaticky.

| Krok | Konkrétní akce a kdo ji může udělat | Souhlas |
| --- | --- | --- |
| 1. Místní kontrola | **Já, hotovo:** porovnání načteného main, Python 3.11, oba strict běhy, kontrola kandidátských YAML a příprava šablony. Mohu dále číst místní soubory a exportované výsledky. | **Ne**, již zadáno. |
| 2. GitHub nastavení | **Vy v UI:** ověřit Actions policy, povolené použité actions, rulesets a aktuální běžící/queued runy; viz checklist níže. Já mohu asistovat z dodaných výsledků, nové API čtení jen se souhlasem a funkčním přístupem. | UI čtení **ne**; nové API čtení mnou **ano**. |
| 3. Zpřístupnění offline kontroly | **Vy**, případně já s funkčním přístupem po souhlasu: push schváleného kandidáta pouze do pracovní větve a samostatně přidat **pouze** ověřovací YAML do main. Žádný merge celého bota v této fázi. | **Ano**, push a CI-only změna main zvlášť schválené. |
| 4. Offline Actions | **Vy / já po souhlasu:** Actions → Offline release check → Run workflow; zadat plnou schválenou SHA. Ověřit SHA v logu, Python 3.11, zelený výsledek a JSON artefakt. Živý bot stále běží beze změny. | **Ano**, spuštění skutečného Actions jobu. |
| 5. Pauza starých writerů | **Vy / já po zvláštním souhlasu:** zakázat nové běhy všech tří publisherů, odstranit queued staré běhy, nechat aktivní doběhnout; při nutném cancelu vyjasnit nejasné POSTy. Zastavit také lokální/jiné repo writery stejného účtu. | **Ano**, výslovně zásah do živého provozu. |
| 6. Přepnutí kódu | **Vy / já po souhlasu:** znovu ověřit aktuální main; zachovat novější změny, případné konflikty rozhodnout s vámi. Teprve při vypnutých publisherech zavést celý schválený celek, ověřit finální SHA a offline testy. | **Ano**, plný merge / nasazení kódu. |
| 7. Inicializace | **Vy / já pouze po dalším výslovném souhlasu:** při neexistující stavové větvi spustit `Initialize publication state` s `legacy-frozen-no-retries`. Tento podpis znamená **trvale neopakovat staré nejasné pokusy**, nikoliv tvrdit, že jsou všechny historické výsledky známé. Předpokládá bezpečnostně uzavřené staré writery, nové stabilní V2 tajemství a prošlé offline regresní testy. | **Ano**, nový šifrovaný zápis do GitHubu; **bez** forecastu na Metaculus. |
| 8. První skutečné publikování | **Vy / já po novém výslovném souhlasu:** povolit jen tournament a spustit jeden ruční běh z ověřené SHA. Cup/Test zatím vypnuté. Ověřit nové confirmed checkpointy a forecast/comment na Metaculus. Jde o původní oba turnaje, ne canary jediné otázky. | **Ano**, zvlášť souhlas s reálnými API a publikováním. |
| 9. Obnovení další automatiky | **Vy / já po souhlasu:** po vyhodnocení teprve povolit schválené další publishery. Cup zachovává vlastní cron a nové samostatné run ID záměrně přeforecastovává. Test Bot také publikuje. | **Ano**, obnovení živých běhů / automatiky. |

## Checklist vlastníka pro krok 2 a 5–7

- **Settings → Actions → General:** Actions a použité akce jsou povolené.
  Workflow explicitně žádají `contents: write` pro publishery/inicializátor;
  organizace nesmí toto oprávnění blokovat. Offline checker žádá jen read.
  Není potřeba PAT ani povolení vytvářet pull requesty. Defaultní read-only nastavení
  nepřepínat bezdůvodně, ověřit možnost explicitních práv workflow.
- **Settings → Rules → Rulesets / Branches:** main zůstane chráněný. Stavová větev
  musí dovolovat přímé checkpoint commity od `GITHUB_TOKEN` bez povinného PR/status
  checku. Zabránit mazání/force-pushi a omezit její správu. Práva tokenu nejsou
  obecně omezená jen na jediný soubor; konkrétní pravidla musí zkontrolovat vlastník.
- **Existující Secrets a billing:** stabilní skutečný `METACULUS_TOKEN` má alespoň
  32 znaků a patří stejnému účtu ve všech writerech; existující Perplexity a
- **Existující Secrets a billing:** platný `METACULUS_TOKEN_V2` patří účtu Vrchcab (ověřeno API GET); původní `METACULUS_TOKEN` byl po rotaci odmítnut (HTTP 403) a nesmí být v žádném publikujícím workflow. Šifrování nového žurnálu bude odvozeno z V2, takže **po inicializaci bez migračního plánu token již nerotovat**. Perplexity, OpenRouter a další placené zdroje se používají až při samostatně schváleném ostrém běhu.
  při cutover nerotuje: jeho změna mění také šifrovací klíč žurnálu.
- **Actions → všichni publishery:** tournament, Cup, Test Bot; zkontrolovat také
  běhy z jiných větví a starých SHA. Zakázání workflow samo neodvolá již probíhající
  POST. Cancelled/terminal status není důkaz, že požadavek server nepřijal. Reruny
  starých workflow SHA se už nepoužijí.
- **Vyjasnění historie:** prázdný nový index neimportuje staré úspěchy, částečné
  zápisy ani ztracená potvrzení. Potvrzovací volba inicializátoru je tvrzení
  operátora, nikoli kontrola Metaculus. Shodná hodnota/text či prázdná history
  nestačí pro automatické odblokování; pokud výsledek nelze určit, nepřepínat.
- **Stav v2:** existující v1 SQLite části se migrují automaticky při práci s nimi,
  nevyžadují nový prázdný žurnál. Šifrovaný index/envelope nadále používá formát
  v1; **SQLite v2** je uvnitř částí jednotlivých otázek. Číslo formátu indexu se
  ručně nemění. Ihned po inicializaci je přítomen jen `publication.enc`; části
  vzniknou při prvním uložení plánu. Zmizení již registrované části je chyba.

## Při selhání

1. **Offline checker selže:** do kroku 5 nepokračovat. Zkontrolovat artefakt,
   dependencies a konkrétní SHA; nic nepublikoval a starý bot může dál běžet.
2. **Inicializace selže nebo se ztratí ACK:** publishery nechat vypnuté, zkontrolovat
   existenci refu a oprávnění. Existující stav neresetovat, nemažte ho a
   nezakládejte jiný scope/path jako obcházení blokace. Žádné předpovědi neinicializuje.
3. **Živý běh selže:** zastavit další writery, uchovat nejnovější žurnál a vyjasnit
   konkrétní operaci. `sending/uncertain` se automaticky neopakuje. Confirmed
   forecast + rejected comment se obnoví bez modelu a bez nového forecastu, i mezi
   režimy. Cup/test rerun bez uloženého intentu se konzervativně blokuje; nová run
   ID nevytvářet jako slepé retry nejisté operace.
4. **Rollback:** nevracet žurnál spolu s kódem; SQLite v2 vyžaduje kompatibilní
   publikační kód. `0a98f97` ani starý main nejsou aktivní rollback target.
   Bezpečná výchozí reakce je pauza + oprava vpřed, nikoli start starého workflow.

Kód je offline ověřený na požadovaném Pythonu. **Souhlas s prvním živým během
zatím nebyl udělen; skutečná oprávnění a historické zápisy zůstávají podmínkami
aktivace.** Nepublikující kontrola nemůže prokázat exactly-once přijetí Metaculus;
nejasný výsledek zůstává karanténou s možným dopadem na dostupnost.
