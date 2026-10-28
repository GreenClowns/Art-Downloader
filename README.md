# Art Downloader

**English** · [Русский](README.ru.md)

A simple Linux desktop app for downloading art and comics from **DeviantArt** (and other sites supported by [gallery-dl](https://github.com/mikf/gallery-dl)) — whole folders at once, in reading order, with no mixed-up files.

![App window](docs/screenshot.png)

## Features

- **Download everything at once**: a folder, chapter, whole series, gallery, profile or a single work — just paste the link.
- **Every image inside a work**: if a DeviantArt post has several images, you get all of them, not just the first one.
- **In order**: each gallery folder on the site gets its own folder on your computer, and files are numbered `001`, `002`, … Order by publication date, gallery order or title (`Part 2` before `Part 10`).
- **18+ works**: the “Connect DeviantArt” button gives the app access on behalf of your account, once (DeviantArt's official sign-in for apps). The app never stores your password.
- **Fast**: parallel downloads.
- **Reliable**: retries failed files automatically, resumes after Stop, never downloads the same file twice.
- **Chapters**: if one folder mixes several chapters, it can split them into `Chapter 01`, `Chapter 02`, …
- **Two languages**: English and Russian, switch with 🌐 in the top-right corner. Defaults to your system language.

Example result:

```
ArtDownloads/My Comic/
├── Chapter 1/
│   ├── 001 - My Comic - Chapter 1 - Cover.png
│   ├── 002 - My Comic - Chapter 1 - Page 1.png
│   └── …
└── Chapter 2/
    ├── 004 - My Comic - Chapter 2 - Page 4.png
    ├── 004-02 - My Comic - Chapter 2 - Page 4.png   ← second image in the same work
    └── …
```

## Installation

Requires Linux (tested on Fedora; Ubuntu/Debian work too).

1. Click the green **Code → Download ZIP** button and unpack the archive.
2. Open a terminal in the unpacked folder and run once:
   ```bash
   bash install.sh
   ```
   The installer sets up everything it needs (`python3-tkinter`, `gallery-dl`) and may ask for your computer password.
3. Done: “Art Downloader” appears in your applications menu (press Super and start typing “Art”).

Or with git:

```bash
git clone https://github.com/GreenClowns/Art-Downloader.git
cd Art-Downloader
bash install.sh
```

## How to use

1. Paste links, one per line.
2. Enter a “Project name” — that's the folder everything is saved to.
3. For 18+ works, click **🔑 Connect DeviantArt** once: DeviantArt opens in your browser, click **Authorize**.
4. Click **⬇ Download**.

If a download gets interrupted, just click “Download” again: the app fetches only what's missing, and file numbers stay the same.

## FAQ

**`403 Forbidden` errors on 18+ works.**
Click “Connect DeviantArt” and download again. Without it, DeviantArt only gives apps a blurred version.

**Some works still didn't download.**
Usually that's paid content (subscriptions, Premium Gallery). You can only download it if you have access to it on the site. The app lists these files at the end of the log.

**Where are the settings stored?**
In `~/.config/art_downloader.json`. Your password is never written there.

## Uninstall

Right-click the app icon in the menu → **“Uninstall Art Downloader”**. Or:

```bash
bash install.sh uninstall
```

Your downloaded images are not deleted.

## For developers

The whole app is a single file, [`art_downloader.py`](art_downloader.py) (Python + Tkinter); downloading is done by [gallery-dl](https://github.com/mikf/gallery-dl). Logic tests (no internet needed):

```bash
python3 -m pytest
```

## Respect the artists

This app is meant for your personal archive. Don't re-upload other people's work without their permission, and support the artists whose work you enjoy.

## License

[MIT](LICENSE)
