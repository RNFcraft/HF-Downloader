# HF Downloader

Настольный загрузчик моделей, датасетов, Spaces, отдельных файлов и папок с Hugging Face. Интерфейс построен на HTML/CSS/JavaScript и открывается в лёгком нативном окне Microsoft Edge WebView2; надёжное Python-ядро работает локально и не поднимает публичный веб-сервер.

## Возможности

- web-ссылки, `hf://` URI и короткие ID `author/repository`;
- предварительный список файлов с поиском и выбором;
- точный dry-run и проверка свободного места;
- параллельная загрузка через официальный `huggingface_hub`;
- Auto, Xet Adaptive, Xet Conservative и Plain HTTP;
- retry, resume, heartbeat, stall detection и Xet fallback;
- прогресс активных файлов, скорость и ETA;
- безопасная остановка с сохранением partial-файлов;
- token хранится только в памяти процесса.

## Запуск из исходников

Для разработки создайте виртуальное окружение, установите зависимости и запустите приложение:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe main.py
```

Требуются Python 3.10+ и Microsoft Edge WebView2 Runtime. В Windows 10/11 WebView2 обычно уже установлен.

Поддерживаемые адреса:

- `https://huggingface.co/author/model`
- `https://huggingface.co/datasets/author/dataset`
- ссылки `blob`, `resolve` и `tree`
- `hf://datasets/author/dataset`
- `author/repository`

## Сборка Windows

Установите [Inno Setup 6](https://jrsoftware.org/isinfo.php), затем выполните:

```powershell
.\build.ps1
```

Скрипт:

1. устанавливает build-зависимости;
2. запускает тесты;
3. создаёт PyInstaller onedir bundle в `dist\HF Downloader`;
4. собирает установщик `release\HF-Downloader-Setup-1.1.2.exe` с выбором каталога установки.

Для проверки только скомпилированного приложения без установщика:

```powershell
.\build.ps1 -SkipInstaller
```

Onedir выбран намеренно: приложение запускается быстрее, не распаковывает Python во временный каталог при каждом старте и лучше подходит как содержимое обычного установщика.

## Структура

- `hf_downloader/app.py` — bridge между web-интерфейсом и Python;
- `hf_downloader/web/` — HTML, CSS и JavaScript;
- `hf_downloader/downloader.py` — планирование, retry и lifecycle;
- `hf_downloader/worker.py` — изолированный процесс загрузки;
- `HFDownloader.spec` — конфигурация PyInstaller;
- `installer/HFDownloader.iss` — проект установщика Inno Setup.

Пользовательские настройки сохраняются атомарно в `%LOCALAPPDATA%\HF Downloader\settings.json`. Token туда не записывается.
