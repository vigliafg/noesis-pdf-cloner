#!/usr/bin/env python3
"""Scarica il binario ufficiale ``uv`` per la piattaforma corrente.

Il binario viene messo in ``vendor/uv-bin/`` e incluso nel bundle PyInstaller
(vedi ``.github/workflows/release.yml``), così l'app può installare il motore
di traduzione **senza** che l'utente abbia ``uv`` già installato.

Versione **pinnata** + verifica **sha256** dell'archivio scaricato: non si
esegue mai "latest" alla cieca.

Uso::

    python3 vendor/fetch_uv.py          # scarica (se serve) e scompatta
    python3 vendor/fetch_uv.py --check  # esce 0 se il binario c'è già
"""
from __future__ import annotations

import argparse
import hashlib
import platform
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# Versione pinnata: aggiornare insieme alle hash qui sotto.
UV_VERSION = "0.12.17"
RELEASE_URL = "https://github.com/astral-sh/uv/releases/download"

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "uv-bin"

# asset -> sha256 (contenuto del file ``<asset>.sha256`` della release pinnata)
ASSETS: dict[str, str] = {
    "uv-x86_64-unknown-linux-gnu.tar.gz": (
        "fa82fd8dde8e8eefdecada6aa0889666556cfceb690d06e0c3bca49eb3070a63"
    ),
    "uv-x86_64-pc-windows-msvc.zip": (
        "a252121d5b59398fcb137c6ea448176459a44010f33f67e0072305a637119ca7"
    ),
    "uv-x86_64-apple-darwin.tar.gz": (
        "8dcf05a8c809bb3c471d2b614788ba27a6e41298fc8c31ac84b5f4339fd468e5"
    ),
    "uv-aarch64-apple-darwin.tar.gz": (
        "85f00cbdc6dd3e97eba4c31b4d014375a9fdfe8f570023b84e5102fc3456896b"
    ),
}


def bin_name() -> str:
    """Nome del binario ``uv`` sulla piattaforma corrente."""
    return "uv.exe" if sys.platform.startswith("win") else "uv"


def asset_for_platform() -> str:
    """Asset della release da usare su questa piattaforma."""
    machine = platform.machine().lower()
    if sys.platform.startswith("win"):
        if machine in ("amd64", "x86_64"):
            return "uv-x86_64-pc-windows-msvc.zip"
        raise SystemExit(f"piattaforma non supportata: windows/{machine}")
    if sys.platform == "darwin":
        if machine in ("arm64", "aarch64"):
            return "uv-aarch64-apple-darwin.tar.gz"
        if machine in ("x86_64", "amd64"):
            return "uv-x86_64-apple-darwin.tar.gz"
        raise SystemExit(f"piattaforma non supportata: macOS/{machine}")
    if sys.platform.startswith("linux"):
        if machine in ("x86_64", "amd64"):
            return "uv-x86_64-unknown-linux-gnu.tar.gz"
        raise SystemExit(f"piattaforma non supportata: linux/{machine}")
    raise SystemExit(f"piattaforma non supportata: {sys.platform}/{machine}")


def sha256_of(path: Path) -> str:
    """sha256 esadecimale di un file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> None:
    """Scarica ``url`` in ``dest`` (con User-Agent: GitHub lo richiede)."""
    request = urllib.request.Request(url, headers={"User-Agent": "noesis-fetch-uv"})
    with urllib.request.urlopen(request) as response, dest.open("wb") as out:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)


def _extract_uv(archive: Path, dest: Path) -> None:
    """Estrae solo il binario ``uv``/``uv.exe`` dall'archivio."""
    wanted = bin_name()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            member = next(
                (n for n in zf.namelist() if Path(n).name == wanted), None
            )
            if member is None:
                raise SystemExit(f"{wanted} non trovato in {archive.name}")
            with zf.open(member) as src, dest.open("wb") as out:
                out.write(src.read())
    else:
        with tarfile.open(archive) as tf:
            member = next(
                (m for m in tf.getmembers() if Path(m.name).name == wanted), None
            )
            if member is None:
                raise SystemExit(f"{wanted} non trovato in {archive.name}")
            extracted = tf.extractfile(member)
            if extracted is None:
                raise SystemExit(f"membro non leggibile: {member.name}")
            with extracted, dest.open("wb") as out:
                out.write(extracted.read())
    if not sys.platform.startswith("win"):
        dest.chmod(0o755)


def fetch(force: bool = False) -> Path:
    """Scarica e scompatta ``uv``; ritorna il percorso del binario."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = OUT_DIR / bin_name()
    if dest.is_file() and not force:
        return dest

    asset = asset_for_platform()
    expected = ASSETS[asset]
    url = f"{RELEASE_URL}/{UV_VERSION}/{asset}"
    print(f"Scarico {asset} (uv {UV_VERSION})…", flush=True)

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / asset
        _download(url, archive)
        actual = sha256_of(archive)
        if actual != expected:
            raise SystemExit(
                f"sha256 non corrispondente per {asset}\n"
                f"  atteso:  {expected}\n  ottenuto: {actual}"
            )
        _extract_uv(archive, dest)

    print(f"uv pronto: {dest}", flush=True)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="verifica solo la presenza"
    )
    parser.add_argument(
        "--force", action="store_true", help="riscarica anche se presente"
    )
    args = parser.parse_args()

    dest = OUT_DIR / bin_name()
    if args.check:
        return 0 if dest.is_file() else 1
    fetch(force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
