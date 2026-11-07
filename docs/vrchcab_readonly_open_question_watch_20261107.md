# Vrchcab — automatická čtecí kontrola otevřených otázek (návrh)

Stav: 7. listopadu 2026. **Připraveno jen na samostatné vývojové větvi; bez merge do `main`, bez aktivního rozvrhu.**

## Co přesně hlídač dělá

- Pokud bude návrh schválen a sloučen do `main`, GitHub Actions může spouštět čtecí kontrolu každých cca **15 minut** (GitHub cron: `7,22,37,52 * * * *`, UTC). GitHub negarantuje přesné spuštění na minutu; může docházet ke zpožděním.
- Každá kontrola má nejvýše dva autentizované, výhradně čtecí GET požadavky `/api/posts/`, po jednom pro **Fall FutureEval (33121)** a **MiniBench (minibench)**, s filtrem `statuses=open` a prvním stránkovaným seznamem 100 příspěvků. Mezi dotazy je odstup minimálně 6 sekund; bez retry, bez přesměrování, bez dalších URL.
- Používá pouze existující GitHub secret `METACULUS_TOKEN_V2` jako hlavičku **Authorization** při čtení Metaculus. Jeho hodnotu nezapisuje ani neposílá jinému serveru. Nepoužívá žádné placené modelové API a nepřidává předpovědi nebo komentáře. GitHub workflow má **`permissions: contents: read`**, nepoužívá publikační žurnál a v PR offline testech nemá žádný secret.
- V GitHub Actions se vypíše pouze počet **potenciálně relevantních příspěvků na nejnovější první stránce**, zvlášť počet vyřazených nepodporovaných *notebook* položek. Při pozitivním nálezu bude **varování** (workflow annotation) a shrnutí. Nepřenáší se ID, názvy nebo prognózy.
- **První stránka není kompletní inventář.** Živá diagnostika [42383629570](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/42383629570) zjistila, že API vrací další stránku `offset=100` i když má první stránka 0 výsledků; [42383816064](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/42383816064) ověřila na druhé stránce obou projektů 0 položek, ale další `offset=200` zůstal přítomen. Hlídač proto **výslovně sleduje jen nejnovější stránku** (což je vhodné k zachycení nově publikovaných příspěvků) a nikdy netvrdí, že na starších stránkách nejsou další otázky.
- **Potenciálně relevantní příspěvek není automaticky otázka, kterou bot ještě nepředpověděl.** Hlídač nezjišťuje osobní historii bota; každé varování vyžaduje následné bezpečné ověření a zvláštní schválení ostrého běhu.
- Pokud API vrátí chybu, HTTP 429 nebo změněné JSON schéma, kontrola skončí neúspěchem s **nejistým výsledkem**. Samotná existence odkazu `next` již neblokuje hlídač, protože reportuje pouze první stránku; další stránky výslovně označuje za nekontrolované.
- Na pull requestech se spouští **pouze offline syntax/request-contract a syntetické testy**. Autentizovaný GET na Metaculus proběhne teprve při ručním `workflow_dispatch` nebo rozvrhu na `main`. Původní předpoklad, že postačí veřejné GET bez tokenu, byl vyvrácen živým testem: [test 42382715942](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/42382715942) skončil HTTP 403 **bez tokenu**, tedy bez jakéhokoli přístupu k soukromé identitě bota.

## Upozorňování a jeho hranice

GitHub repo aktuálně vykazuje `has_issues=false`, takže nelze využít standardní upozornění prostřednictvím nového Issue. Tento read-only návrh proto **sám o sobě neslibuje doručení push/e-mail notifikace**. Pozitivní nález je viditelný v GitHub Actions (warning + job summary). Pokud má přijít oznámení přímo do ChatGPT, vhodný samostatný plán je přidat hodinovou podmíněnou automatizaci, která sleduje výsledky běhů a hlásí pouze novou změnu. ChatGPT task nepoběží častěji než jednou za hodinu.

## Co zůstává nedotčeno

- Starý GitHub workflow run `34463156990` je stále `queued` (vyřízení s GitHub Support #4966035 je samostatné).
- Produkční opravy jsou v `main` SHA `3d54363f49768147606cde16698920ac514f29a6`; žurnál `metaculus-publication-state` je chráněný a další ostrý POST ještě nebyl odzkoušen.
- Externí `cron-job.org` zůstává vypnutý. Tento návrh ho nezapíná ani nespouští `run_bot_on_tournament.yaml`, Cup či Test Bot.
- Aktivace rozvrhu vyžaduje **samostatné schválení merge**; publikování bude mít další, oddělené schválení.

## Jak ověřit

Otevřít `Actions → Vrchcab authenticated open-question watch (read only)`. Ručně spustit pouze tento čtecí workflow, nebo po merge sledovat rozvrh a souhrny. Nikdy nevyhodnocovat červený výsledek (HTTP chybu) jako nulovou dostupnost ani úspěšnou prázdnou první stránku za důkaz, že jinde v turnaji nejsou otevřené otázky.
