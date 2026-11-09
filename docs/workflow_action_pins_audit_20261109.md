# Offline audit všech 15 workflowů – PRODUCTION DENY

Základ: `85545c531c9725661cc4c4346912e393a743fb77`.
Nový detached worktree: `/workspace/vrchcab-action-pins-85545c5`.
Žádný dřívější patch se neaplikoval. Změny jsou jen lokální k revizi.

## Nezávisle ověřené SHA a původ

Oficiální HTTPS Git repozitáře byly čteny 9. 11. 2026 přibližně
22:03:12–22:03:15 Europe/Prague (21:03 UTC). U každé akce:
`git ls-remote --tags` → fetch major i release tagu → objekt typu commit →
přepočet Git SHA-1 objektu → `git fsck --strict` → kontrola action.yml blobu →
opětovné ls-remote obou tagů. Oba tagy měly stejný níže uvedený commit.
Žádný SHA nebyl převzat z paměti, cizího workflow nebo forku. Kód akcí nebyl spuštěn.

| Oficiální repozitář / commit | Ověřená shoda tagů | action.yml Git blob |
| --- | --- | --- |
| [actions/checkout@11d5960a326750d5838078e36cf38b85af677262](https://github.com/actions/checkout/commit/11d5960a326750d5838078e36cf38b85af677262) | v4 = v4.4.0 | 24e73e5a12126edc2adb9e5cd1bf245ce85bde56 |
| [actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065](https://github.com/actions/setup-python/commit/a26af69be951a213d495a4c3e4e4022e16d87065) | v5 = v5.6.0 | efa8de904209196588db1453bdb44079b3c393d7 |
| [actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02](https://github.com/actions/upload-artifact/commit/ea165f8d65b6e75b540449e92b4886f43607fa02) | v4 = v4.6.2 | 2a0ecf19e8d0087dd2e5d1785dcf764811e79fae |
| [snok/install-poetry@a783c322200f0519c7926aa6faa857c4e23e9263](https://github.com/snok/install-poetry/commit/a783c322200f0519c7926aa6faa857c4e23e9263) | v1 = v1.4.2 | 80ad15c818c7f94e4aad60e279284b67b1d5fc40 |

První tři action.yml používají node20; snok je composite spouštějící main.sh.
Původ je ověřen přes oficiální Git/TLS a integritu objektů, nikoliv nezávislou
GPG/Sigstore autentizací autorů nebo úplným auditem JS kódu. Připínají se
aktuální cíle dosavadních major tagů, nemění se major/runtime. Historické
rozlišení pohyblivého tagu v dřívějším běhu nelze bez jeho metadat doložit.

## Přesné položky pro budoucí Actions allowlist

```text
actions/checkout@11d5960a326750d5838078e36cf38b85af677262
actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065
actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02
snok/install-poetry@a783c322200f0519c7926aa6faa857c4e23e9263
```

Před nasazením musí repo/org/enterprise policy tyto čtyři reference povolovat.
Pokud je již pokrývá širší pravidlo, duplicitní položka není nutná. Povolení
jen @v4/@v5/@v1 nemusí povolit SHA. Existující pravidla se zde neodstraňují.
Efektivní Settings/allowlist nebyly načteny ani změněny; bez jejich kontroly
nelze zaručit přijetí běhu. Jde o seznam k samostatnému schválení, ne provedený zásah.

## Inventura všech workflowů

C/S/P/U = počty checkout/setup-python/install-poetry/upload-artifact. Všechny
aktivní uses v repozitáři patří těmto čtyřem akcím; celkem 17/17/11/4 = **49**.

| Soubor .github/workflows/ | Vstup / zachovaná hranice | C/S/P/U |
| --- | --- | --- |
| initialize_publication_state.yaml | Manual, contents:write; explicitní legacy freeze, společný publication lock | 1/1/1/0 |
| manual_github_queue_diagnostic.yaml | Manual approval/attempt1, contents+actions:read, omezené GET | 1/1/0/0 |
| manual_one_post_history_readonly.yaml | Manual approval/attempt1, contents:read, jediný povolený post GET | 1/1/0/0 |
| metaculus_history_audit.yaml | Manual, contents:read, checkout starého c2b5b01… zachován | 1/1/0/1 |
| offline_bounded_pagination.yaml | Pull request, contents:read, offline safety | 1/1/1/0 |
| offline_integration_check.yaml | Manual, contents:read, existující historické ref/fetch/copy zachovány | 1/1/1/1 |
| offline_publisher_approval_guard.yaml | Pull request, contents:read, default-deny regressions | 1/1/0/0 |
| offline_release_check.yaml | Manual candidate SHA, contents:read, offline check/artifact | 1/1/1/1 |
| readonly_inventory_preflight_manual.yaml | Dva joby, manual approval/attempt1, offline před GET | 2/2/2/0 |
| review_bot.yaml | Manual/weekly variable gate, contents:read, externí bot-review CLI | 1/1/1/1 |
| run_bot_on_metaculus_cup.yaml | Manual publication approval/attempt1, contents:write, live legacy cesta | 1/1/1/0 |
| run_bot_on_tournament.yaml | Manual publication+research approval/attempt1, contents:write, single post | 1/1/1/0 |
| test_bot.yaml | Manual publication approval/attempt1, contents:write; přes název publikuje | 1/1/1/0 |
| test_post_rotation_secret_isolation.yaml | Pull request, contents:read, offline token-isolation guard | 1/1/1/0 |
| watch_open_metaculus_questions_readonly.yaml | PR/manual/15min schedule, contents:read; PR offline, monitor GET | 2/2/0/0 |

Žádný pull_request_target ani nové oprávnění/trigger nepřibyly. U všech15
workflowů jsou celé parsed YAML stromy shodné po normalizaci pouze uses refů;
zpětná textová náhrada pinů vrací přesně původní bytes. Nezměnily se run,
with/ref, env/secrets bindings, if/approval, concurrency, timeout, paths filtry,
artifact parametry ani Python3.11. Zakomentované actions/cache@v4 zůstává neaktivní.

Ostatní rizika se tím neřeší: review checkout dál používá původní default
persist-credentials a externí integrations CLI zde nebylo spuštěno; starý
c2b5b01… history target není tímto auditem validován. ubuntu-latest a Poetry
version=latest zůstávají pohyblivé. Ověřený snok main.sh již pinuje installer
skript na d7fd0502ef807e711f65204b1ab39c1a1e23c69c; jeho původ samostatně
ověřován nebyl a není to další Actions allowlist položka. Pinování akcí samo
neprokazuje bezpečnost dependencies, efektivních práv ani live publisheru.

## Skutečné místní ověření

- Plný tests.run --strict-known-bugs: **718/718**, včetně všech původních717
  a jedné nové bezpečnostní regrese všech aktivních pinů. Původní credential
  persistence test pouze rozpoznává checkout podle názvu místo major tagu.
- Tools samostatně **19/19**, synthetic botocore lab samostatně **6/6**.
- Všechny běhy: **0 failures/errors/skips**, 0 known failures/unexpected
  successes; **0 unexpected network**, také outer audit. Nejde o GitHub CI.
- Python3.12.14, ověřeno112 použitelných main lock pinů včetně PyJWT2.10.1,
  cryptography45.0.4; SDK lab botocore1.43.126. Žádná instalace. Python3.11
  runtime, Node akce ani živý GitHub runner se zde netestovaly. Pouze lokální
  urllib3 IPv6 capability probe potlačena, síťové guardy zůstaly aktivní.

Nezbytné read-only Git čtení oficiálních repozitářů pro ověření SHA proběhlo
odděleně před offline testy; nejde o tvrzení nulové sítě celé relace. Produkční
API, dispatch, forecast/publikování ani GitHub write se neprováděly. Původní
None kotvy, execution DENY, plán2×3/4 Agent POST a automatický limit2 beze změny.
Patch mění jen15 workflowů, jeden test a přidává tento dokument. PRODUCTION DENY.
