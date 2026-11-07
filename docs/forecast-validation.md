# Validace prognóz proti otázce Metaculus

Následující text zachycuje stav `816c171` (208 prošlých a 6 očekávaných selhání).
Navazující [oprava kontextu otázky](question-context-integrity.md) vysvětluje
pět identifikátorů / šest testů a opravuje zbývající regrese. Aktuálně prochází
242 testů bez očekávaných selhání; nezbytné změny promptů jsou popsány tam.

Navazuje na `ac4e5e6`; změny jsou pouze ve větvi `fix/forecast-answer-validation`.
Neproběhla živá API volání, push, merge, nasazení ani změna Secrets nebo větve
`main`. Kontrakty byly ověřeny v lokálním `forecasting-tools==0.2.92` a syntetických
API snapshotech, nikoli proti aktuálnímu serveru. Nový typ otázky se nepřidává.

## Existující formáty a kontroly

| Typ | Dosavadní formát publikování | Doplněné kontroly |
| --- | --- | --- |
| binary | `probability_yes: float` | Konečné číslo, vstup 0–1, wire 0.001–0.999 podle SDK klienta. Původní bot clamp 0.01–0.99 zůstává. |
| multiple choice | `probability_yes_per_category: {přesný název: pravděpodobnost}` | Každá možnost právě jednou, přesná shoda s `question.options`, žádná nová/chybějící/přejmenovaná možnost; čísla 0–1 a výsledný součet 1 s tolerancí 1e-9. |
| numeric | `continuous_cdf: [F0, …, Fn]` | Meze, otevřenost, log scale a CDF velikost musí patřit otázce. Konečná čísla, pořadí, délka, hustota a koncové podmínky. |
| discrete | Stejné CDF jako numeric | Typ je už podporován přes dědičnost `DiscreteQuestion(NumericQuestion)`. Délka je API `inbound_outcome_count + 1`, nikoli vynuceně 201. Bez nového zaokrouhlování nebo discrete metodiky. |
| date | Numeric CDF na UTC epoch sekundách | Stejné CDF kontroly a správné časové pásmo. Datum bez času znamená půlnoc UTC podle stávajícího promptu. |
| conditional | Forecast + komentář pro `question_yes`, pak forecast + komentář pro `question_no` | Každá větev podle vlastního typu/ID/možností/mezí; kontrola celého plánu před prvním zápisem. Parent/child se podle stávajícího SDK nepublikují. |

Skupiny SDK již rozbaluje na jednotlivé otázky těchto typů. Nepřidává se nové
rozhraní pro skupiny ani vnořené conditional predikce, které stávající prediction
union neumí reprezentovat. Neznámý či nekonzistentní typ se odmítne.

## Co se nově odmítá a co zůstává

Před Pydantic konverzí se kontroluje stejný JSON úsek, který čte SDK parser.
Duplicitní JSON klíče, chybějící/extra pole, boolean/string místo čísla,
NaN/Infinity a nesmyslné rozsahy už nemohou projít koercí či clampem jako platná
prognóza. Parserový model, jeho schema/prompt, dva validační vzorky a dosavadní
tři format retry se nemění. Neplatný pokus může být v rámci těchto existujících
retry znovu parsován; nepřidávají se další modelová volání pro validní odpověď.

U multiple choice se duplicity odhalí ještě před převodem na dict. Názvy se
netrimují, nepřekládají ani neopravují podle podobnosti či velikosti písmen.
Pořadí může být jiné, protože drátový formát je mapa názvů; vazba názvu na jeho
pravděpodobnost musí zůstat přesná. Možnost s explicitní nulou se nevynechá.
**Původní SDK clamp/normalizace MC zůstává:** vstupní součet 0.99–1.01 může být
normalizován a SDK může posunout 0/1 do svého intervalu. Finální wire součet se
ověří znovu. Nepřidává se vlastní přepočet pravděpodobností.

Numerické percentily musí mít rostoucí pravděpodobnosti, neklesající konečné
hodnoty a nejméně dva body jako dosavadní SDK. Otevřená mez dovoluje hodnoty mimo
zobrazený rozsah, uzavřená mez nikoli. Používá se `lower_bound/upper_bound` ze
scaling, **nikoli nominal_min/max**. Neodvozují se nové sémantické meze z textu
kritérií. Opakované hodnoty jsou nadále povoleny a zpracuje je původní SDK;
validace již normalizovaného modelu počítá s jeho perturbací do 1e-6 směrem dolů
a 1e-10 nad horní mezí. Raw parser vstupy tuto toleranci nemají. Finální CDF
uzavřené meze stále splňuje přesně; žádný bod se v novém validátoru neopravuje.

CDF **nemá mít součet bodů 1**. Kontrolují se přírůstky a obě krajní hmotnosti:
podle SDK minimum přírůstku `0.01/(n-1)`, maximum
`0.2 * 200/(n-1)` s numerickou tolerancí 1e-10. Uzavřený dolní/horní konec má
hodnotu 0/1; otevřený dolní konec nejméně 0.001 a horní nejvýše 0.999, opět
s tolerancí zaokrouhlení. Je-li v API snapshotu `continuous_range`, ověří se
rovněž jeho přiřazení k CDF gridu; tolerance vychází z velikosti rozsahu a ULP,
ne z pevného absolutního prahu nevhodného pro velmi malá čísla.

API scaling nadále přijímá SDK podporovaná čísla ve stringu. U úplného typovaného
API snapshotu se ale nově vyžadují explicitní bound flags a scaling: chybějící
uzavřenost se nesmí tiše změnit na SDK default „obě meze otevřené“. Standardní
fallback CDF velikosti 201 při chybějícím/None outcome count zůstává jako v SDK.

U date je datum bez hodin nově nezávislé na časovém pásmu hostu a správně znamená
UTC, jak již požaduje prompt. Čas s hodinami bez časového pásma a numerické datum
s nejednoznačnou jednotkou se odmítne; ISO datum/čas s explicitním offsetem je
podporován. Chronologie, uzavřené meze a následné CDF se kontrolují také.

Neplatná dílčí prognóza skončí chybou. SDK ji vyřadí a použije dosavadní
`required_successful_predictions=0.5`; při dostatku validních dílčích prognóz lze
stále publikovat jejich původní agregaci s viditelnými chybami v reportu. Některé
smíšené MC výstupy tak nově mohou bezpečně dokončit běh z validní podmnožiny,
místo selhání až při agregaci nekonzistentních možností. Při nedostatku validních
prognóz nebo neplatném finálním payloadu se nic nepublikuje.

## Publikační plán a obnova

Nový plán se ověří před uložením a celý nedokončený zmrazený plán znovu před
prvním novým `claim`. Kontrolují se typy/ID, počet a pořadí kroků, přesné wire
hodnoty proti uloženému reportu a SDK kontrakt soukromého komentáře. Nesoulad
hashů nebo reportu s plánem se odmítne. Chybná „no“ větev tedy nezpůsobí, že se
nejprve odešle platná „yes“ větev.

Mezi uloženým a právě dostupným snapshotem se porovnají identita, typ, možnosti,
meze/scaling, jednotky, text otázky a přesná kritéria/fine print. Jejich změna
zablokuje nedokončený plán; nic se nepřejmenuje, nepřepočítá, nepřepíše a
nevytvoří se nový záměr pro obejití blokace. To může zablokovat i retry komentáře
po již přijaté předpovědi a vyžadovat ruční kontrolu. Zcela potvrzený plán se znovu
neodesílá. `sending/uncertain` má přednost a zůstává v původní karanténě.

Conditional reporty se nově serializují se zachováním konkrétních podtypů
podotázek. Staré Pydantic base-only záznamy lze rekonstruovat jen z úplného
vloženého API snapshotu; pokud metadata chybí, typ se nehádá z pravděpodobnosti
a obnova se bezpečně zastaví. Uložené wire operace ani jejich identita se nemění.
Tím je opraveno i nalezené omezení conditional round-tripu. Původní mediány,
interpolace, SDK agregace, modely, teploty, 2×3 parametry, prompty, rešerše,
deadliny, GitHub CAS, run intent a jednopokusové POSTy zůstávají.

## Ověření a zbývající omezení

Offline sada: **214 testů, 208 prošlo, 6 očekávaných selhání**. `MC-1/2/3` už
jsou běžné procházející regrese. Nové případy zahrnují všech šest existujících
SDK typů, validní i chybné JSON odpovědi, přesné možnosti, meze, log scale,
CDF grid/density, časová pásma, celé pipeline a obnovu bez dalších prognóz,
conditional round-trip a odmítnutí neplatného celého plánu. Síťové zábrany zůstaly
aktivní; skutečné API ani produkční CLI nebyly spuštěny.

Stejně dopadl běh se simulovanými Actions proměnnými. Přísný režim vrátil kód 1
právě kvůli těmto šesti známým chybám; žádné neočekávané selhání, error, skip ani
pokus o živé volání. AST kontrola potvrdila původní těla prognostických metod a
promptů po odečtení nového předání otázky validátoru. Konfigurace modelů a
forecast parametrů je stejná. Rešeršní politika, runtime, publikační transakce,
GitHub backend a workflows zůstaly oproti `ac4e5e6` beze změny. Samotný
publikační adaptér doplňuje popsané kontroly a typed serialization.
Offline Python 3.12 nenahrazuje pozdější ověření reálných služeb/Actions 3.11.

Nejde o ověření pravdivosti rešerše nebo správnosti pravděpodobnostního úsudku.
Zbývají TIMING-1 (2 testy), BOUNDS-1 a CONTEXT-1/2/3: nesoulad časového kontextu,
nominal bounds v promptu a předávání options/fine print/parent reasoning. Prompty
se v této opravě podle zadání neměnily. Validace stojí na dostupném snapshotu;
změnu otázky na serveru po jeho načtení nelze bez dalšího serverového kontraktu
atomicky vyloučit. Přísnější validace může omezit dostupnost u změněného API
formátu nebo neúplných historických plánů; žádný z nich se automaticky neodblokuje.
