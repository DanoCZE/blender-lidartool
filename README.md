# Traťový terén

Addon pro Blender 4.2 a novější. Z tratě nakreslené v mapě postaví mesh terénu podle výšek ČÚZK a položí na něj ortofoto. Panel je ve 3D okně pod záložkou **Terén**.

Celý projekt byl nakódován AI modelem Grok 4.7.

## Instalace z release

1. Otevřete [Releases](https://github.com/DanoCZE/blender-lidartool/releases) a stáhněte ZIP přiložený k release. Je to archiv addonu, ne odkaz **Source code (zip)** na konci stránky. Ten má v kořeni jinou složku a Blender z něj addon nenačte.
2. V Blenderu otevřete **Edit > Preferences > Add-ons**, vpravo nahoře rozbalte šipku a zvolte **Install from Disk**.
3. Vyberte stažený ZIP. Nerozbalujte ho.
4. Addon **Traťový terén** zapněte zaškrtnutím.
5. Ve 3D okně otevřete postranní panel (**N**), záložku **Terén**, a jednou spusťte **Připravit prostředí**. Addon si do vlastního Pythonu doinstaluje knihovny z `requirements.txt`.

## Práce

1. **Otevřít editor** a v mapě nakreslit trať. Oranžové tlačítko ji přenese do scény.
2. **Vložit terén** stáhne výškový model a vloží mesh.
3. **Ortofoto na materiál** stáhne ortofoto ČÚZK. **Stáhnout ortofoto znovu** ho vymění a mesh ve scéně nechá.
4. **AI upscale ortofota** zvětší texturu modelem Real-ESRGAN. Nejdřív v panelu nainstalujte AI model. Chce grafiku NVIDIA. Složku instalace i odinstalaci nastavíte tamtéž. Upscale jde spustit z originálu, nebo z už zvětšené verze.

Podrobný popis mapového editoru je v `editor/help.md`.

## Editor

Mapa používá knihovny z `editor/vendor`. Znovu se sestaví v `editor` příkazem `npm install`.
