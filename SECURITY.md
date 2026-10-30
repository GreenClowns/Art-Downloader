# Security

**English** · [Русский](#безопасность)

## What the app stores

| What | Where | Notes |
|---|---|---|
| Settings (folder, language, order, username) | Linux `~/.config/art_downloader.json`, Windows `%APPDATA%\ArtDownloader\settings.json` | **Never the password.** |
| DeviantArt access from “Connect DeviantArt” | gallery-dl's cache: Linux `~/.cache/gallery-dl/cache.sqlite3`, Windows `%APPDATA%\gallery-dl\cache.sqlite3` | A refresh token, never shown in the log. |
| Download order | `.order.json` in each download folder | Only work numbers, titles and dates. |

If you type a password, it is passed to gallery-dl through a temporary file that only your user can read and that is deleted right after the download.

**To revoke DeviantArt access:** DeviantArt → Settings → Applications, remove gallery-dl, and delete the gallery-dl `cache.sqlite3` file above.

## What the app does to stay safe

- Runs gallery-dl without a shell; links are only accepted if they start with `http://` or `https://`.
- Folder names coming from websites are cleaned so they can never point outside your download folder; a damaged or edited `.order.json` can't make the app write elsewhere.
- Never overwrites a different file; a name clash gets “ (2)”.
- Warns if a download could run code (`.exe`, `.bat`, `.sh`, …).
- Network: besides the sites you download from, the app only contacts `pypi.org` and `api.github.com` once a day to check for updates (no personal data is sent). Turn it off in Settings → Notifications and updates.
- Library versions are pinned and checked with `pip-audit`; Dependabot proposes updates. GitHub Actions are pinned to exact commits and the build runs with read-only access. Build tools for the AppImage are pinned versions checked by SHA-256.

## Known limitations

- The Windows `.exe` is **not code-signed**, so Windows SmartScreen warns about it. The Linux AppImage isn't signed either. Download both only from this repository's Releases page.
- The app trusts gallery-dl and the sites you download from. Downloaded files are not scanned for viruses.

## Reporting a problem

Please open an issue, or use **Security → Report a vulnerability** on GitHub if it's sensitive.

---

# Безопасность

## Что хранит программа

| Что | Где | Примечание |
|---|---|---|
| Настройки (папка, язык, порядок, логин) | Linux `~/.config/art_downloader.json`, Windows `%APPDATA%\ArtDownloader\settings.json` | **Пароль никогда не сохраняется.** |
| Доступ к DeviantArt после «Подключить DeviantArt» | кэш gallery-dl: Linux `~/.cache/gallery-dl/cache.sqlite3`, Windows `%APPDATA%\gallery-dl\cache.sqlite3` | Ключ доступа, в журнал не выводится. |
| Порядок файлов | `.order.json` в каждой папке загрузки | Только номера работ, названия и даты. |

Если ввести пароль, он передаётся gallery-dl через временный файл, который может прочитать только твой пользователь; файл удаляется сразу после скачивания.

**Чтобы отозвать доступ к DeviantArt:** DeviantArt → Settings → Applications, удали gallery-dl, и удали файл `cache.sqlite3` gallery-dl (см. выше).

## Как программа защищается

- Запускает gallery-dl без командной оболочки; принимаются только ссылки, начинающиеся с `http://` или `https://`.
- Названия папок с сайтов очищаются и не могут указать за пределы папки загрузки; испорченный или изменённый `.order.json` не заставит программу писать в другое место.
- Никогда не перезаписывает другой файл: при совпадении имени добавляется « (2)».
- Предупреждает, если скачанный файл может запускать программы (`.exe`, `.bat`, `.sh`, …).
- Сеть: кроме сайтов, с которых ты качаешь, программа раз в день обращается только к `pypi.org` и `api.github.com`, чтобы проверить обновления (личные данные не передаются). Отключается в Настройки → Уведомления и обновления.
- Версии библиотек закреплены и проверены `pip-audit`; Dependabot предлагает обновления. Действия GitHub Actions закреплены по точным коммитам, сборка работает только с правами чтения. Инструменты сборки AppImage — закреплённые версии с проверкой SHA-256.

## Известные ограничения

- `.exe` для Windows **не подписан цифровой подписью**, поэтому SmartScreen предупреждает о нём. AppImage для Linux тоже не подписан. Скачивай их только со страницы Releases этого репозитория.
- Программа доверяет gallery-dl и сайтам, с которых скачивает. Скачанные файлы на вирусы не проверяются.

## Как сообщить о проблеме

Открой issue, а если это что-то чувствительное — **Security → Report a vulnerability** на GitHub.
