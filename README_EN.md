# File Extension Changer 🔄

[English](README_EN.md) | [中文](README.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.8%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey.svg)]()

## 📖 Introduction

A **zero-dependency, ready-to-run** local tool for batch-renaming file extensions: change the extension of images (`.jpg` / `.jpeg` / `.png`) or any other files to `.zip` / `.7z` or a custom suffix, with one-click undo.

> Core principle: **renames only — never reads, converts, or compresses file content.**
> Every operation is a plain filesystem rename (`os.rename`, a pure metadata operation within the same directory). Not a single byte of your data is touched.

## ✨ Features

- 🔄 **Batch renaming** — one click for many files, keeping the base name: `photo.jpg → photo.zip`
- 🛡️ **Never overwrites** — appends a numeric suffix on conflict (`photo_1.zip`, `photo_2.zip`...)
- ↩️ **One-click undo** — every conversion batch is recorded and can be reverted batch by batch
- ✏️ **Custom suffixes** — preset `.zip` / `.7z` plus any custom suffix (e.g. `tar.gz`) with validation
- 📊 **Live feedback** — progress bar, status text, and timestamped logs (success / skipped / failure reasons)
- 🧵 **Non-blocking UI** — renames run in a worker thread; the UI is refreshed via a message queue
- 📋 **List export** — save the current file list to a text file
- 🖥️ **Cross-platform** — Windows / Linux, no third-party packages required
- 🈶 **Chinese UI** — the interface is in Chinese; see the [Chinese README](README.md) for full docs

## 🚀 Quick Start

### Windows

```bash
python file_extension_changer.py   # Python 3.8+ ships with tkinter
```

### Linux

```bash
sudo apt install python3-tk            # Debian / Ubuntu
sudo dnf install python3-tkinter       # Fedora
python3 file_extension_changer.py
```

## 📄 License

Released under the [MIT License](LICENSE).
