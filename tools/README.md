# `tools/` — misura e profiling del motore

Strumenti di supporto alla feature sperimentale **"motore veloce"** (default OFF).
Non fanno parte dell'app: servono a misurare e a riprodurre i numeri del piano
in `.opencode/plan/velocita-traduzione.md`.

## `bench_page.py` — banco di misura per una pagina

Misura il tempo **reale** di una pagina usando lo stesso percorso di produzione
(`clone_engine.CloneEngine`), ripulendo la cache del desktop a ogni run.

```bash
# baseline (feature OFF), 3 run, traduzione fresca (--ignore-cache)
.venv/bin/python tools/bench_page.py --page 3575 --runs 3 --fresh

# motore veloce + preset rapido
.venv/bin/python tools/bench_page.py --page 3575 --runs 3 --fresh \
    --fast --fast-flags --label fast-w4

# confronto modello/pool
.venv/bin/python tools/bench_page.py --page 3575 --runs 3 --fresh \
    --model openai/gpt-oss-120b --workers 8 --label gpt-oss-w8

# cache interna del motore calda (scenario ri-traduzione)
.venv/bin/python tools/bench_page.py --page 3575 --runs 3 \
    --fast --fast-flags --label warm
```

- `--fresh` aggiunge `--ignore-cache` al motore (misura il lavoro reale, non la
  cache interna di pdf2zh_next).
- `--page` è **0-based** (come tutto il motore).
- Serve `OPENROUTER_API_KEY` per il motore `llm`.
- Risultati: un JSONL (default `tools/bench_results.jsonl`, ignorato da git) con
  `wall_s`, `child_cpu_s`, `tokens`, esito; e un riepilogo min/mediana/max.

## `child_profile.py` — profilo del processo figlio

`pdf2zh_next` esegue il lavoro pesante in un `multiprocessing.Process`: profilare
il padre misura solo l'attesa. Questo wrapper sostituisce (a runtime)
`pdf2zh_next.high_level._translate_wrapper` con una versione profilata.

```bash
.venv2/bin/python tools/child_profile.py --patch --out child.prof -- \
    /tmp/page.pdf --lang-in en --lang-out it --output /tmp/out/ \
    --openai --openai-model inception/mercury-2.5 --pool-max-workers 4 \
    --no-auto-extract-glossary --ignore-cache

.venv2/bin/python -c "import pstats; \
    pstats.Stats('child.prof').sort_stats('cumulative').print_stats(30)"
```

- `--patch` applica `engine_patch` prima di profilare (per confrontare con/senza).
- Dopo `--` passano gli argomenti di `pdf2zh_next` invariati.

## `provider_proxy.py` — pin del provider LLM (model-aware)

`pdf2zh_next` non espone il routing del provider: per forzare Groq su OpenRouter
serve il campo `provider` nel body. Il proxy lo inietta, **solo per i modelli
scelti** (`PROXY_MODELS`, default `openai/gpt-oss-120b`; `*` = tutti): così gli
altri modelli (DeepSeek, Gemini, Mercury…) passano invariati.

```bash
PROXY_PORT=8790 .venv/bin/python tools/provider_proxy.py   # in background
.venv/bin/python tools/bench_page.py --page 3575 --runs 3 --fresh \
    --fast --fast-flags --fast-worker --prewarm \
    --model openai/gpt-oss-120b --base-url http://127.0.0.1:8790/v1 \
    --reasoning-effort minimal --workers 8 --label gpt-oss-groq
```

Nell'app si imposta da **Impostazioni → Prestazioni** (modello + base URL), senza
variabili d'ambiente. Richiede `OPENROUTER_API_KEY`; `PROXY_PROVIDER` (default
`groq`) sceglie il provider.

Nota: in alternativa si può usare l'allowlist account-wide di OpenRouter
(Settings → Privacy → Allowed Providers), ma è **globale** e blocca tutti gli
altri provider/modelli — valutata e scartata a favore del proxy per-richiesta.

## Worker persistente (Fase 2)

`engine_worker.py` (lato `.venv2`) e `engine_client.py` (lato app) implementano
il worker persistente usato da `CloneEngine` quando l'opzione sperimentale è
attiva (`fast_engine` + `fast_worker`). Non si avviano a mano: li gestisce
l'engine. Il bench li usa con `--fast-worker --prewarm`.

## Nota

Questi tool usano `.venv/bin/python` (app: PyMuPDF) per il bench e
`.venv2/bin/python` (motore) per il profilo. I PDF di test (`ha22.pdf`) non sono
versionati.
