# Validace rešerší a bezpečné retry

Navazující [validace prognóz](forecast-validation.md) popisuje novější kontrolu
odpovědí a zmrazených plánů. Počty testů níže zachycují stav `ac4e5e6`; věcná
kvalita historických rešerší se nadále zpětně neověřuje.

Navazuje na `c71e79f`; implementace je pouze ve větvi
`fix/research-validation-retries`. Neproběhla živá API volání, push, merge,
nasazení ani změna Secrets nebo produkční větve `main`.

## Kdy se nově odmítne prognóza

| Situace | Nové chování |
| --- | --- |
| Agent vrátí `failed`, `incomplete`, pending stav nebo chybějící `status` | Report je neplatný i při HTTP 200 a přítomném textu. Z této větve nevznikají prognózy. |
| Prázdná/whitespace odpověď, neplatný JSON, odmítnutí, chybová zpráva nebo deklarované zkrácení | Neplatný report; nedochází k opakování chybného obsahu jako síťového výpadku. |
| Odkazy bez vlastního textu rešerše | Neplatný report. Dříve přidaný seznam `Sources` maskoval prázdnou odpověď. |
| Neplatná první Agent rešerše | Fact-check se nespustí. |
| Selhaný/prázdný fact-check | Nevrací se původní neověřený text jako náhrada. |
| Všechny povinné rešeršní větve selžou | Nespustí se forecasting/parser; nevznikne plán publikování, předpověď ani komentář. |
| Jeden report je validní a druhý selže | Zachován SDK práh `required_successful_predictions=0.5`: tři podložené prognózy z jedné větve mohou stačit. Chyba zůstává v `report.errors`, vysvětlení a WARNING logu. Agregace používá pouze úspěšné větve. |
| Rešerše je výslovně vypnutá (`no_research`, `None` jako string, prázdný string nebo Python `None`) | Nadále lze forecastovat z kontextu otázky a znalostí modelu; log uvádí úmyslné vypnutí. Chyba poskytovatele se na tento režim nikdy nemění. Python `None` nově dosáhne zamýšlené volitelné větve místo dřívější chyby v SDK `get_llm`. |

To jsou záměrné změny rozhodnutí o vytvoření **nové** prognózy. Neplatné reporty
se nepočítají jako úspěšná rešerše a neprodukují dílčí prognózy. Počty reportů,
prognóz, validačních vzorků, minimální práh, výpočet pravděpodobností, prompty,
modely, teplota a agregace se nemění. Text i formát validní rešerše a zdrojů se
zachovávají včetně whitespace.

## Kontrakt dokončené odpovědi

Agent musí vrátit JSON objekt s `status=completed`, neprázdným seznamem `output`
a alespoň jedním neprázdným `output_text` v message. Deklarované error/refusal,
neúplné stavy či `finish_reason=length/max_tokens` na podporovaných úrovních se
odmítnou. Známé message/source položky musí mít správné datové typy; ostatní
tool metadata se nepovažují za text rešerše. Validace platí nezávisle pro první
průchod i fact-check. Neznámé změny formátu mohou bezpečně zastavit provoz;
kontrakt nebyl během této práce ověřován proti živému API.

U alternativního `GeneralLlm` researcheru se odmítne prázdný či netextový výsledek.
SDK `0.2.92` ale přes `invoke()` poskytuje pouze text a nepropaguje finish reason;
neumíme z něj prokázat nezkrácení. Bot na to jednou za instanci upozorní WARNING
logem. Tento režim zůstává funkční, nenahrazuje se vynuceně jiným modelem.
Produkční `perplexity-agent` má výše popsaný strukturovaný kontrakt.

Validace neověří věcnou pravdivost, dostatek důkazů ani úplné dodržení kritérií
otázky. Záměrně nepoužívá minimum znaků, počet zdrojů či odhad kvality podle
klíčových slov; to by měnilo metodiku. Například poctivý text vysvětlující, že
relevantní důkazy nebyly nalezeny, není technicky prázdná odpověď. Známé chyby
předávání options/fine print a další prognostická pravidla zůstávají mimo rozsah.

## Retry pouze pro výzkumné požadavky

| Chyba | Zacházení |
| --- | --- |
| HTTP 401/403, včetně historické Perplexity billingové 403 | Žádné retry. Status se uloží v paměti této instance pro daného research poskytovatele; další reporty/otázky se o stejný přístup nepokoušejí. Nová instance při dalším běhu může přístup znovu ověřit. |
| Ostatní 4xx kromě 408/429 | Bez retry. |
| 408, 429, 5xx, známé síťové timeouty/výpadky, `IncompleteRead` | Omezené retry pouze po návratu ukončeného pokusu. |
| Certifikátová chyba, neznámá aplikační chyba, validace odpovědi | Bez retry. Neodvozuje se dočasnost z libovolného textu chybové hlášky. |
| Deadline/zrušení waiteru, jehož worker ještě může běžet | Bez retry. Nový `BlockingDeadlineExceeded` rozlišuje tento stav od vráceného socket `TimeoutError`. |

Agent má nejvýše **3 pokusy na fázi**. Obě rešeršní fáze mají každá dosavadních
**210 s**, tentokrát společně pro všechny pokusy, frontu a backoff; deadline se
neresetuje. Socket timeout zůstává nejvýše 180 s a při zbývajícím kratším rozpočtu
se zkrátí. Pro dvě výzkumné větve se tedy může provést až 12 Agent požadavků,
nikoli neomezená série. Vše stále podléhá limitu otázky a celého běhu.

Backoff je 2 s, 4 s, případně omezený exponenciální základ do 30 s, plus jitter
0–1 s. `Retry-After` v sekundách i HTTP-date je minimální čekání; neplatná hodnota
použije běžný backoff. Když platný požadovaný odklad nevejde do zbývajícího
rozpočtu, fáze selže bez předčasného retry. Spánek je async a lze ho zrušit.
HTTPError response se uzavírá. Logy obsahují fázi, status, typ chyby a číslo
pokusu, nikoli tokeny, hlavičky nebo tělo chybové odpovědi.

Pro `GeneralLlm` **researcher** se na kopii nahradí obecný SDK retry dekorátor
stejnou klasifikací. Zůstává jeho konfigurované `allowed_tries` (zde 2) i modelový
rozpočet 240 s. Neobaluje se další retry okolo původního dekorátoru, takže se
aplikační pokusy nenásobí. Interní transport LiteLLM/provider klientů zůstává podle
lockfilu; počet fyzických HTTP požadavků těchto alternativních SDK transportů
nebyl měřen živě. Originální modelový objekt a jeho konfigurační metadata se
nemění. Adaptér závisí na ověřeném dekorátoru SDK 0.2.92; změna kontraktu zastaví
rešerši místo tichého návratu k neomezené či nerozlišené politice.

Tato politika se **nepoužívá** pro forecast/parser/summarizer role, Metaculus
publikování ani GitHub CAS. Existující retry těchto jiných klientů se v tomto
kroku nemění. Zejména nikdy nepřidává opakování nejasného Metaculus POSTu.

## Zachované provozní záruky a omezení

Souběžnost, 3h run budget, 60/120min question deadline, procesový dohled,
Actions workflows, triggery i GitHub journal zůstaly zachovány. Daemon IO worker
drží permit až do skutečného návratu; timeout jeho waiteru nespustí další pokus.
Zrušení během backoffu zruší i čekající otázky v rámci dosavadních pravidel.

Rešeršní POST nemá ověřený serverový idempotentní klíč. Po skutečném síťovém
výpadku může opakování znamenat další placenou rešerši či serverový job. Omezené
pokusy a deadliny náklady ohraničují; nedávají exactly-once billing. Již běžící
HTTP nelze spolehlivě odvolat, což stále chrání držení permitu a procesový dohled.

Již zmrazené reporty v publikačním žurnálu se nadále obnovují beze změny a bez
nové rešerše. **Historické reporty se tímto zpětně nevalidují.** Před pozdějším
zavedením je třeba zkontrolovat případné staré neodeslané plány; nepřepisovat
wire payload ani neodemykat nejisté operace kvůli této opravě. Záruka odmítnutí
nevalidní rešerše se vztahuje na nově vytvářené reporty touto verzí.

## Offline ověření

`python -B -m tests.run`: **173 testů, 164 prošlo, 9 očekávaných selhání** ostatních
známých chyb. Pět regresí `RESEARCH-1`, `RESEARCH-2`, `RESEARCH-3` je povýšeno na
běžné procházející testy. Nové testy pokrývají 401/403, 408/429/500/503, síťové
výpadky, `Retry-After`, nevalidní JSON, prázdné/neúplné odpovědi, selhaný fact-check,
volitelnou rešerši, smíšené úspěšné/neúspěšné větve, deadline a zrušení během
backoffu. SDK testy spouštějí skutečné uzamčené tělo `invoke()` s nahrazenou
přímou modelovou hranicí; všechny HTTP/socket zábrany zůstávají aktivní.
Produkční CLI ani žádná živá služba nebyly spuštěny.

Stejný výsledek měl běh se simulovanými Actions proměnnými. Přísný režim skončil
správně kódem 1 právě kvůli devíti nezměněným známým chybám; žádná nová chyba,
skip ani pokus o živé volání. AST srovnání potvrdilo stejné rešeršní/prognostické
prompty, prognostické metody, modelové parametry a runtime rozpočty. Publikační
kód a všechny workflows jsou beze změny oproti `c71e79f`. Offline Python 3.12
nenahrazuje pozdější ověření na skutečných službách či Actions Pythonu 3.11.
