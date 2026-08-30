# HF Downloader: архитектура и технический аудит

**Проект:** D:\projects\aiagent\hf_downloader  
**Папка загрузки по умолчанию:** D:\projects\aiagent\hf_models  
**Дата аудита:** 29 августа 2026 года  
**Версия приложения:** 1.0.0

> **Обновление реализации после аудита.** Transport layer переработан для HDD и
> нестабильной сети: принудительный HF_XET_HIGH_PERFORMANCE удалён, добавлены
> Auto/Xet Adaptive/Xet Conservative/Plain HTTP, worker heartbeat, plan-scoped
> progress, Xet→HTTP fallback и permanent/transient errors. HFD-H01, HFD-M02,
> HFD-M05 и HFD-L08 закрыты новой реализацией.

Этот документ описывает фактическую реализацию downloader: компоненты, потоки данных, безопасность, восстановление после сбоев, ограничения и рекомендуемые улучшения.

---

## 1. Заключение

HF Downloader — локальное Windows-приложение на Python/Tkinter для загрузки моделей, датасетов, Spaces, отдельных файлов и папок из Hugging Face Hub.

Система уже пригодна для персонального использования:

- принимает web-ссылки, hf:// URI и короткие ID;
- показывает дерево файлов до скачивания;
- позволяет выбрать файлы галочками;
- планирует объём только выбранных файлов;
- проверяет свободное место;
- использует официальный huggingface_hub и Xet;
- поддерживает параллельную загрузку, retry и resume;
- изолирует сетевую операцию в отдельном процессе;
- не сохраняет Hugging Face token;
- показывает прогресс, скорость, ETA и попытки;
- имеет автономный launcher и локальное .venv.

Критических уязвимостей в собственном коде не обнаружено. Высокий lifecycle-риск исходного аудита уже исправлен; перед широким production-развёртыванием остаются средние ограничения воспроизводимости и масштабирования metadata.

| Область | Оценка | Комментарий |
|---|---:|---|
| Функциональность | 8/10 | Основные сценарии реализованы |
| Надёжность сети | 8/10 | Retry, resume, Xet, timeout, worker process |
| Безопасность | 8/10 | Token не сохраняется и не передаётся аргументом |
| Масштабирование | 7/10 | Загрузка масштабируется лучше browser огромных репозиториев |
| Наблюдаемость | 7/10 | Есть live-метрики, но нет постоянной истории |
| Тестируемость | 6/10 | Unit-тесты есть, process-level матрица неполна |
| Воспроизводимость | 6/10 | Есть .venv, но отсутствует lock-файл |

---

## 2. Границы ответственности

Downloader отвечает за:

- разбор Hugging Face ссылки;
- получение metadata репозитория;
- выбор файлов;
- dry-run и оценку размера;
- проверку свободного места;
- запуск и контроль загрузки;
- отображение состояния;
- сохранение безопасных настроек.

Downloader не отвечает за:

- запуск скачанных моделей;
- проверку совместимости с llama.cpp или Transformers;
- распаковку архивов;
- квантование и конвертацию;
- антивирусный анализ;
- принятие условий gated-репозитория;
- создание token;
- доступность CDN Hugging Face.

Скачанные файлы рассматриваются как данные и приложением не исполняются.

---

## 3. Структура проекта

~~~text
hf_downloader/
├── Install-and-Run.bat       пользовательская точка входа Windows
├── launcher.ps1              Python, .venv, зависимости и запуск
├── main.py                   минимальная Python-точка входа
├── requirements.txt          runtime-зависимости
├── settings.json             настройки без token
├── README.md                 краткая инструкция
├── ARCHITECTURE_AUDIT.md     данный документ
├── hf_downloader/
│   ├── __init__.py           версия
│   ├── app.py                GUI и координация
│   ├── models.py             URL parser и HubSource
│   ├── downloader.py         plan, retry, progress, process control
│   └── worker.py             изолированный snapshot_download
└── tests/
    ├── test_models.py
    └── test_downloader.py
~~~

Генерируемые каталоги:

- .venv — изолированное окружение;
- .pytest_cache и __pycache__ — технические кэши;
- D:\projects\aiagent\hf_models — стандартный корень загрузок;
- destination\.cache\huggingface — metadata для resume.

---

## 4. Архитектура верхнего уровня

~~~mermaid
flowchart TD
    U[Пользователь] --> GUI[Tkinter GUI]
    GUI --> P[URL parser]
    P --> S[HubSource]
    GUI --> API[HfApi metadata]
    API --> L[RepositoryFile list]
    L --> C[Галочки и фильтр]
    C --> M[DownloadManager]
    M --> D[snapshot_download dry_run]
    D --> PLAN[DownloadPlan]
    PLAN --> DISK[Disk-space gate]
    DISK --> W[Python worker subprocess]
    W --> HUB[huggingface_hub + hf_xet]
    HUB --> CDN[Hugging Face CDN]
    HUB --> CACHE[local cache]
    HUB --> FILES[Выбранные файлы]
    M --> Q[DownloadEvent queue]
    Q --> GUI
~~~

Ключевое решение — разделение UI, manager и network worker:

- Tk main thread обслуживает интерфейс;
- browser thread получает дерево репозитория;
- manager daemon thread планирует, повторяет и наблюдает;
- отдельный Python subprocess выполняет snapshot_download;
- внутренние потоки Hub скачивают файлы параллельно;
- thread-safe queues возвращают события GUI.

---

## 5. Компоненты и ответственность

### Install-and-Run.bat

Переходит в каталог проекта, запускает launcher.ps1 и оставляет консоль открытой при ошибке. Сам пакеты не устанавливает.

### launcher.ps1

Выполняет:

1. поиск python.exe или py.exe;
2. проверку Python 3.10+;
3. создание .venv внутри проекта;
4. обновление pip;
5. установку или обновление requirements;
6. fallback на установленные пакеты при недоступности PyPI;
7. создание стандартной папки моделей;
8. запуск GUI через pythonw.exe.

Режим -CheckOnly выполняет проверку без запуска GUI. Глобальный Python launcher не изменяет.

### main.py

Импортирует hf_downloader.app.run и запускает Tk event loop. Бизнес-логики не содержит.

### models.py

Преобразует пользовательский ввод в HubSource.

| Ввод | Интерпретация |
|---|---|
| owner/repository | model repository |
| datasets/owner/repository | dataset |
| URL обычной страницы | repository |
| URL /blob/revision/file | файл |
| URL /resolve/revision/file | файл |
| URL /tree/revision/folder | папка |
| hf:// URI | repository, файл или папка |

Проверки:

- web host обязан быть huggingface.co;
- ID имеет форму owner/repository;
- части ID используют ограниченный набор символов;
- путь не содержит .. и не является абсолютным;
- repo type ограничен model, dataset и space;
- revision не содержит обратный слеш или NUL.

destination_for формирует:

~~~text
model:   root\owner--repository
dataset: root\dataset--owner--repository
space:   root\space--owner--repository
~~~

### app.py

Содержит HfDownloaderApp — presentation layer и UI orchestration.

Функции:

- адаптивное окно и прокручиваемая страница;
- закреплённые кнопки скачивания и остановки;
- debounce 750 мс после изменения URL;
- background-загрузка дерева;
- generation number для отбрасывания старых ответов;
- галочки, поиск, выбор всех и снятие всех;
- визуальный лимит 5000 строк;
- валидация старта;
- преобразование DownloadEvent в progress и журнал;
- сохранение настроек без token.

Полный список хранится в памяти:

~~~python
available_files: dict[str, int]
selected_files: set[str]
~~~

Фильтр меняет только отображение и не сбрасывает скрытые галочки.

### downloader.py

Application/backend layer:

- DownloadPlan, RepositoryFile и DownloadEvent;
- list_repository_files;
- dry-run;
- disk-space gate;
- retry policy;
- запуск и остановка worker;
- измерение progress;
- нормализация ошибок.

### worker.py

Минимальный изолированный исполнитель. Получает repo ID, type, revision,
destination, workers, selection manifest и heartbeat path. Читает manifest,
вызывает snapshot_download и передаёт manager byte progress, I/O counters,
текущий файл и heartbeat.

Worker не содержит GUI, retry или пользовательских настроек. Ненулевой exit code возвращает управление manager.

---

## 6. Модели данных

### HubSource

| Поле | Значение |
|---|---|
| repo_id | owner/repository |
| repo_type | model, dataset или space |
| revision | main, branch, tag или commit |
| path | путь внутри repo или None |
| path_kind | repository, file или folder |

### RepositoryFile

Содержит относительный POSIX-путь и размер в байтах.

### DownloadPlan

| Поле | Назначение |
|---|---|
| files | файлов после allow/ignore |
| total_bytes | полный размер плана |
| download_bytes | сколько ещё требуется |
| destination | итоговая папка |
| free_bytes | свободное место |

### DownloadEvent

Типы: status, plan, progress, retry, complete, cancelled, error.

Дополнительные поля: message, downloaded, total, speed, eta, files_done, files_total и attempt.

---

## 7. Полный поток работы

### 7.1 Получение дерева

~~~text
URL
 → parse_huggingface_source
 → HubSource
 → ожидание 750 мс
 → background HfApi request
 → RepositoryFile list
 → file_events queue
 → проверка generation
 → таблица файлов
~~~

Для репозитория и папки используется HfApi.list_repo_tree. Для прямой ссылки на файл — HfApi.get_paths_info. Содержимое файлов на этой стадии не скачивается.

### 7.2 Выбор

После получения списка по умолчанию отмечены все файлы. Пользователь меняет отдельные галочки либо использует массовые кнопки. Кнопка запуска активна, только если:

- список принадлежит текущему URL;
- выбран минимум один файл;
- manager не занят.

### 7.3 Планирование

Manager вызывает:

~~~python
snapshot_download(
    dry_run=True,
    allow_patterns=selected_files,
    ignore_patterns=user_patterns
)
~~~

Проверка места:

~~~text
required = download_bytes
reserve  = минимум 512 МБ, обычно 5%, максимум 5 ГБ
required + reserve <= free_bytes
~~~

При нехватке места worker не запускается.

### 7.4 Selection manifest

Тысячи выбранных путей нельзя надёжно передавать аргументами Windows. Поэтому manager создаёт временный JSON:

~~~json
{
  "allow": ["weights/model.gguf", "tokenizer.json"],
  "ignore": ["*.md"]
}
~~~

Manifest не содержит token, передаётся worker одним путём и удаляется после попытки.

### 7.5 Загрузка

Manager запускает отдельный процесс:

~~~text
python -m hf_downloader.worker ...
~~~

Token не находится в command line. При наличии он передаётся только через environment HF_TOKEN.

Worker вызывает официальный snapshot_download с local_dir, выбранными путями,
max_workers и telemetry-aware tqdm class.

### 7.6 Прогресс

Каждые 600 мс manager:

1. измеряет только файлы текущего DownloadPlan;
2. читает heartbeat, process I/O и byte progress;
3. различает отсутствие роста final-файла и отсутствие activity;
4. считает текущую скорость за 12 секунд и среднюю за 60 секунд;
5. вычисляет ETA и помещает progress в queue.

GUI проверяет queue каждые 120 мс. Tk widgets изменяет только main thread.

### 7.7 Завершение

Источником истины является exit code worker, а не progress bar. Exit code 0 создаёт complete; ненулевой код запускает retry или итоговый error.

---

## 8. Retry, resume, stall и cancel

Полное число попыток равно retries + 1. Задержки растут:

~~~text
2, 4, 8, 16, 30, 30 ... секунд
~~~

Resume предоставляет официальный Hub client:

- завершённые файлы не загружаются повторно;
- metadata хранится в local_dir\.cache\huggingface;
- partial data сохраняется;
- повтор с тем же URL, revision, selection и destination продолжает работу.

Stall определяется отсутствием одновременно plan-file growth, tqdm activity и
process I/O. Свежий heartbeat при отсутствии роста final safetensors не считается
зависанием. В Auto два последовательных Xet stall переключают следующие попытки
на Plain HTTP без удаления cache или partial.

Cancel:

1. выставляет threading.Event;
2. monitor замечает его;
3. вызывает terminate;
4. ждёт до пяти секунд;
5. при необходимости вызывает kill;
6. не удаляет partial data;
7. отправляет cancelled.

~~~mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Planning
    Planning --> Downloading
    Planning --> Failed: disk/error
    Downloading --> Completed: exit 0
    Downloading --> RetryWait: error/stall
    RetryWait --> Downloading
    RetryWait --> Failed
    Downloading --> Cancelling
    Cancelling --> Cancelled
    Completed --> Idle
    Failed --> Idle
    Cancelled --> Idle
~~~

---

## 9. Постоянное состояние

settings.json сохраняет:

- destination;
- repo type;
- create_subfolder;
- workers;
- retries;
- stall timeout;
- exclude patterns.

Не сохраняются token, URL, selection, журнал и история загрузок.

Текущая локальная конфигурация при аудите:

~~~json
{
  "destination": "D:\\projects\\aiagent\\hf_models",
  "repo_type": "auto",
  "create_subfolder": true,
  "workers": 4,
  "retries": 6,
  "stall_timeout": 600,
  "transport": "auto",
  "exclude": ""
}
~~~

Fallback в коде: 4 workers, 6 retries, timeout 600 секунд, Auto и отдельная папка.

---

## 10. Безопасность

Реализовано:

1. URL ограничен доменом huggingface.co.
2. Remote path не допускает .. и абсолютный путь.
3. Repo ID валидируется.
4. Token не сохраняется в settings.
5. Token отсутствует в command line.
6. Manifest не содержит token.
7. Загруженные файлы не исполняются.
8. Worker запускается без shell.
9. Destination нормализуется через pathlib.
10. Текст ошибки ограничивается перед GUI.

Жизненный цикл token:

~~~text
Tkinter StringVar
 → manager memory
 → environment HF_TOKEN worker
 → huggingface_hub
~~~

При пустом поле официальный клиент может использовать credential, ранее сохранённый Hugging Face в профиле пользователя. Downloader самостоятельно его не отображает.

Не проверяются вредоносность данных, лицензия, model card, безопасность будущего trust_remote_code и CVE зависимостей. В рамках аудита выполнен pip check, но отдельный актуальный CVE scan не выполнялся.

---

## 11. Производительность

UI разрешает 1–16 parallel files. Worker дополнительно ограничивает значение максимумом 32. Для Xet включено:

~~~text
HF_XET_HIGH_PERFORMANCE не устанавливается
~~~

Полное дерево хранится в dict и set, визуально отображаются первые 5000 совпадений. Поиск работает по полному набору.

| Операция | Сложность |
|---|---|
| Получение дерева | O(N) |
| Сортировка | O(N log N) |
| Поиск | O(N) на изменение |
| Выбрать всё | O(N) |
| Подсчёт selected size | O(S) |
| Scan progress | O(F) каждые 600 мс |

Для сотен тысяч файлов browser и filesystem scan станут узким местом раньше network layer.

---

## 12. Окружение и зависимости

requirements.txt:

~~~text
huggingface_hub>=0.34,<2
hf-xet>=1.1,<2
certifi>=2025.1
psutil>=6,<8
~~~

Проверенное окружение:

~~~text
Python           3.11.9
huggingface_hub  1.29.0
hf_xet           установлен
certifi          2026.07.22
Tk               8.6
pip check        No broken requirements found
~~~

Переменные:

~~~text
HF_HUB_DOWNLOAD_TIMEOUT=600
HF_HUB_ETAG_TIMEOUT=60
HF_XET_HIGH_PERFORMANCE не устанавливается
HF_HUB_DISABLE_SYMLINKS_WARNING=1
HF_HUB_DISABLE_PROGRESS_BARS=1
~~~

Встроенный progress Hub отключён, поскольку используется GUI progress и stderr worker не должен бесконтрольно заполнять pipe.

---

## 13. Выполненные проверки

~~~text
python -m compileall: успешно
pytest: 26 passed
pip check: успешно
launcher -CheckOnly: успешно
GUI initialization: успешно
~~~

Функционально проверены:

- model, dataset и hf:// URL;
- direct file и folder parser;
- запрет чужого домена и ..;
- отдельная destination;
- friendly errors;
- selected-only dry-run;
- получение 26 файлов реального GPT-2;
- metadata прямого config.json;
- GUI: 26 файлов, снятие галочки, 25 выбранных;
- загрузка только config.json размером 665 байт;
- отсутствие невыбранного model.safetensors;
- видимость закреплённой кнопки.
- независимые environment variables четырёх transport profiles;
- Auto: два Xet stall и fallback на Plain HTTP;
- сохранение partial при fallback и распознавание completed resume;
- heartbeat с I/O counters;
- permanent error без retry;
- cancel и shutdown GUI с остановкой worker;
- progress только по текущему DownloadPlan.
- file-level telemetry для каждого параллельно скачиваемого файла.

Тестовые загрузки удалены.

---

## 14. Реестр рисков

### HFD-H01 — worker после закрытия GUI

**Приоритет:** высокий  
**Статус:** исправлено

При закрытии окна _close устанавливает cancel и сразу уничтожает Tk root. Manager является daemon thread и проверяет flag примерно раз в 600 мс. Главный процесс может завершиться раньше, чем manager вызовет terminate. Windows не гарантирует автоматическое завершение дочернего worker.

Последствие: загрузка может продолжиться без GUI, занимая сеть и диск.

Реализовано: manager хранит active process; GUI вызывает shutdown, ожидает
завершение, затем применяет terminate/kill и только после этого уничтожает Tk.

### HFD-M02 — progress в непустом общем каталоге

**Приоритет:** средний
**Статус:** исправлено

_measure суммирует все файлы destination. При отключённой отдельной папке посторонние файлы могут преждевременно показать 100%. Завершение при этом остаётся корректным, потому что определяется exit code.

Реализовано: filesystem progress учитывает только entries DownloadPlan, а
незавершённый transfer дополняется byte telemetry worker.

### HFD-M03 — зависимости не закреплены

**Приоритет:** средний

requirements использует диапазоны, launcher выполняет upgrade. Будущая версия Hub может изменить поведение.

Решение: lock-файл с точными версиями и hashes; обновление отдельной процедурой.

### HFD-M04 — память очень больших repositories

**Приоритет:** средний

Полное дерево материализуется, сортируется и хранится в dict/set. Для сотен тысяч объектов это создаёт задержки и расход RAM.

Решение: lazy folders, virtual table или SQLite metadata index.

### HFD-M05 — false stall

**Приоритет:** средний
**Статус:** исправлено

Stall использует только размер destination. Длительная metadata-фаза или Xet activity вне наблюдаемого дерева может быть ошибочно принята за зависание. Текущие 60 секунд особенно агрессивны для GGUF на 27 ГБ.

Реализовано: heartbeat, tqdm activity, process I/O, timeout 600 секунд и
раздельные metadata/download timeouts.

### HFD-M06 — неполные integration tests

**Приоритет:** средний

Нет автоматизированной матрицы cancel, forced kill, retry после обрыва, resume partial, gated/private, disk full, corrupted cache и десятков тысяч selections.

Решение: mock HTTP server и process-level suite.

### HFD-M07 — смешивание без subfolder

**Приоритет:** средний

При выключении отдельной папки repository записывается прямо в root и может смешаться с другими файлами.

Решение: предупреждение для непустого root или обязательное подтверждение.

### HFD-L08 — retry постоянных ошибок

**Приоритет:** низкий
**Статус:** исправлено

401, 403, 404 повторяются как временный timeout, хотя без изменения параметров это не поможет.

Реализовано: 401/403/404 и invalid repository/revision завершаются без retry.

### HFD-L09 — ignore против selected

**Приоритет:** низкий

Exclude pattern может убрать отмеченный файл. Строка выбора до dry-run и фактический plan будут отличаться.

Решение: после plan показывать исключённые paths.

### HFD-L10 — остаточный manifest

**Приоритет:** низкий

Если subprocess.Popen завершится исключением до установленного lifecycle, временный manifest может остаться. Секретов в нём нет.

Решение: общий внешний try/finally вокруг manifest и Popen.

### HFD-L11 — неатомарные settings

**Приоритет:** низкий

Авария во время write_text может повредить settings.json. Приложение восстановится с defaults, но настройки потеряются.

Решение: temporary file и os.replace.

---

## 15. Что сделано правильно

- Network не блокирует Tk main thread.
- Фактическая загрузка отделена process boundary.
- Успех не зависит от приблизительного progress.
- Тысячи paths передаются manifest, а не CLI.
- Token отсутствует в manifest, settings и command line.
- Dry-run выполняется до передачи данных.
- Disk check включает резерв.
- Retry повторно использует Hub cache.
- Старый browser response не заменяет новый UI state.
- Direct file использует правильный metadata endpoint.
- Визуальный лимит не ограничивает поиск и selection.
- Кнопка запуска закреплена.
- Launcher имеет offline fallback.

---

## 16. План развития

### Этап 1 — lifecycle и точность

1. Добавить Windows Job Object как дополнительную страховку lifecycle.
2. Расширить heartbeat именем Xet shard/current file.
3. Добавить persistent structured telemetry.

### Этап 2 — качество поставки

1. Добавить lock-файл.
2. Добавить process-level integration tests.
3. Ввести structured logs.
4. Формировать итоговый report: revision commit, files, bytes, duration.

### Этап 3 — крупные datasets

1. Ленивое дерево.
2. Виртуальная таблица.
3. SQLite metadata index.
4. Выбор папок patterns без materialization всех leaf files.

### Этап 4 — функции

1. История загрузок.
2. Предварительный disk check при изменении selection.
3. Итоговая hash-верификация.
4. Ограничение скорости.
5. Proxy settings.
6. Очередь фоновых загрузок.

---

## 17. Эксплуатация

Обычный сценарий:

1. запустить Install-and-Run.bat;
2. вставить URL;
3. дождаться списка;
4. выбрать файлы;
5. оставить отдельную папку включённой;
6. для gated repository ввести read-token;
7. нажать закреплённую кнопку «Скачать выбранное».

Рекомендуемые параметры:

| Условия | Workers | Retry | Stall timeout |
|---|---:|---:|---:|
| Быстрый SSD и сеть | 8–16 | 4 | 180–300 с |
| HDD или слабая сеть | 2–4 | 6 | 300–600 с |
| Большой GGUF | 4–8 | 6 | 600 с |
| Нестабильная сеть | 2–4 | 8–10 | 300 с |

Для resume нужны тот же URL/revision, selection, destination и сохранённая .cache\huggingface.

---

## 18. Архитектурные инварианты

При развитии необходимо сохранять:

1. Tk widgets изменяет только main thread.
2. Token не записывается в settings, logs или manifest.
3. Token не передаётся в command line.
4. Selection одинаково применяется к dry-run и download.
5. Успех определяется worker и Hub client.
6. Cancel не удаляет partial data.
7. Remote path не выходит за repository.
8. Destination не строится из непроверенного absolute remote path.
9. Старый metadata response не изменяет новый URL state.
10. Retry не удаляет завершённые файлы.

---

## 19. Итог

Архитектура рациональна для локального desktop downloader: официальный Hub client выполняет сложную передачу, GUI отделён от сети, worker отделён от manager, а выбор проходит единым набором через dry-run и actual download.

Ключевые lifecycle, progress и stall-проблемы закрыты. Главные оставшиеся зоны
усиления — воспроизводимость зависимостей, масштабирование metadata browser и
расширенная process-level интеграционная матрица.
