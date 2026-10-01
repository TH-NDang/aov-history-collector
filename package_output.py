#!/usr/bin/env python3
from pathlib import Path
import shutil

root = Path(__file__).resolve().parent
output = root / "output"
archive = root / "aov-history-data.zip"
if archive.exists():
    archive.unlink()
if output.exists():
    shutil.make_archive(str(archive.with_suffix("")), "zip", root, "output")
    print(archive)
else:
    print("Chưa có thư mục output; không tạo gói.")

