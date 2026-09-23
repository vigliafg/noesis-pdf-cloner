"""Tests for vendor/fetch_uv.py (asset selection, hashing, extraction).

Non scarica nulla: verifica solo la logica pura (piattaforma -> asset,
sha256 e scompattamento tar.gz/zip) con archivi finti.
"""

import hashlib
import io
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENDOR = os.path.join(_ROOT, "vendor")
for _p in (_VENDOR, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fetch_uv  # noqa: E402


class AssetSelectionTests(unittest.TestCase):
    def test_linux_x86_64(self):
        with mock.patch.object(fetch_uv.sys, "platform", "linux"), \
             mock.patch.object(fetch_uv.platform, "machine", return_value="x86_64"):
            self.assertEqual(
                fetch_uv.asset_for_platform(),
                "uv-x86_64-unknown-linux-gnu.tar.gz",
            )

    def test_windows_x86_64(self):
        with mock.patch.object(fetch_uv.sys, "platform", "win32"), \
             mock.patch.object(fetch_uv.platform, "machine", return_value="AMD64"):
            self.assertEqual(
                fetch_uv.asset_for_platform(), "uv-x86_64-pc-windows-msvc.zip"
            )

    def test_macos_arm64(self):
        with mock.patch.object(fetch_uv.sys, "platform", "darwin"), \
             mock.patch.object(fetch_uv.platform, "machine", return_value="arm64"):
            self.assertEqual(
                fetch_uv.asset_for_platform(), "uv-aarch64-apple-darwin.tar.gz"
            )

    def test_macos_x86_64(self):
        with mock.patch.object(fetch_uv.sys, "platform", "darwin"), \
             mock.patch.object(fetch_uv.platform, "machine", return_value="x86_64"):
            self.assertEqual(
                fetch_uv.asset_for_platform(), "uv-x86_64-apple-darwin.tar.gz"
            )

    def test_unsupported_platform_raises(self):
        with mock.patch.object(fetch_uv.sys, "platform", "linux"), \
             mock.patch.object(
                 fetch_uv.platform, "machine", return_value="riscv64"
             ):
            with self.assertRaises(SystemExit):
                fetch_uv.asset_for_platform()

    def test_every_asset_has_a_pinned_hash(self):
        for asset, digest in fetch_uv.ASSETS.items():
            self.assertEqual(len(digest), 64, asset)
            int(digest, 16)  # esadecimale valido


class Sha256Tests(unittest.TestCase):
    def test_sha256_of_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.bin"
            path.write_bytes(b"hello uv")
            self.assertEqual(
                fetch_uv.sha256_of(path),
                hashlib.sha256(b"hello uv").hexdigest(),
            )


class ExtractTests(unittest.TestCase):
    def test_extract_from_targz(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            archive = tmp / "uv.tar.gz"
            data = b"#!fake uv binary"
            with tarfile.open(archive, "w:gz") as tf:
                info = tarfile.TarInfo("uv-x86_64-unknown-linux-gnu/uv")
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            dest = tmp / "uv"
            with mock.patch.object(fetch_uv, "bin_name", return_value="uv"):
                fetch_uv._extract_uv(archive, dest)
            self.assertEqual(dest.read_bytes(), data)

    def test_extract_from_zip_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            archive = tmp / "uv.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("uv-x86_64-pc-windows-msvc/uv.exe", b"MZ fake")
            dest = tmp / "uv.exe"
            with mock.patch.object(fetch_uv, "bin_name", return_value="uv.exe"):
                fetch_uv._extract_uv(archive, dest)
            self.assertEqual(dest.read_bytes(), b"MZ fake")

    def test_extract_missing_member_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            archive = tmp / "uv.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("other/thing", b"x")
            with mock.patch.object(fetch_uv, "bin_name", return_value="uv.exe"):
                with self.assertRaises(SystemExit):
                    fetch_uv._extract_uv(archive, tmp / "uv.exe")


if __name__ == "__main__":
    unittest.main()
