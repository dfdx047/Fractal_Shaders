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

**From the app** (recommended): open the game's menu and choose **Community shaders → Share**. No
account is needed: Fractal sends the cache files to a small upload service, which stores them as
assets of the [`submissions` pre-release](https://github.com/dfdx047/Fractal_Shaders/releases/tag/submissions)
and opens a pull request that adds only a `meta.json` pointing to them:

```
submissions/<titleid>/<any-name>/meta.json
```

```json
{
  "titleId": "000500001010ec00",
  "gameName": "Mario Kart 8",
  "appVersion": "0.1-beta",
  "device": "AYN Odin 2",
  "gpu": "Adreno 740",
  "assets": {
    "shaders": "000500001010ec00_shaders_<id>.bin",
    "vkpipeline": "000500001010ec00_vkpipeline_<id>.bin"
  }
}
```

**By hand**: fork this repository and open a pull request that only adds one folder with the files
themselves:

```
submissions/<titleid>/<any-name>/
    <titleid>_shaders.bin        (from shaderCache/transferable/)
    <titleid>_vkpipeline.bin     (from shaderCache/transferable/, optional)
    meta.json                    (as above, without "assets")
```

`<any-name>` may use letters, digits, `.`, `_` and `-` (up to 64 characters). Each cache file must be
at most 95 MB. In `meta.json`, `titleId` and `gameName` are required; `appVersion`, `device`, `gpu`
and `driver` are optional.

Every submission is reviewed as a pull request. An automatic check runs first: paths and file names,
the title id, sizes, that the caches parse and belong to that game, and `meta.json`. After a pull
request is merged, a workflow merges the new entries into the `caches` release, updates `index.json`,
removes the submission folder and deletes its files from the `submissions` pre-release. Entries
already in the cache are never replaced, so a submission can only add shaders.

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
- `tools/process_submissions.py`: what runs after a merge (`--local-release-dir DIR` and
  `--local-submissions-dir DIR` to try it locally without GitHub).

## Português

Caches de shaders da comunidade para o Fractal. No app, abra o menu do jogo e escolha
**Shaders da comunidade** para baixar; para compartilhar o seu, use **Compartilhar** (sem conta: o
envio vira um pull request revisado antes de entrar) ou abra um pull request com a pasta `submissions/<titleid>/<nome>/` como mostrado acima. Caches do
Cemu de PC do mesmo jogo são compatíveis. Os caches contêm apenas shaders gerados pelo emulador, sem
código ou dados do jogo; pedidos de remoção vão nas issues.

## Español

Cachés de shaders de la comunidad para Fractal. En la app, abre el menú del juego y elige
**Shaders de la comunidad** para descargar; para compartir el tuyo, usa **Compartir** (sin cuenta: el
envío se convierte en un pull request que se revisa antes de entrar) o abre un pull request con la carpeta `submissions/<titleid>/<nombre>/` como se muestra
arriba. Las cachés de Cemu de PC del mismo juego son compatibles. Las cachés solo contienen shaders
generados por el emulador, sin código ni datos del juego; las solicitudes de eliminación van en issues.
