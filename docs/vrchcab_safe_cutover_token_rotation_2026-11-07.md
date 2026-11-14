# Vrchcab: bezpečný postup znovuspuštění (návrh, 7. listopadu 2026)

**Tento dokument neopravňuje k obnovení publikování.** Žádný token, produkční běh ani publikační žurnál se tímto dokumentem nemění.

## 1. Proč nestačí jen změnit GitHub Secrets

GitHub čte repository/organization secrets už **při zařazení běhu do fronty** (podle [oficiální dokumentace](https://docs.github.com/en/actions/reference/security/secrets)). Zaseknutý běh [34463156990](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/34463156990) ze dne 10. září 2026 tedy **může obsahovat původní token** i po jeho nahrazení v GitHub Secrets. Není bezpečné předpokládat opak.

Bezpečná alternativa při nedostupnosti GitHub Support je ověřené zneplatnění původního tokenu **přímo u Metaculus** a vytvoření nového, odlišného tokenu. Není zatím potvrzeno, jak přesně aktuální rozhraní Metaculus v daném účtu provádí regeneraci/revokaci. Je nutné při uživatelském kroku ověřit, že starý token již opravdu nefunguje. Rozhodnutí musí předcházet první nové publikaci.

## 2. Doporučený postup

1. Před rotací tokenu **neinicializovat** publikační žurnál, nespouštět žádný publishing workflow ani nevydávat neotestované změny.
2. V nastavení Metaculus [API Access](https://www.metaculus.com/accounts/settings/account/#api-access) zjistit dostupný postup revokace/regenerace. Nikdy nevkládat token do chatu, issue, PR, kódu nebo logů.
3. Zneplatnit původní token u poskytovatele a **ověřit serverové zneplatnění**. Pouhá změna tajemství na GitHubu nestačí. Pokud není možné prokázat zneplatnění, cestu rotace nepovažovat za dokončenou a pokračovat přes GitHub Support.
4. Nový token uložit jako oddělené GitHub Actions secret, návrh názvu `METACULUS_TOKEN_V2`. Staré jméno `METACULUS_TOKEN` nesmí žádné **nové** publishing workflow používat.
5. Všechny publikující workflow v PR #1 (`run_bot_on_tournament.yaml`, `run_bot_on_metaculus_cup.yaml`, `test_bot.yaml`) a `initialize_publication_state.yaml` musí **explicitně** mapovat nový secret do běhové proměnné `METACULUS_TOKEN`. Změna se musí plně otestovat v odděleném PR před merge.
6. Provedení historického auditu je samostatný krok. Schválený autorizovaný GET seznamu **aktuálně otevřených** otázek z 7. 11. není náhradou historie. Připravený, ale **nespuštěný** skript `tools/project_history_inventory_readonly.py` dokáže souhrnně načítat historické předpovědi na dvou konkrétních projektech pomocí dokumentovaného `GET /api/questions/bulk-forecast-read/?project=...`. U účtu Vrchcab používá `acting_user=307558`. Před jakýmkoli dalším živým dotazem je potřeba samostatný souhlas a respektování limitů HTTP 429.
7. Při rekonciliaci odlišit potvrzené publikace, nejasná zrušená spuštění a možné duplicitní pokusy. **Agregovaný počet nestačí** jako důkaz, že každá nejistá operace byla vyřešena.
8. Teprve po splnění předchozích podmínek, úplné kontrole vstupních bodů, offline integračním testu a výslovném schválení inicializovat **nový** publikační žurnál.
9. Jediný supervisovaný první publish a až následně obnovit automatické spouštění.

## 3. Kritická vazba žurnálu na token

V opravách PR #1 soubor `github_publication.py` odvozuje šifrovací klíč publikací z `METACULUS_TOKEN` pomocí HKDF. Když by se token změnil **po inicializaci**, dříve zapsaný žurnál by se nemusel dát dešifrovat a systém bezpečně odmítne další běhy. Rotaci proto plánovat **před** vytvořením žurnálu, nikoli po něm. Záložní přístup k původnímu klíči nesmí být vystaven v logu.

## 4. Ověřené podklady a omezení

- [GitHub Secrets reference](https://docs.github.com/en/actions/reference/security/secrets): načtení secretů při queueingu.
- [Metaculus API](https://www.metaculus.com/api/): GET **i POST** na `bulk-forecast-read` jsou dokumentované; `project` podporuje čtení vlastní historie v rámci projektu; server může uplatňovat přístupové limity.
- [Forenzní zpráva](./vrchcab_publication_reconciliation_2026-11-07.md): dva doložené publikační eventy a nejasné zrušené běhy.
- [Opravný PR #1](https://github.com/mcabejsek-creator/metac-bot-template/pull/1) není nasazen; [diagnostický PR #2](https://github.com/mcabejsek-creator/metac-bot-template/pull/2) je draft.
- **Nevíme**, zda změna tokenu v aktuálním Metaculus účtu skutečně zruší předchozí credential, ani zda bude přes dotaz `project` dostupná celá historie. Bez ověření nelze tyto body deklarovat jako splněné.

**Žádný krok v tomto dokumentu se automaticky nespouští.**

## Ověření po rotaci (provedeno 2026-11-07)

Uživatel potvrdil, že přes rozhraní Vrchcab provedl rotaci původního API klíče a do GitHub Actions uložil nový pod jménem `METACULUS_TOKEN_V2`. **Hodnoty tokenů nejsou nikde v tomto dokumentu.**

- [Před rotací: identita původního GitHub tokenu](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/42371618440): 2026-11-07 12:14 UTC, HTTP 200 na `GET /api/users/me/`, identita `BOT_VRCHCAB`.
- [Po rotaci: jednorázový test obou GitHub secrets](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/42372668966): 2026-11-07 12:29 UTC, nový secret `METACULUS_TOKEN_V2` vrací HTTP 200 se správnou identitou `BOT_VRCHCAB`; starý `METACULUS_TOKEN` vrací HTTP 403 na **stejné** identitní adrese.
- Kód sám klasifikoval starý HTTP 403 konzervativně jako **neprůkazný pro všechny ostatní API operace**, nikoli jako úspěšnou bezvýhradnou revokaci. V kontextu potvrzovacího dialogu Metaculus („zneplatní aktuální API klíč“) a předchozího 200 pro původní token jde o silný důkaz změny autentizace; nikoli o povolení k ostrému publikování.
- Jednorázový workflow i spouštěcí marker byly z testovací větve odstraněny. Produkční `main` a PR #1 zůstávají bez merge, žurnál nebyl inicializován, starý GitHub běh z 10. 9. je nadále ve frontě.
- Dále zbývá provést omezený čtecí audit historických předpovědí s novým tokenem (vyžaduje samostatné schválení), vyhodnotit nejasné historické pokusy, připravit integraci PR #3 do PR #1, offline ověřit všechny vstupy a teprve poté výslovně povolit inicializaci žurnálu.

