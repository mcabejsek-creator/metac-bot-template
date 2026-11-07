# Vrchcab — rozhodnutí o bezpečném přechodu po rotaci tokenu
*Stav kontroly: 2026-11-07. POUZE návrh rozhodnutí; žádný souhlas s inicializací ani publikováním není udělen.*

## Co jsme ověřili

1. Nový token Vrchcab `METACULUS_TOKEN_V2` vrací HTTP 200 a správnou identitu bota 307558; starý `METACULUS_TOKEN` na identitním endpointu po rotaci vrací HTTP 403. Webové potvrzení Metaculus deklarovalo zneplatnění původního klíče. Rotace je **doložena**, aniž by byly zveřejněny hodnoty tokenů.
2. [Starý běh 34463156990](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/34463156990) je stále `queued`, z 2026-09-10; přes GitHub API k němu bylo 2026-11-07 ověřeno **0 jobs**. GitHub Support ticket #4966035 čeká na věcné vyjádření. Podpora není totožná s povolením nového publikování.
3. Po merge [PR #3](https://github.com/mcabejsek-creator/metac-bot-template/pull/3) je `main` na `e52f5e73411868c07e2e3b8c301e1b05fba6113e`. Všechny tři publikační workflow mají jen **ruční workflow_dispatch**, nikoli `schedule` nebo `push`, a přistupují pouze k `METACULUS_TOKEN_V2`. Externí spouštění cron-job.org zůstává vypnuté podle informací uživatele. Čtecí review má plánovač, ale jeho job vyžaduje opt-in `REVIEW_BOT_ENABLED`.
4. [Poslední offline CI](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/42376087887) prošlo: 5/5 kontrol izolace tokenu, 273/273 regresí, žádný nečekaný živý pokus.
5. Čtyři ověřené projekty: FutureEval 111 záznamů na 111 otázkách, MiniBench 60 na 60, testovací `bot-testing-area` 780 záznamů na 12 otázkách, Fall Metaculus Cup 0. Rozsah je omezen na tyto projekty a data dostupná botu; nelze z něj odvodit úplnou globální historickou inventuru. Testovací režim záměrně opakoval publikace.
6. Stavová větev `metaculus-publication-state` při kontrole **neexistuje**. Existující `github_publication.py` je fail-closed a neumí bez výslovného provozního potvrzení automaticky inicializovat nový žurnál.

## Dvě oddělené otázky

**A. Starý zaseknutý GitHub run:** Support jej může administrativně odstranit, ale dokud nemá přístup k platnému tokenu, neměl by mít oprávnění publikovat na Metaculus. Úspěšná rotace tento bezpečnostní aspekt oddělila od kosmetického/administrativního `queued` stavu. To je bezpečnostní úsudek ze známých důkazů, nikoli tvrzení o serverovém interním stavu GitHubu.

**B. Historické výsledky publikací:** Nový prázdný žurnál neobsahuje dřívější ack, forecast ani comment. Neznáme výsledek každé možné dřívější zprávy nebo částečného odeslání. GitHub Support tuto aplikační nejistotu sám nevyřeší.

## Konzervativní provozní rozhodnutí, které by umožnilo přechod

Toto je **návrh**, nikoli už provedený krok:

- Všechny staré workflow a minulá run ID **trvale vyřadit z opakování**; žádný `Re-run jobs`, žádný pokus o zpětné odesílání historických forecastů nebo komentářů.
- Historické existující předpovědi ponechat za Metaculus jako autoritativní záznam. Neoznačovat neověřené historické komentáře za doručené ani je automaticky doposílat. Známá otevřená otázka bez historické předpovědi může být novou intent pouze v `tournament` režimu.
- První živý režim **jen `tournament` (FutureEval 33121 + MiniBench)** s `skip_previously_forecasted_questions=True`. Ruční `metaculus_cup` a `test_questions` i externí cron-job.org ponechat vypnuté. Před publikováním musí proběhnout preflight a nové atomické žurnálové uložení.
- Před žurnálem ověřit, že starý token je revokován, žádný jiný publisher s platným tokenem neběží a nejsou otevřené nové writers. Při jakékoli nejistotě publikování zastavit.
- Po samostatném výslovném souhlasu vytvořit novou šifrovanou stavovou větev a ověřit její trvalé uložení. Po *dalším* souhlasu spustit jeden kontrolovaný živý běh.

**Rozpor k vyřešení před inicializací:** Stávající UI inicializátoru vyžaduje `all-prior-runs-reconciled`. Agregátní GET historie samy o sobě toto tvrzení **neprokazují**. Operátor musí výslovně rozhodnout, zda výše popsané trvalé ukončení a neobnovení veškerých starých pokusů představuje přijatelnou konzervativní rekonciliaci. Pokud ne, NESMÍ potvrzení zadávat — nejprve je nutné další zkoumání nebo upravený bootstrap postup s přesným, neklamavým potvrzením `legacy-frozen-no-retries`.

## Stav při vytvoření tohoto souboru

**Go for offline preparation; NO-GO for automatic publishing.** Žádný nový Metaculus GET, POST, iniciační zápis, restart cron ani pozměnění `main` nebylo součástí tohoto dokumentu.

## Inicializace nového žurnálu — provedena a ověřena 7. 11. 2026

Uživatel samostatně schválil **pouze** vytvoření nového šifrovaného GitHub žurnálu, bez Metaculus publikování. Po nasazení PR #4 do `main` (`f77f2329e9c09e787333416300ae35fa02f240a0`) byl spuštěn jednorázový [GitHub Actions běh 42378368496](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/42378368496), jehož checkout byl pevně ukotven na této SHA. Použit pouze tajný `METACULUS_TOKEN_V2`; hodnoty tokenů nebyly zveřejněny.

- **5/5** offline kontrol tajemství a **278/278** regresních testů, **0 unexpected live attempts**.
- Větve `metaculus-publication-state` dříve nebylo; API inicializátoru provedlo pouze vytvoření nového orphan refu s jedním šifrovaným souborem `publication.enc`.
- Následný `configured_store().preflight()` úspěšně autentizoval a dešifroval manifest. Při nezávislé GitHub kontrole: ref `02e324f9045952ed00b0a1bb5058ded4d39178e6`, samotný soubor **239 bajtů**, JSON obálka se `version=1` a polem `data` (ciphertext). V Git větvi nejsou žádné workflow ani jiné zdrojové soubory a commit je orphan bez rodičů.
- Jednorázový spouštěcí YAML a marker byly po provedení odstraněny z dočasné ops větve. `main` se nezměnil; starý run `34463156990` je stále `queued`; žádné další aktivní jobs nejsou.
- **Pozor:** GitHub označuje stavovou větev jako `protected: false`. Před živým provozem je vhodné nastavit pravidlo proti mazání a force-pushi stavové větve, **aniž by zablokovalo přímé checkpoint commity GitHub Actions**. Tento krok zatím proveden nebyl.
- **Žádný ostrý forecast, komentář, modelová API útrata ani obnovení cron-job.org nebyly autorizovány nebo spuštěny.**

Další věcný milník: ochrana stavové větve a potom samostatně schválený, sledovaný jednorázový `tournament` run. Po dobu dalších příprav udržovat `Test Bot`, `Cup` a externí trigger vypnuté. Historické nedoložené pokusy zůstanou trvale neobnovené, nový šifrovaný manifest výslovně přiznává `not-fully-reconciled`.
