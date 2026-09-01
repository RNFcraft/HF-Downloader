# HF Downloader: архитектура webview-версии

Актуально для версии, указанной в корневом файле `VERSION`.

## Заключение

HF Downloader — локальное Windows-приложение с HTML/CSS/JavaScript-интерфейсом в системном Microsoft Edge WebView2. Python остаётся владельцем файловой системы, Hub API и жизненного цикла загрузки. Публичный HTTP API не поднимается: frontend обращается к ограниченному объекту `DesktopApi` через локальный bridge pywebview.

Tkinter полностью удалён из runtime и исключён из PyInstaller bundle.

## Компоненты

```text
WebView2 window
  └─ web/index.html + styles.css + app.js
       └─ pywebview.api
            └─ DesktopApi
                 ├─ URL parser / metadata browser
                 ├─ DownloadManager thread
                 │    └─ HF Downloader.exe --worker
                 │         └─ huggingface_hub + hf_xet threads
                 └─ DownloadEvent queue
```

- `main.py` выбирает GUI-режим либо внутренний `--worker`-режим.
- `app.py` предоставляет минимальный bridge, диалог папки, настройки и события.
- `web/` содержит независимый frontend без npm и внешнего CDN.
- `models.py` валидирует Hugging Face URL и определяет destination.
- `downloader.py` строит dry-run plan, проверяет диск и управляет retry/cancel.
- `worker.py` выполняет загрузку в отдельном процессе и пишет heartbeat.

## Поток данных

1. Frontend вызывает `inspect_source`.
2. Backend валидирует адрес и получает metadata через `HfApi`.
3. Пользователь выбирает файлы в виртуализированном до 5000 строк представлении.
4. `start_download` повторно проверяет источник, selection и параметры.
5. `snapshot_download(..., dry_run=True)` формирует точный план.
6. Manager проверяет свободное место и запускает тот же executable с `--worker`.
7. Worker параллельно вызывает `hf_hub_download` и атомарно обновляет heartbeat JSON.
8. Frontend забирает очередь событий через `poll_events` каждые 250 мс.

## Безопасность

- HF token существует только в памяти и передаётся worker через environment.
- Token не попадает в settings, manifest или command line.
- В bridge опубликованы только методы `DesktopApi`; manager, queue и window имеют приватные имена.
- Frontend поставляется локально, не загружает скрипты/CDN и использует CSP.
- URL parser допускает только `huggingface.co`, `hf://` и bare repo ID.
- Temporary selection manifest не содержит token и удаляется в `finally`.

## Постоянное состояние

Настройки находятся в `%LOCALAPPDATA%\HF Downloader\settings.json`, поскольку каталог установленной программы может быть read-only. Запись выполняется во временный файл с последующим `os.replace`. Повреждённые и некорректно типизированные значения заменяются безопасными defaults.

## Сборка и доставка

PyInstaller создаёт onedir bundle. В нём находятся Python runtime, зависимости, WebView bridge, worker mode и статические web-ресурсы. Inno Setup превращает bundle в один установочный `.exe`, создаёт ярлыки и uninstall entry.

Onedir используется вместо onefile, чтобы не распаковывать runtime во временный каталог на каждом запуске. WebView2 не включается в bundle: используется системный Evergreen Runtime.

## Проверенные инварианты

- GUI не выполняет network download в UI thread.
- Закрытие окна синхронно останавливает активный worker.
- Скомпилированный executable использует `--worker`, а не `python -m`.
- Progress учитывает только файлы текущего `DownloadPlan`.
- Permanent 401/403/404 ошибки не повторяются.
- Auto переключается с Xet на Plain HTTP после двух stall.
- Partial-файлы не удаляются при retry, fallback или cancel.

## Оставшиеся ограничения

1. Полный metadata tree хранится в памяти; для сотен тысяч файлов нужен SQLite/lazy browser.
2. Зависимости ограничены диапазонами, но пока не закреплены hash-lock файлом.
3. Нет process-level тестов disk-full, corrupted cache и реального обрыва сети.
4. Режим без отдельной подпапки может смешивать файлы с существующим каталогом.
5. Для запуска требуется установленный Microsoft Edge WebView2 Runtime.
6. Установщик пока не подписан Authenticode-сертификатом, поэтому Windows SmartScreen может показывать предупреждение.

## Рекомендуемый следующий этап

- добавить lock-файл для release build;
- добавить иконку и version resource в executable;
- подписывать setup и основной executable;
- внедрить release pipeline с чистой Windows VM;
- добавить integration suite с локальным mock HTTP server.
