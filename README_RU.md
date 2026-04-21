# ChatRepo MCP

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](#)
[![MCP](https://img.shields.io/badge/MCP-Remote%20Server-black)](#)
[![Read Only](https://img.shields.io/badge/Mode-Read%20Only-green)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Read-only MCP сервер для ChatGPT, который даёт модели глубокий доступ к **одному Git-репозиторию** на вашем VPS.

[Русская версия](README_PUBLIC_RU.md) | [English](README_PUBLIC_EN.md)

* * *

## Что это

Этот проект превращает одну локальную репу в **безопасное remote MCP-приложение** для ChatGPT.

Он нужен для работы с кодовой базой прямо в чате:

- смотреть структуру репозитория
- читать файлы и сравнивать модули
- искать код и текст
- находить TODO / FIXME
- смотреть недавние изменения файлов
- анализировать Git-историю, diff, ветки, blame и grep

Первая версия специально сделана **только для чтения**:
- без записи файлов
- без применения патчей
- без shell execution
- без commit и push

* * *

## Набор инструментов

### Репозиторий / файлы

- `repo_info`
- `list_dir`
- `tree`
- `read_text_file`
- `read_multiple_files`
- `file_metadata`
- `find_files`
- `search_text`
- `symbol_search`
- `recent_changes`
- `todo_scan`
- `dependency_map`

### Git

- `git_status`
- `git_diff`
- `git_log`
- `git_show`
- `git_branches`
- `git_blame`
- `git_grep`

* * *

## Зачем это нужно

ChatGPT гораздо лучше понимает проект, когда видит реальный контекст репозитория.

Этот сервер даёт сильный набор возможностей для анализа кодовой базы в чате и при этом держит безопасные границы:

- только одна репа
- только read-only тулзы
- валидация пути на каждой файловой операции
- блокировка секретов по шаблонам
- лимиты на размер чтения и объём вывода

* * *

## Быстрый старт

```bash
git clone <your-repo-with-this-project>.git
cd chatrepo-mcp

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .

cp .env.example .env
# укажите PROJECT_ROOT на репозиторий, который нужно анализировать

python -m chatrepo_mcp
```

По умолчанию MCP endpoint будет доступен по адресу:

```text
http://127.0.0.1:8000/mcp
```

* * *

## Конфигурация

Минимальный пример `.env`:

```env
APP_NAME=ChatRepo MCP
HOST=127.0.0.1
PORT=8000
PROJECT_ROOT=/opt/myproject
MAX_FILE_BYTES=200000
MAX_READ_LINES=1200
MAX_SEARCH_RESULTS=100
BLOCKED_PATTERNS=.env,.env.*,*.pem,*.key,*.p12,*.pfx,**/.git/**,**/.venv/**,**/node_modules/**
```

Рекомендуемая схема размещения:

```text
/opt/myproject        # целевая репа
/opt/chatrepo-mcp     # этот MCP сервер
```

* * *

## Структура проекта

```text
chatrepo-mcp/
├── README.md
├── README_PUBLIC_EN.md
├── README_PUBLIC_RU.md
├── pyproject.toml
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DEPLOY_VPS.md
│   └── CONNECT_CHATGPT.md
├── deploy/
│   ├── caddy/
│   ├── nginx/
│   └── systemd/
├── scripts/
│   ├── install_ubuntu.sh
│   └── smoke_test.sh
├── src/chatrepo_mcp/
│   ├── __main__.py
│   ├── config.py
│   ├── fs_tools.py
│   ├── git_tools.py
│   ├── security.py
│   └── server.py
└── tests/
```

* * *

## Модель безопасности

Этот сервер предназначен для доступа к контексту репозитория, а не к секретам.

Защита по умолчанию:

- работа только внутри одного корня репозитория
- блокировка типовых секретов и ключей
- запрет прямого чтения `.git`
- проверка каждого пути перед доступом
- лимиты на размер файлов и вывода

* * *

## Подключение к ChatGPT

После деплоя за публичным HTTPS создайте кастомное MCP-приложение в ChatGPT и укажите:

```text
https://YOUR_DOMAIN/mcp
```

Рекомендуемые настройки приложения:

- **Название:** Repo Reader
- **Описание:** Read-only repository and git analysis for one project
- **Аутентификация:** Без авторизации для v1

Подробные инструкции:
- `docs/DEPLOY_VPS.md`
- `docs/CONNECT_CHATGPT.md`

* * *

## Где это полезно

- онбординг в чужой кодовой базе
- обзор архитектуры проекта
- расследование багов
- анализ влияния изменений
- ревью репозитория
- изучение Git-истории в чате

* * *

## Дальше можно добавить

- GitHub слой для PR и issues
- write-тулзы с явным подтверждением
- безопасный запуск тестов
- более умный symbol indexing
- optional UI для дерева и diff

* * *

## Лицензия

MIT — см. [LICENSE](LICENSE)
