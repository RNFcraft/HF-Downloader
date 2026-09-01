# HF Downloader updater

## Архитектура

`hf_downloader/updater.py` — независимая от загрузчика Hugging Face подсистема. Она обращается только к latest stable release официального репозитория `RNFcraft/HF-Downloader`, выбирает installer `HF-Downloader-Setup-<version>.exe`, потоково скачивает его во временный каталог `%TEMP%\HF Downloader Update`, проверяет SHA-256 и передаёт запуск Inno Setup desktop-слою.

Состояния: `IDLE → CHECKING → UPDATE_AVAILABLE | NO_UPDATE | FAILED → DOWNLOADING → VERIFYING → READY_TO_INSTALL → INSTALLING`. Отмена скачивания переводит updater в `CANCELLED` и удаляет `.part`.

Поток обновления:

```text
WebView загружается
  → неблокирующая проверка GitHub Releases (не чаще раза в 6 часов)
  → уведомление и changelog как безопасный plain text
  → потоковое скачивание installer
  → digest GitHub asset или отдельный .sha256
  → интерактивный Inno Setup
  → graceful shutdown HF Downloader
  → замена program files с тем же AppId
  → запуск новой версии с сохранёнными настройками из LocalAppData
```

WebView API: `check_for_updates`, `get_update_status`, `download_update`, `cancel_update_download`, `ignore_update`, `set_update_preferences`, `open_update_release`, `install_update`.

## Единая версия

Версия хранится только в корневом файле `VERSION`. Python получает её через `get_current_version()`. PyInstaller включает `VERSION` в bundle, а `build.ps1` передаёт значение в Inno Setup через `/DAppVersion=...`. Имя installer и checksum также формируются из этого значения.

## Создание release

1. Изменить только `VERSION`, например на `1.2.1`.
2. Выполнить `.\build.ps1`.
3. Создать стабильный Git tag `v1.2.1` и GitHub Release `HF Downloader v1.2.1`.
4. Добавить changelog в body release.
5. Загрузить оба файла из `release`:
   - `HF-Downloader-Setup-1.2.1.exe`
   - `HF-Downloader-Setup-1.2.1.exe.sha256`

Release не должен быть draft/prerelease. Постоянный Inno Setup `AppId` менять нельзя.

## Локальная проверка

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --check .\hf_downloader\web\app.js
.\build.ps1
```

Для end-to-end проверки следует опубликовать тестовый стабильный release с версией выше локальной либо временно использовать отдельный тестовый repository URL в изолированной ветке. Dev/portable-запуск показывает release, но не запускает installer автоматически. Установленная PyInstaller-версия разрешает установку.

## Учтённые случаи

- корректное semantic version comparison, включая `1.10.0 > 1.9.0` и pre-release parsing;
- draft/pre-release игнорируются;
- отсутствие сети, timeout, некорректный JSON и отсутствие installer не ломают основной Downloader;
- asset принимается только по HTTPS из Releases официального GitHub repository;
- поддерживаются GitHub asset digest и отдельный `.sha256`;
- mismatch удаляет installer и запрещает запуск;
- отсутствие checksum разрешено в non-strict режиме и явно показывается пользователю;
- обрыв потока определяется по `Content-Length`, partial-файл удаляется;
- cancel updater не связан с cancel обычной загрузки;
- при активной HF-загрузке требуется отдельное подтверждение безопасной остановки;
- настройки updater объединяются со старыми настройками через defaults и атомарную запись;
- старые updater-файлы удаляются только из собственного временного каталога.

## Следующие этапы

- silent update после дополнительного тестирования интерактивной схемы;
- GitHub Actions для сборки по tag и публикации release assets;
- Authenticode-подпись и обязательный strict checksum;
- отдельные интеграционные VM-тесты обновления старой установленной версии поверх новой.
