# Fractal Shaders

Community shader caches for [Fractal](https://github.com/dfdx047/Fractal), the Android fork of the Cemu
Wii U emulator.

The first time a game draws something new, the emulator has to translate and compile its shaders,
which causes stutter. A shader cache built by other players lets Fractal compile those shaders while
the game is loading instead, so the stutter is mostly gone from the first session.

## Downloading caches

In Fractal: open the game's menu and choose **Community shaders**. The app checks
[`index.json`](index.json) for that game and, if there is a cache, downloads it and merges it into
your own cache (nothing you already have is lost).

The files are assets of the [`caches` release](https://github.com/dfdx047/Fractal_Shaders/releases/tag/caches):

```
https://github.com/dfdx047/Fractal_Shaders/releases/download/caches/<titleid>_shaders.bin
https://github.com/dfdx047/Fractal_Shaders/releases/download/caches/<titleid>_vkpipeline.bin
```

`<titleid>` is the base game's title id in lowercase hex (for example `000500001010ec00`).

### Desktop Cemu

These are standard Cemu "transferable" caches. Caches from desktop Cemu work in Fractal and the
other way around, as long as they come from the same game (same title id). To use one in desktop Cemu,
put the files in `shaderCache/transferable/` of your Cemu folder (merge with an existing cache using
`tools/merge_cache.py` if you have one).

## Sharing your cache

**From the app** (recommended): open the game's menu, choose **Community shaders → Share**, and log in
with GitHub. Fractal opens a pull request for you with your cache and a small `meta.json`.

**By hand**: fork this repository and open a pull request that only adds one folder:

```
submissions/<titleid>/<any-name>/
    <titleid>_shaders.bin        (from shaderCache/transferable/)
    <titleid>_vkpipeline.bin     (from shaderCache/transferable/, optional)
    meta.json
```

```json
{
  "titleId": "000500001010ec00",
  "gameName": "Mario Kart 8",
  "appVersion": "Cemu 2.6",
  "device": "Desktop",
  "gpu": "Radeon RX 6600",
  "driver": "Mesa 25.2"
}
```

`<any-name>` may use letters, digits, `.`, `_` and `-` (up to 64 characters). Each file must be at most
95 MB.

An automatic check runs on every pull request: file names and paths, the title id in the file names,
sizes, that the caches parse and match the game, and `meta.json`. After a pull request is merged, a
workflow merges the new entries into the release files, updates `index.json` and removes the
submission folder. Entries already in the cache are never replaced, so a submission can only add
shaders.

## What the caches contain

A cache holds shader programs and pipeline descriptions that the emulator generated while the game
was running. It contains no game code and no other game data. If you believe something here should not
be, open an issue and it will be looked at and removed.

## Tools

All tools are Python 3 with no dependencies.

- `tools/filecache.py`: reads and writes Cemu FileCache containers (the format of both files).
- `tools/merge_cache.py --out OUT IN1 IN2 ...`: merges caches of the same game and kind;
  `tools/merge_cache.py --info FILE` prints entry count and version.
- `tools/validate_submission.py`: the pull request check.
- `tools/process_submissions.py`: what runs after a merge (`--local-release-dir DIR` to try it
  locally without GitHub).

## Português

Caches de shaders da comunidade para o Fractal. No app, abra o menu do jogo e escolha
**Shaders da comunidade** para baixar; para compartilhar o seu, use **Compartilhar** (login com GitHub)
ou abra um pull request com a pasta `submissions/<titleid>/<nome>/` como mostrado acima. Caches do
Cemu de PC do mesmo jogo são compatíveis. Os caches contêm apenas shaders gerados pelo emulador, sem
código ou dados do jogo; pedidos de remoção vão nas issues.

## Español

Cachés de shaders de la comunidad para Fractal. En la app, abre el menú del juego y elige
**Shaders de la comunidad** para descargar; para compartir el tuyo, usa **Compartir** (inicio de sesión
con GitHub) o abre un pull request con la carpeta `submissions/<titleid>/<nombre>/` como se muestra
arriba. Las cachés de Cemu de PC del mismo juego son compatibles. Las cachés solo contienen shaders
generados por el emulador, sin código ni datos del juego; las solicitudes de eliminación van en issues.
