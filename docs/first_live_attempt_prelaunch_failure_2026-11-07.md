# Vrchcab: první schválený ostrý pokus — zastavil se před spuštěním bota

Datum 2026-11-07, [GitHub Actions run 42379816283](https://github.com/mcabejsek-creator/metac-bot-template/actions/runs/42379816283).

- Spuštěn **jeden** předem uživatelem schválený běh, cílený jen na Fall FutureEval 33121 a MiniBench. Jednorázové workflow běželo na oddělené `ops/` větvi, ale checkout byl při běhu připnut na přesnou produkční SHA `f77f2329e9c09e787333416300ae35fa02f240a0`.
- Předem prošlo 13 bezpečnostních kontrol (validní chráněná stavová větev, stejný lock `metaculus-publication-${{ github.repository }}`, nový token, žádný další aktivní run).
- V běhu prošlo 5/5 tajemství testů a 278/278 offline regresí. Na chráněné větvi úspěšně proběhl čistě čtecí `configured_store().preflight()`.
- Hlavní krok selhal **bezprostředně** při vyhodnocení `poetry run python -B -m bounded_run -- python main.py --mode tournament` s chybou **`The option "-B" does not exist`**. Program `main.py` vůbec nebyl spuštěn; nevykonal se žádný Metaculus forecast/comment ani placený výzkum.
- Po pokusu zůstal žurnál beze změny na `02e324f9045952ed00b0a1bb5058ded4d39178e6`. Nedošlo k novému obsahu ani GitHub checkpointu. Produkční `main` rovněž zůstal stejný. Jednorázový spouštěč a YAML byly z `ops/` větve po pokusu odstraněny.
- Běh z 10. 9. 2026 `34463156990` je i nadále `queued`, bez vytvořených jobs a s dříve revokovaným tokenem. Externí cron-job.org zůstává vypnutý.

## Oprava v tomto PR

Zachovat existující supervisora `bounded_run` a **spouštět jej přímo prostřednictvím interpreteru projektu** `.venv/bin/python`. Odstraňuje se vrstva zpracování argumentů Poetry při spouštění, jež vyprodukovala tuto chybu.

Tři workflow v tomto PR používají:
- `.venv/bin/python -B -m bounded_run -- .venv/bin/python -B main.py`
- U Cup/Test se pouze připojí původní odpovídající `--mode` argument.

Již provedená instalace `poetry install --no-interaction --no-root` připravuje `.venv` přímo v pracovním adresáři, jak potvrzují předchozí GitHub logy. Nový CI krok na čistém runneru skutečně spustí tento `.venv/bin/python` příkaz se **syntetickým neškodným dítětem** (pouze výpis textu), bez importu `main.py`, tokenů a placených API. Navíc byla upravena sada offline regresí.

## Omezení oprávnění

Původní souhlas uživatele se vztahoval **pouze na jeden ostrý běh**. Ten už byl spotřebován, přestože nedošlo k forecastu. **Neopakovat a nespouštět další živý běh bez nového výslovného souhlasu.** Tento PR obsahuje jen návrh opravy a testování bez publikování.
