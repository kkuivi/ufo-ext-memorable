# ufo-ext-memorable

Procedure memory for a [UFO](https://github.com/ufo-ai/ufo-core) deploy. The first time a turn solves a task, the resolution is saved locally and the tool trace is sent to [Memorable](https://www.memorable.sh). The next similar turn gets that procedure back as reference text.

## Install

Install it into the same environment as `ufo`. That `ufo` install must not already register an extension named `memorable`.

```bash
uv pip install "ufo-ext-memorable @ git+https://github.com/kkuivi/ufo-ext-memorable.git"
```

## Configure

In `ufo.toml`:

```toml
[pack]
name = "assistant-memorable"

[ext]
store = "catalog.toml"
```

Write `catalog.toml` next to `ufo.toml` (this repo includes a copy):

```toml
[[extensions]]
name = "memorable"
version = "0.1.0"
```

`assistant-memorable` is the assistant pack plus this extension. A deploy left on the stock `assistant` pack will not load it.

Put a Memorable key in the deploy `.env` before the server starts. Create one at [memorable.sh/dash](https://www.memorable.sh/dash) under Account, New key for an agent.

```bash
UFO_MEMORABLE_API_KEY=mk_...
```

Then pin, migrate, and start:

```bash
ufoctl ext install memorable
ufoctl migrate
ufoctl serve
```

`ufoctl serve` does not run migrations. Restart the server after the key or the package changes.

## What a finished turn does

1. The answer is written to a local GBrain page at `ufo/resolutions/<turn id>`.
2. The scrubbed tool trace is sent to Memorable. If Memorable admits a procedure, a later turn with the same task sees that procedure as reference text.

The extract request does not include file contents, conversation text, or the API key. Only a short allowlist of tool fields is sent, and tokens that look like secrets are replaced with `[redacted]`.

With `UFO_MEMORABLE_API_KEY` unset, turns still answer and nothing is sent.

GBrain is optional. The server needs `gbrain` on `PATH`, and Bun, because the GBrain CLI is a Bun script. If `gbrain` is missing, that write is skipped and Memorable still records.

## License

Apache-2.0. See [LICENSE](LICENSE).
