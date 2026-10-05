# Jak pracovat s editorem tratě

Mapový editor kreslí trať, zóny a volitelnou předlohu. Oranžové tlačítko vpravo nahoře to přenese do Blenderu. Snímek předlohy se do scény vloží až tam, tlačítkem **Vložit referenci**.

## Tok práce

1. V Blenderu otevřete panel **Terén** (záložka vpravo ve 3D okně).
2. Jednou klikněte **Připravit prostředí**, dokud addon nehlásí, že je prostředí připravené. Bez toho nepoběží zjednodušení, přichycení na silnice ani pozdější terén.
3. **Otevřít editor** spustí tuto mapu. Pokud už ve scéně trať nebo předloha je, editor je načte.
4. Vyhledejte místo, nakreslete hlavní trať (aspoň dva body), podle potřeby odbočky a zóny.
5. Volitelně přepněte na předlohu a položte snímek (PNG, JPEG nebo WEBP).
6. Oranžové tlačítko přenese trať, zóny a umístění předlohy do scény a editor zavře.
7. Ve stejném panelu pak spusťte **Vložit terén**, případně **Vložit referenci**, **Ortofoto na materiál**, **AI upscale ortofota** a **Osa jako křivka**.

Úlohy běží postupně. Stav je dole v panelu. Běžící akci zruší křížek vedle stavového řádku.

## Kreslení tratě

Režim tratě je první ikona vlevo (aktivní oranžovým rámečkem). Pravý panel ukazuje **Zóny**. Šipkou v hlavičce panel sbalíte. Dole vlevo je tlačítko konzole kroků.

| Akce | Výsledek |
| --- | --- |
| `Shift` a klik do mapy bez označeného bodu | Přidá bod na konec **hlavní** tratě. První bod vznikne jen takto. |
| Klik bez `Shift` | Posune **poslední** bod hlavní tratě. |
| Klik na existující bod | Označí ho. Označený bod je větší a světlejší. |
| `Shift` a klik, když je označený **konec** úseku | Prodlouží ten úsek. |
| `Shift` a klik, když je označený bod **uvnitř** úseku | Založí odbočku z toho bodu. Odbočky jsou tyrkysové. |
| Tažení bodu | Posune ho. |
| `Delete` nebo `Backspace` | Smaže označený bod. Prázdná odbočka zmizí. |

První bod hlavní tratě je **modrý**. Hlavní trať je světle šedá, odbočky tyrkysové. Přenos i úpravy tratě chtějí aspoň **dva body**. Nad 500 body se značky na mapě nestrkají, aby mapa zůstala svižná; trať ale dál platí.

Klik do prázdné mapy bez `Shift`, když ještě žádný bod není, nic neudělá.

## Lišta tratě

Tyto ikony jsou vidět jen v režimu tratě.

- **Koš** smaže celou trať (zóny v panelu nechá).
- **Smazat bod** smaže označený bod, stejně jako `Delete`.
- **Zjednodušit** spojí rovné úseky a ubere zbytečné body na všech částech tratě. Potřebuje připravené prostředí v Blenderu.
- **Připnout na silnice** vede **jen nakreslenou hlavní** trať po nejbližší silnici mezi vašimi body a doplní body podle tvaru cesty (asi po 8 m). Nehledá jiný okruh ani zkratku mimo náčrt. Odbočky zůstanou. Najetím na ikonu zvolíte profil: **auto**, **kolo** nebo **chůze**. Když router cestu nenajde, zkuste profil chůze.
- **Zpět** (`Ctrl+Z`) a **Znovu** (`Ctrl+Y` nebo `Ctrl+Shift+Z`) vrací trať, plochu štětce a předlohu. Čísla zón v panelu zpět nejdou.

Oranžové tlačítko vpravo přenese trať, zóny i předlohu a editor zavře. Snímek ve scéně ještě není; ten vloží **Vložit referenci**.

## Zóny a štětec

Zóny jsou čtyři pásy od silnice ven. Určují, jak hustý bude mesh terénu.

- **Hranice terénu** je okraj staženého výřezu v metrech od tratě (výchozí 2000 m). Musí být větší než nula.
- **Koridor silnice** (nejméně 5 m od osy na každou stranu) je na mapě **zlatý pás** podél tratě i odboček. Ve Blenderu se váže na volbu **Podrobně jen koridor**: podrobný výškový model se stáhne v tomto pásu a v namalovaných zónách, zbytek výřezu hruběji. Po změně čísla se pás hned překreslí.
- **Poloměr** zóny je vzdálenost od tratě. Prázdné pole nebo nula znamená **zbytek terénu**. Poloměry, které mají číslo, musí **růst** od silnice ven.
- **Krok** je rozteč bodů meshe v metrech. Menší krok znamená hustší síť a víc bodů. Musí být větší než nula.

Výchozí hodnoty: zóna 1 má 12 m a krok 2 m, zóna 2 80 m a 6 m, zóna 3 400 m a 20 m, zóna 4 je zbytek terénu s krokem 40 m. Barevné plochy na mapě jsou zóny, zlatý pás je koridor. Odhad bodů meshe vidíte v panelu Terén v Blenderu.

### Štětec

Štětec rozšiřuje **zvolený cíl** mimo pás kolem tratě (parkoviště, šikana, plocha mimo silnici). V nastavení štětce přepnete **Zónu 1 až 4** nebo **koridor silnice**. `Tab` ho zapne i vypne; zapne se i režim tratě.

- Najetím na ikonu zvolíte cíl, nastavíte poloměr 2–200 m a smažete namalovanou plochu **tohoto** cíle.
- Tažení levým tlačítkem plochu přidá k vybrané zóně, nebo k podrobnému koridoru výšek. Namalovaná zóna i koridor dostanou podrobný výškový model, nejen hustší mesh.
- `Ctrl` (nebo `Cmd` na macOS) při tahu maže jen zvolený cíl.
- `Alt` a kolečko myši mění poloměr po 2 m.

Namalovaná plocha má barvu cíle: zóny 1–4 jako v panelu, koridor zlatě. Zóna mění hustotu meshe a stáhne tam stejný podrobný reliéf jako koridor. Koridor rozšiřuje pás podrobných výšek bez změny hustoty meshe.

## Předloha

Druhá ikona vlevo přepne režim předlohy. Pravý panel ukáže **Předloha**, kreslení tratě se ztlumí.

1. Přetáhněte snímek do panelu, nebo klikněte a vyberte **PNG, JPEG nebo WEBP** (do 40 MB).
2. Snímek leží na mapě. V režimu předlohy ho chytnete a posunete.
3. `G` posune, `R` otočí, `S` mění měřítko. Pohyb myši úpravu provádí. Levé tlačítko nebo `Enter` potvrdí, pravé tlačítko nebo `Esc` zruší.
4. Rohové úchyty mění měřítko, horní kulatý úchyt otáčí. Posuvníky v panelu nastaví natočení, měřítko a průhlednost.
5. **Sundat předlohu** ji z mapy odebere.

Přenos uloží zeměpisné umístění. Do scény Blenderu snímek vloží až **Vložit referenci**. Když soubor mezitím zmizí, přeneste předlohu z editoru znovu.

## Hledání a mapa

Pole vlevo hledá **adresu, obec nebo ulici**. Po třech znacích nabídne návrhy, lupa nebo Enter skočí na výsledek.

Na liště vlevo přepínáte **Ortofoto** (ČÚZK) a **Mapu OSM**. Ortofoto je podklad pro kreslení, ne finální textura ve scéně; tu stáhne **Ortofoto na materiál**.

Ikona oka zapne **viditelnost** v pásu 400 m od tratě. Zelená plocha je to, co je vidět z výšky očí (1,5 m) nad osou. Podkladem je DMR 5G. Vybrané budovy se do něj zapečou podle DMP OK, takže střecha výhled zakryje. Bez vybrané budovy je vidět jen přes terén. Výpočet běží ve Blenderu, editor zůstane otevřený. Další klik vrstvu schová.

## Budovy

Třetí ikona vlevo přepne režim **Budovy**. Mapa musí být přiblížená (zoom 16 a blíž), jinak se půdorysy nenačtou. Klik na obrys ho vybere, další klik výběr zruší. Výběr zůstane uložený ve scéně, takže po zavření a novém otevření editoru jsou tytéž budovy zase vybrané. **Vložit budovy** pošle výběr do Blenderu a editor nechá otevřený. **Přenést** dál řeší jen trať, zóny a předlohu.

Každá budova je vlastní objekt v kolekci **Budovy**. Střecha dostane ortofoto, které už je na terénu, včetně zvětšené verze. Stěny zůstanou jednobarevné. Svislé stěny končí na DMR 5G, takže budova sedí na terénu ze stejného počátku. Když terén ještě není, počátek se vezme ze středu tratě. Změna tratě potom budovy vůči novému terénu posune, dokud je nevložíte znovu. Najednou jde vložit nejvýš 200 budov. Objekt nižší než zhruba 1,5 m se přeskočí. ZABAGED někdy slučuje sousední domy do jednoho bloku; takový blok je jeden objekt.

Robot vlevo dole otevře konzoli kroků: co právě běží a čím to skončilo. Červená tečka značí chybu.

## Po přenosu v panelu Terén

Nastavení z editoru (zóny, hranice, koridor, štětec, trať) už ve scéně jsou. V Blenderu ještě zvolte:

- **Zdroj:** **DMR 5G, terén** (holý povrch) nebo **DMP 1G, včetně vegetace**.
- **Podrobně jen koridor:** podrobná výška v koridoru silnice a v namalovaných zónách, okolí hruběji. Vypnutím se stáhne podrobný model na celý výřez; to je výrazně těžší.
- **Zoom ortofota** (6–20, výchozí 18) a **krok osy** pro křivku tratě. Zóna 1 se stahuje v tomto zoomu; když je trať dlouhá, rozdělí se na víc textur místo snížení kvality. Stahuje se po velkých výřezech (až 4096 px), ne po stovkách malých dlaždic. 19–20 je ostřejší, ale stahuje víc dat.
- **AI upscale** po zónách (1× až 16×, výchozí 8 / 4 / 2 / 1) a **dlaždice**. Vedle dlaždice panel ukáže odhad VRAM (384 ≈ 8 GB). Po **Ortofoto na materiál** i RAM a velikost JPEG podle největší zóny. 8× už nedělá mezikrok 16× do pagefile; výsledek se uloží jako JPEG. 16× je volitelné a pořád žere hodně paměti. Nejdřív **Nainstalovat AI model**. V panelu Terén zvolíte složku (váhy i PyTorch) a model jde **odinstalovat**. Sestavení prostředí znovu smaže jen starší PyTorch přímo ve venv; model ve zvolené složce zůstane.
- **OSRM URL** jen když nechcete výchozí veřejný router.

Pořadí, které dává smysl: **Vložit terén** → v editoru **Vložit budovy**, ať sedí na hotovém terénu → **Vložit referenci** (když je předloha) → **Ortofoto na materiál** → případně **AI upscale ortofota** → **Osa jako křivka**.

## Klávesy

| Klávesa | Režim | Účinek |
| --- | --- | --- |
| `Shift` + klik | trať | Přidá bod, prodlouží úsek, nebo založí odbočku. |
| `Tab` | kdekoliv mimo pole | Zapne nebo vypne štětec. |
| `Delete` / `Backspace` | trať | Smaže označený bod. |
| `Ctrl+Z` | oba | Zpět. |
| `Ctrl+Y` / `Ctrl+Shift+Z` | oba | Znovu. |
| `Alt` + kolečko | štětec zapnutý | Mění poloměr štětce. |
| `G` / `R` / `S` | předloha | Posun / otočení / měřítko. |
| `Enter` | úprava předlohy | Potvrdí. |
| `Esc` | úprava předlohy, nebo nápověda | Zruší úpravu, nebo zavře tuto nápovědu. |
