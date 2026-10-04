# Traťový terén

Addon pro Blender 4.2 a novější. Z tratě nakreslené v mapě postaví mesh terénu podle výšek ČÚZK a položí na něj ortofoto. Panel je ve 3D okně pod záložkou **Terén**.

## Instalace

Zazipujte tuto složku tak, aby v kořeni archivu byla `blender_lidartool` a v ní `__init__.py`. V Blenderu ji nainstalujte přes **Edit > Preferences > Add-ons > Install from Disk** a addon zapněte.

V panelu **Terén** jednou spusťte **Připravit prostředí**. Addon si do vlastního Pythonu doinstaluje knihovny z `requirements.txt`.

## Práce

1. **Otevřít editor** a v mapě nakreslit trať. Oranžové tlačítko ji přenese do scény.
2. **Vložit terén** stáhne výškový model a vloží mesh.
3. **Ortofoto na materiál** stáhne ortofoto ČÚZK. **Stáhnout ortofoto znovu** ho vymění a mesh ve scéně nechá.
4. **AI upscale ortofota** zvětší texturu modelem Real-ESRGAN. Nejdřív v panelu nainstalujte AI model. Chce grafiku NVIDIA. Složku instalace i odinstalaci nastavíte tamtéž. Upscale jde spustit z originálu, nebo z už zvětšené verze.

Podrobný popis mapového editoru je v `editor/help.md`.

## Editor

Mapa používá knihovny z `editor/vendor`. Znovu se sestaví v `editor` příkazem `npm install`.
