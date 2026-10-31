# MAI Corrosion Control — контроль коррозии скважин

Сервис собирает данные по скважинам (телеметрия ДАРС, химия ХИМИК, ЭРА ремонта, расписание бригад, акты, протоколы и паспорта в PDF, сканах и Excel), рассчитывает **рейтинг скважин** по риску коррозии НКТ с мероприятием и сроком и готовит **предложение ИИ-агента** по скважине. Хранение — Microsoft SQL Server, расчёты и API — FastAPI, ежедневное расписание — n8n, языковая модель и OCR — через LiteLLM. Дашборд в браузере — рабочее место оператора и запасной вариант, пока выход не перенесён в Промайн.

> Пороги и баллы правил НТЦ — демонстрационные. Это индикатор для проверки системы, а не вероятность отказа и не производственная инструкция: перед эксплуатацией их проверяют на фактических данных. Предложения ИИ проверяет инженер; сервис ничего не отправляет и не бронирует сам.

**Развёртывание на сервере (Windows, без Docker)** — архив сборки и `mai.cmd`: [`standalone/README.md`](standalone/README.md) (в архиве — `README-DEMO.md`). Этот файл — устройство сервиса для разработчика и интегратора.

## Содержание

1. [Как устроен](#1-как-устроен)
2. [Запуск для разработки](#2-запуск-для-разработки)
3. [Источники данных](#3-источники-данных)
4. [Документы: Excel, PDF, сканы](#4-документы-excel-pdf-сканы)
5. [Рейтинг скважин](#5-рейтинг-скважин)
6. [ИИ: LiteLLM, OCR, предложение агента](#6-ии-litellm-ocr-предложение-агента)
7. [Ежедневный цикл и n8n](#7-ежедневный-цикл-и-n8n)
8. [Дашборд](#8-дашборд)
9. [API](#9-api)
10. [Настройки MAI_*](#10-настройки-mai_)
11. [База данных](#11-база-данных)
12. [Демо-данные и тесты](#12-демо-данные-и-тесты)
13. [Частые проблемы](#13-частые-проблемы)

## 1. Как устроен

```mermaid
flowchart LR
  subgraph SRC[Источники]
    A1[API источников / витрины Промайн<br/>ДАРС, ХИМИК, ЭРА, бригады]
    A2[Excel-выгрузки, push JSON]
    A3[Документы: PDF, сканы,<br/>фото, Excel; папка-приёмник]
  end
  subgraph API[FastAPI]
    B1[Приём: профили<br/>source_profiles.json]
    B2[Документы: текст → тип →<br/>поля с цитатами → проверка]
    B3[Рейтинг: правила НТЦ, среда,<br/>защита, мероприятие, срок]
    B4[ИИ-агент:<br/>предложение по скважине]
  end
  DB[(SQL Server)]
  LLM[LiteLLM: LLM и OCR]
  N8N[n8n: ежедневный цикл,<br/>форма оператора]
  UI[Дашборд]
  A1 --> B1
  A2 --> B1
  A3 --> B2
  B2 -->|подтверждённые данные| B1
  B1 --> DB --> B3 --> DB
  B2 <--> LLM
  B4 <--> LLM
  N8N -->|/monitor/run, /ai-proposal| API
  DB --> UI
```

1. Данные приходят запросом к API источников (pull), push-запросами, загрузкой Excel или документами. Документ проходит конвейер (раздел 4); подтверждённые значения записываются так же, как данные источников.
2. После каждой загрузки и в ежедневном цикле пересчитывается рейтинг: текущие значения и тренды показателей → правила НТЦ (если замеров достаточно) → среда, защита, категория, мероприятие и срок; у каждого значения — источник и дата (раздел 5).
3. Ежедневный цикл n8n обновляет источники, пересчитывает рейтинг и просит ИИ-агента подготовить предложение по каждой скважине, ожидающей решения (раздел 7).

| Путь | Что там |
| --- | --- |
| `app/main.py` | Приложение FastAPI, `/health`, фоновый обработчик документов |
| `app/api/routes/` | `ingestion` (приём данных), `documents`, `rating` (рейтинг, выгрузка, предложение ИИ), `monitoring` (дневной цикл, пересчёт скважины) |
| `app/core/` | `rating_builder` (значения рейтинга из данных), `rating` (индекс, мероприятие, срок), `rules` (правила НТЦ), `features`, `normalization`, `readiness`, `monitor_runner`, `proposal_agent` и `proposal_language` (ИИ-агент), `excel_ingestion`, `source_connectors`, `llm` (клиент LiteLLM) |
| `app/core/documents/` | Конвейер документов: `intake`, `text`, `extract`, `load`, `pipeline`, `excel_reader`, `nkt_registry` |
| `app/db.py` | Таблицы и запросы SQL Server |
| `config/` | `source_profiles.json` (колонки источников), `document_types.json` (типы документов), `response_examples.json` (примеры решений инженеров для ИИ) |
| `dashboard/index.html` | Дашборд (один файл, без сборки) |
| `n8n/` | Ежедневный цикл и форма оператора |
| `standalone/` | Сборка для Windows Server: `mai.cmd`, команды управления, сборка архива |
| `scripts/`, `examples/` | Демо-данные и демо-документы; шаблон адаптера источника |
| `tests/` | Автотесты и имитация LiteLLM |

## 2. Запуск для разработки

Нужны Python 3.12 и SQL Server 2016+ (уровень совместимости базы 130+) — например, контейнер `mcr.microsoft.com/mssql/server:2022-latest`.

```bash
python -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
export MAI_DATABASE_URL="mssql+pymssql://логин:пароль@localhost:1433/mai"
uvicorn app.main:app --reload
```

Драйвер: `mssql+pymssql://…` (без ODBC) или `mssql+pyodbc://…?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes` (нужен установленный ODBC-драйвер). Таблицы создаются при старте; схема — `MAI_DATABASE_SCHEMA` (пусто — схема входа по умолчанию). Переменные можно держать в `.env` в корне проекта — шаблон с комментариями: `standalone/env.template`.

Дашборд — <http://localhost:8000/dashboard>, схема API — <http://localhost:8000/docs>. При заданном `MAI_API_KEY` все запросы `/api/v1/...` требуют заголовок `X-API-Key` (в дашборде — поле «Ключ API», в n8n — Credential **Header Auth**).

## 3. Источники данных

| Способ | Когда | Как |
| --- | --- | --- |
| Pull API | У источника (витрины) есть HTTP-выгрузка по скважине | `MAI_SOURCE_<ID>_URL` (+ `_TOKEN`); дневной цикл и «Пересчитать скважину» запрашивают сами |
| Адаптер источника | Нужен другой метод, авторизация или формат | Python-пакет с entry point `mai_corrosion.sources` (шаблон `examples/source_adapter_template.py`), ключ `adapter` в профиле |
| Push JSON | Источник сам отправляет данные | `POST /api/v1/ingestion/telemetry`, `/history`, `/crew-schedule` |
| Excel | Ручная выгрузка | Дашборд, «Загрузка данных», или `POST /api/v1/ingestion/excel` (`profile_id`, `file`, опционально `well_id`); `.xlsx` и `.xls` |
| Документы | Акты, протоколы, паспорта, реестр НКТ | Дашборд, «Документы», `POST /api/v1/documents` или папка-приёмник (раздел 4) |

После загрузки рейтинг пересчитывается для скважин из этих данных (до `MAI_RATING_RECOMPUTE_SYNC_LIMIT` скважин сразу, больше — в фоне). Повтор того же файла или пачки с тем же `batch_id` данные не дублирует; значение в тот же момент времени от того же источника заменяет прежнее.

**Pull.** Коннектор делает `GET <URL>?well_id=<ID>` на каждую скважину, с токеном — `Authorization: Bearer <token>`. ДАРС, ХИМИК, ЭРА отвечают книгой `.xlsx`, расписание бригад — JSON; `404` — выгрузки по скважине нет. Переменные: `MAI_SOURCE_DARS_URL`, `MAI_SOURCE_KHIMIK_URL`, `MAI_SOURCE_ERA_URL`, `MAI_SOURCE_CREW_SCHEDULE_URL` и `…_TOKEN`. Скважины цикла — известные базе и `MAI_WELL_IDS`; источники опрашиваются параллельно (`MAI_SOURCE_REFRESH_WORKERS`), временные сбои повторяются (`MAI_SOURCE_MAX_RETRIES`). Режим каждого источника — `GET /api/v1/ingestion/sources`.

**Push JSON** (даты ISO 8601, числа в единицах полей ниже; тот же `batch_id` — безопасный повтор):

```json
POST /api/v1/ingestion/telemetry
{"well_id": "WELL-001", "source_id": "DARS", "batch_id": "DARS-WELL-001-2026-09-30",
 "records": [{"timestamp": "2026-09-30T08:00:00+03:00", "water_cut_pct": 71.4, "co2_pct": 0.8,
              "chlorides_mg_l": 18500, "inhibitor_efficiency": 0.76, "corrosion_rate_mm_year": 0.31}]}

POST /api/v1/ingestion/history
{"well_id": "WELL-001", "source_id": "ERA",
 "work_history": [{"date": "2026-05-10T10:00:00+03:00", "work_type": "Разбор на базе", "equipment": "НКТ",
                   "corrosion_detected": true}],
 "failure_history": [{"failure_date": "2026-05-08T07:30:00+03:00", "failure_type": "утечка", "cause": "коррозия"}]}

POST /api/v1/ingestion/crew-schedule?well_id=WELL-001      (или ?all_wells=true)
{"crew_availability": [{"crew_id": "BRIGADE-17", "specialties": ["КРС"], "status": "available",
  "available_from": "2026-09-29T08:00:00+03:00", "available_to": "2026-10-29T20:00:00+03:00"}]}
```

`status` бригады: `available`, `busy`, `unknown`; личные данные сотрудников не передавайте. Сохранённое — `GET /api/v1/ingestion/wells/{id}/telemetry` и `/history`.

**Excel.** Колонки описаны в `config/source_profiles.json`: `field_map` — варианты заголовков, `required_columns`, `scale_fields` — множители единиц; листы проверяются все (или `sheet_name`, `sheet_names`, `sheet_header_rows`). Даты — значения Excel, ISO 8601 или «16.10.2026 10:30»; время без пояса читается в `MAI_SOURCE_TIMEZONE`. Переименовали колонку — добавьте вариант в `field_map`. Числовые колонки без поля в системе (pH, жёсткость…) сохраняются как дополнительные показатели: видны в карточке и ИИ-агенту, в рейтинг не входят.

| Поле | Единица | Пример заголовка |
| --- | --- | --- |
| `well_id`, `timestamp` | — | «Скважина», «Дата измерения» |
| `water_cut_pct`, `co2_pct`, `chlorides_mg_l` | %, %, мг/л | «Обводнённость, %», «CO₂, %», «Хлориды, мг/л» |
| `inhibitor_efficiency`, `injection_deviation_pct` | доля 0–1, % | «Эффективность ингибитора», «Отклонение закачки, %» |
| `corrosion_rate_mm_year`, `wall_thickness_mm`, `initial_wall_thickness_mm` | мм/год, мм | «Скорость коррозии, мм/год», «Толщина стенки, мм» |
| `field`, `in_operation`, `liquid_rate_m3_day` (ДАРС) | — , статус, м³/сут | «Месторождение», «Статус», «Дебит жидкости» |
| `tonnage_t`, `noo_days` (ХИМИК; строки — лабораторные пробы, `"lab_samples": true`) | т, сут | «Тоннаж обработки, т», «НОО, сут» |
| ЭРА: `event_date`, тип (работа / отказ), `work_type`, `equipment`, `result`, `corrosion_detected` | — | «Дата», «Вид работ», «Обнаружена коррозия» |

Виды работ ЭРА (спуск оборудования, демонтаж, разбор на базе) задаются словами `rating_work_types` профиля `era`, слова защитного исполнения — `protection_keywords`.

**Минимум для прогноза НТЦ:** 8 замеров с разными датами (`MAI_MIN_TELEMETRY_RECORDS`), из них 8 за 28 дней (`MAI_MIN_TREND_OBSERVATIONS`, `MAI_TREND_WINDOW_DAYS`) и 2 показателя-сигнала не старше 7 дней от последнего замера (`MAI_MIN_CURRENT_SIGNALS`, `MAI_MAX_FORWARD_FILL_DAYS`). Иначе скважина остаётся в рейтинге без индекса, а «Полнота данных» говорит, чего не хватает. Устаревшие данные расчёт не блокируют.

## 4. Документы: Excel, PDF, сканы

Документ загружается в разделе «Документы» дашборда, `POST /api/v1/documents` или кладётся в папку-приёмник `MAI_DOCS_INBOX_DIR` (проверяется каждые `MAI_DOCS_INBOX_SCAN_SECONDS`; взятый файл уходит в `processed/<дата>/`, отклонённый — в `failed/<дата>/` с причиной). Повтор того же файла (SHA-256) возвращает уже обработанный документ; документ с ошибкой обрабатывается заново.

1. **Приём:** PDF, `.xlsx`, `.xls`, JPG, PNG, TIFF, BMP, WebP до `MAI_DOCS_MAX_BYTES` и `MAI_DOCS_MAX_PAGES`.
2. **Excel — без ИИ.** Реестр коррозии НКТ (`nkt_registry.py`); таблица в формате выгрузки ДАРС / ХИМИК / ЭРА — обычным импортом; другая таблица со столбцом скважины или форма с одной пробой — по названиям показателей (`excel_reader.py`). В документе у каждого значения — лист и ячейка, у непринятых строк — причина.
3. **PDF, сканы, фото:** текстовый слой PDF читается напрямую, страница-скан — OCR-моделью через LiteLLM; тип документа — по ключевым словам `config/document_types.json`, иначе моделью; поля извлекает модель с дословной цитатой, значение принимается, только если цитата найдена в тексте и его подтверждает.
4. **Запись:** акты демонтажа и разбора — события ЭРА с признаком коррозии; протокол — пробы (как ХИМИК); паспорт — месторождение и спуск оборудования; реестр НКТ — показатели среды и дебит (не замер: свежими данные не делает) и запись реестра для карточки и ИИ. Скважина, которой ещё нет, создаётся документом; удаление документа удаляет его данные (и скважину, если других данных нет).

| Статус | Что значит |
| --- | --- |
| В очереди / Обрабатывается | Ждёт фонового обработчика (`MAI_DOCS_WORKER`; при `false` — `POST /api/v1/documents/process-queue`) |
| Загружен | Записи подтверждены и записаны |
| Не распознан | Тип не определён или данные не подтвердились — ничего не загружено |
| Ошибка | Файл повреждён, OCR или LLM недоступны; повтор `MAI_DOCS_MAX_ATTEMPTS` раз через `MAI_DOCS_RETRY_MINUTES` |

Новый тип документа добавляется в `config/document_types.json` без программирования: `title`, `keywords`, `source_id`, `target` (`work_event`, `telemetry`, `passport`), `fields`.

## 5. Рейтинг скважин

Рейтинг рассчитывает сервис: в дневном цикле, кнопкой «Пересчитать скважину» и после каждой загрузки.

| Поле | Источник и правило |
| --- | --- |
| Месторождение, статус | ДАРС (последнее значение; без статуса — «в работе» при замере не старше `MAI_MAX_TELEMETRY_AGE_HOURS`); месторождение — и из паспорта |
| Оборудование, наработка | Последний «Спуск оборудования» (ЭРА или паспорт); наработка — дни с этой даты |
| Отбор проб | Проба ХИМИК или протокол за `MAI_RATING_SAMPLING_DAYS` (30) дней |
| Среда | CO₂ ≥ 3 %, обводнённость ≥ 75 % или хлориды ≥ 30 000 мг/л — коррозионная. Значение телеметрии действует `MAI_MAX_FORWARD_FILL_DAYS` (7) дней, пробы и реестра НКТ — `MAI_RATING_SAMPLING_DAYS` |
| Демонтаж, разбор на базе | Последний акт за `MAI_RATING_ACT_DAYS` (365) дней и его «Обнаружена коррозия» — исторические данные |
| Категория фонда | Коррозионная — коррозия по акту разбора или коррозионная среда при скорости коррозии ≥ 0,35 мм/год; среднекоррозионная — один признак; иначе некоррозионная |
| Прогноз НТЦ | Баллы правил `app/core/rules.py` / 14 (не больше 1): высокий или критический уровень CO₂, обводнённости, хлоридов, ингибитора, отклонения закачки, скорости коррозии (4 или 7 баллов), рост и превышение 7 дней подряд (по 1), несовместимость вод (7), утонение стенки (2 или 14) |
| Полнота данных | По столбцам таблицы: каких полей нет и какие устарели (с датой последнего значения); для пустого прогноза — чего не хватает («замеров с разными датами — 3 из 8 нужных») |
| Ожидает решения | Есть мероприятие, а плановая дата не задана |

**Индекс коррозии на НКТ** = округлённый прогноз НТЦ × 100.

| Индекс | Мероприятие | Код | Срок |
| --- | --- | --- | --- |
| 80 и выше | Плановый комплекс мероприятий по ремонту, включение в ЭРА ремонта | ППР | `MAI_RATING_DAYS_REPAIR` (14 дн.) |
| 61–79 | Решение инженера | РИ | `MAI_RATING_DAYS_ENGINEER` (30 дн.) |
| 40–60 | Ударная дозировка реагента | УДР | `MAI_RATING_DAYS_UD` (45 дн.) |
| ниже 40 | Мероприятия не требуются | — | — |

Пороги — `MAI_RATING_REPAIR_FROM`, `MAI_RATING_UD_FROM`, `MAI_RATING_UD_TO`. Срок отсчитывается от дня, когда мероприятие впервые назначено: ежедневный пересчёт его не отодвигает. Плановая дата, статус документа Промайн и комментарий — поля оператора (`PATCH /api/v1/rating/wells/{id}`), пересчёт их не меняет.

**Защита.** Эффективна — ингибитор ≥ `MAI_RATING_PROTECTION_MIN_INHIBITOR` (0,80) при отклонении закачки < `MAI_RATING_PROTECTION_MAX_INJECTION_DEV` (15 %) или защитное исполнение НКТ, подтверждённые скоростью коррозии < `MAI_RATING_PROTECTION_MAX_RATE` (0,1 мм/год) без коррозии в последнем акте; недостаточна — мера не выполняется, коррозия в акте или скорость ≥ 0,35 мм/год; иначе не подтверждена. При эффективной защите и `MAI_RATING_PROTECTION_MODE=adjust` баллы среды учитываются с коэффициентом `MAI_RATING_PROTECTION_FACTOR` (0,5); баллы за фактические повреждения не снижаются (`info` — только показать).

**Выгрузка:** «Скачать рейтинг (Excel)» или `GET /api/v1/rating/export.xlsx` — листы «Рейтинг», «График ППР», «Сводка», «Источники данных», «Параметры».

## 6. ИИ: LiteLLM, OCR, предложение агента

Сервис вызывает OpenAI-совместимый Chat Completions LiteLLM (`choices[0].message.content`); ключ — в заголовке `Authorization: Bearer`.

```dotenv
MAI_LLM_API_URL=https://litellm.example.ru/v1/chat/completions
MAI_LLM_API_KEY=ключ
MAI_LLM_MODEL=модель_агента
MAI_DOCS_LLM_MODEL=модель_извлечения_полей     # пусто — MAI_LLM_MODEL
MAI_OCR_MODEL=ocr_модель                       # принимает изображения; MAI_OCR_API_URL / _KEY — если отдельный адрес
```

Без LiteLLM рейтинг и Excel работают; PDF и сканы ждут повторной попытки.

**Предложение ИИ-агента** (`POST /api/v1/rating/wells/{id}/ai-proposal`, кнопка в карточке, ежедневный цикл n8n): модель получает по-русски всё о скважине — индекс, мероприятие и срок рейтинга, прогноз НТЦ, категорию, среду, защиту, акты, отказы, пробы, дополнительные показатели, реестр НКТ, пробелы в данных, документы, расписание бригад — и примеры решений инженеров из `config/response_examples.json` (замените их реальными: ситуация → мероприятие → действия → почему). Ответ проверяется: мероприятие — ППР / РИ / УДР / —; бригада — только свободная из расписания; срок — не позже срока рейтинга; «Нет данных» — известные пробелы плюс новые пункты модели без повторов. Предложение сохраняется у скважины.

## 7. Ежедневный цикл и n8n

`POST /api/v1/monitor/run?background=true` (ответ — `job_id`, статус — `GET /api/v1/monitor/jobs/{job_id}`; одновременно идёт не больше одного прогона): запрос настроенных источников по всем скважинам, пересчёт рейтинга, итог — `alerts[]` (скважины, ожидающие решения: номер, место, индекс, код мероприятия, срок) и `source_failures[]`.

Импорт в n8n: **Workflows → Import from File** (на сервере сборки — `mai.cmd n8n` подставит адрес API). Если задан `MAI_API_KEY` — Credential **Header Auth** `X-API-Key` во всех HTTP-узлах.

| Workflow | Узлы |
| --- | --- |
| `n8n/scheduled_risk_monitor.json` — каждый день в 07:00 | дневной цикл в фоне → ожидание (раз в минуту, до 3 ч) → по каждой скважине из `alerts[]` `POST /rating/wells/{id}/ai-proposal` → итог: предложения и ошибки |
| `n8n/well_analysis_operator.json` — форма | номер скважины → `POST /monitor/wells/{id}/analyze` (источники и пересчёт; неизвестный номер — 404) → если индекс есть, предложение ИИ, иначе — чего не хватает → ответ формы |

## 8. Дашборд

`/dashboard` — один HTML-файл. **Рейтинг**: таблица (место, скважина, индекс, прогноз НТЦ, мероприятие, категория, среда, акты, оборудование и защита, наработка, жидкость / нефть, полнота данных), плитки по мероприятиям, фильтры «ожидают решения» и «нет данных», поиск, выгрузка Excel. **График ППР (ДДО)** на 12 недель. **Карточка скважины** (`#well=<номер>`): прогноз НТЦ, предложение рейтинга и ИИ-агента, пробелы в данных, значения с источником и датой, дополнительные показатели, реестр НКТ, документы, «Пересчитать скважину». **Документы**, **Загрузка данных**, **Расписание бригад**. «Выполнить дневной цикл» делает то же, что n8n: цикл и предложения ИИ по скважинам, ожидающим решения.

## 9. API

Полная схема — `/docs`. При `MAI_API_KEY` нужен заголовок `X-API-Key`.

| Группа | Метод и путь | Назначение |
| --- | --- | --- |
| Состояние | `GET /health/live`, `GET /health/ready` | API; база, LiteLLM, OCR, папка-приёмник |
| Приём | `GET /api/v1/ingestion/sources`, `GET /api/v1/ingestion/wells` | Профили и режимы источников; скважины |
| | `POST /api/v1/ingestion/excel`, `/telemetry`, `/history`, `/crew-schedule` | Excel; push JSON |
| | `GET /api/v1/ingestion/wells/{id}/telemetry`, `/history` | Сохранённые данные |
| Документы | `POST /api/v1/documents` (`?wait=true`), `GET /api/v1/documents`, `GET /api/v1/documents/{id}`, `/{id}/file` | Загрузка, список, поля с цитатами, оригинал |
| | `POST /api/v1/documents/{id}/reprocess`, `DELETE /api/v1/documents/{id}`, `DELETE /api/v1/documents?status=…` / `?all=true` | Повтор, удаление с данными |
| | `POST /api/v1/documents/inbox/scan`, `POST /api/v1/documents/process-queue` | Проверить папку; обработать очередь |
| Рейтинг | `GET /api/v1/rating`, `GET /api/v1/rating/export.xlsx` | Рейтинг, счётчики, пороги; Excel |
| | `GET /api/v1/rating/wells/{id}`, `PATCH /api/v1/rating/wells/{id}` | Скважина с документами и предложением ИИ; поля оператора |
| | `POST /api/v1/rating/wells/{id}/ai-proposal` | Предложение ИИ-агента |
| Цикл | `POST /api/v1/monitor/run`, `GET /api/v1/monitor/jobs/{id}` | Дневной цикл и его статус |
| | `POST /api/v1/monitor/wells/{id}/analyze` | Источники и пересчёт одной скважины |

## 10. Настройки MAI_*

Переменные окружения или `.env` в корне проекта (шаблон — `standalone/env.template`). В скобках — по умолчанию.

| Группа | Переменные |
| --- | --- |
| Основные | `MAI_DATABASE_URL`, `MAI_DATABASE_SCHEMA`, `MAI_API_KEY` (пусто — без ключа), `MAI_WELL_IDS`, `MAI_SOURCE_TIMEZONE` (Europe/Moscow), `MAI_DASHBOARD_URL`, `MAI_SOURCE_PROFILES_PATH` |
| Источники | `MAI_SOURCE_<ID>_URL`, `MAI_SOURCE_<ID>_TOKEN` (DARS, KHIMIK, ERA, CREW_SCHEDULE), `MAI_SOURCE_REFRESH_WORKERS` (4), `MAI_SOURCE_MAX_RETRIES` (2), `MAI_MAX_EXCEL_BYTES` (15 МБ), `MAI_MAX_EXCEL_ROWS` (10 000) |
| Достаточность данных | `MAI_MIN_TELEMETRY_RECORDS` (8), `MAI_MIN_TREND_OBSERVATIONS` (8), `MAI_TREND_WINDOW_DAYS` (28), `MAI_MIN_CURRENT_SIGNALS` (2), `MAI_MAX_FORWARD_FILL_DAYS` (7), `MAI_MAX_TELEMETRY_AGE_HOURS` (48), `MAI_MAX_FUTURE_SKEW_HOURS` (24) |
| Рейтинг | `MAI_RATING_REPAIR_FROM` (80), `MAI_RATING_UD_FROM` (40), `MAI_RATING_UD_TO` (60), `MAI_RATING_DAYS_REPAIR` (14), `MAI_RATING_DAYS_ENGINEER` (30), `MAI_RATING_DAYS_UD` (45), `MAI_RATING_SAMPLING_DAYS` (30), `MAI_RATING_ACT_DAYS` (365), `MAI_RATING_RECOMPUTE_SYNC_LIMIT` (50) |
| Защита | `MAI_RATING_PROTECTION_MODE` (adjust), `MAI_RATING_PROTECTION_FACTOR` (0,5), `MAI_RATING_PROTECTION_MIN_INHIBITOR` (0,80), `MAI_RATING_PROTECTION_MAX_INJECTION_DEV` (15), `MAI_RATING_PROTECTION_MAX_RATE` (0,1) |
| LiteLLM | `MAI_LLM_API_URL`, `MAI_LLM_API_KEY`, `MAI_LLM_MODEL`, `MAI_LLM_TIMEOUT_SECONDS` (45), `MAI_LLM_MAX_RETRIES` (1), `MAI_RESPONSE_EXAMPLES_PATH` |
| OCR | `MAI_OCR_MODEL`, `MAI_OCR_API_URL`, `MAI_OCR_API_KEY`, `MAI_OCR_TIMEOUT_SECONDS` (120), `MAI_OCR_MAX_SIDE_PX` (2000) |
| Документы | `MAI_DOCS_STORAGE_DIR` (./data/documents), `MAI_DOCS_INBOX_DIR`, `MAI_DOCS_INBOX_SCAN_SECONDS` (300), `MAI_DOCS_WORKER` (true), `MAI_DOCS_MAX_ATTEMPTS` (3), `MAI_DOCS_RETRY_MINUTES` (5), `MAI_DOCS_MAX_BYTES` (25 МБ), `MAI_DOCS_MAX_PAGES` (30), `MAI_DOCS_MIN_PAGE_CHARS` (40), `MAI_DOCS_MAX_TEXT_CHARS` (60 000), `MAI_DOCS_LLM_MODEL`, `MAI_DOCS_TYPES_PATH` |

## 11. База данных

Microsoft SQL Server 2016+, драйвер pymssql или pyodbc. При старте (`app/db.py`, `init_db`) создаются схема и таблицы, к существующим таблицам добавляются новые столбцы, а таблицы и столбцы прежних версий удаляются; запускающиеся одновременно процессы ждут друг друга (`sp_getapplock`). Текст хранится в NVARCHAR с бинарным сопоставлением (DEMO-1 и demo-1 — разные скважины), время — DATETIME2 в UTC, JSON — NVARCHAR(MAX).

| Таблица | Что хранит |
| --- | --- |
| `telemetry`, `ingestion_batches` | Показатели и пробы по пакетам загрузки |
| `history_events`, `history_batches` | События ЭРА и актов из документов |
| `source_health`, `source_documents` | Состояние источников; расписание бригад и паспорта |
| `documents` | Документы: файл, статус, текст, извлечённые поля, что загружено |
| `well_rating` | Рейтинг: значения с источниками, поля оператора, предложение ИИ |
| `monitor_jobs` | Фоновые прогоны дневного цикла |

## 12. Демо-данные и тесты

`scripts/build_sample_data.py` создаёт книги ДАРС, ХИМИК, ЭРА по скважинам `DEMO-001` … `DEMO-014` (все мероприятия; у `DEMO-013` мало замеров, у `DEMO-011`, `DEMO-012` нет проб, у `DEMO-002` демонтаж без акта разбора, у `DEMO-014` защита снижает индекс); `scripts/build_demo_documents.py` — демо-документы в `examples/documents/` (акт в PDF, скан акта, протокол в Excel, паспорт, служебная записка). Данные устаревают через 48 часов, поэтому создавайте их с текущей датой:

```bash
python scripts/build_sample_data.py --end-date 2026-10-30 --out data
python scripts/build_demo_documents.py --end-date 2026-10-30
```

Без реальных моделей — имитация LiteLLM: `python -m tests.fake_litellm --port 4010 --end-date 2026-10-30` и `MAI_LLM_API_URL=http://127.0.0.1:4010/v1/chat/completions MAI_LLM_MODEL=fake MAI_OCR_MODEL=fake-ocr`.

```bash
pytest                                                                         # без базы данных
MAI_TEST_DATABASE_URL="mssql+pymssql://sa:пароль@localhost:1433/mai_test" pytest   # полный сценарий
```

Тесты подменяют LiteLLM имитацией и проходят путь от выгрузок и документов (PDF, скан, Excel без ИИ, реестр НКТ, `.xls`) до рейтинга, предложения ИИ-агента и особенностей SQL Server (`tests/test_mssql.py`); отдельная схема — `MAI_TEST_DATABASE_SCHEMA`.

## 13. Частые проблемы

| Признак | Что делать |
| --- | --- |
| Источник `not_configured` | Нет `MAI_SOURCE_*_URL` — задайте адрес или загружайте файлы вручную |
| `unavailable` / HTTP 404 у источника | У источника нет выгрузки по этой скважине — проверьте номер |
| `error` у источника | Доступность адреса, токен, формат файла, листы, названия и единицы колонок |
| Скважина без индекса | «Полнота данных» в карточке называет, каких замеров и показателей не хватает; это не «низкий риск» |
| Документ «Ошибка» | Только PDF, сканы, фото (Excel — без ИИ): задайте `MAI_OCR_MODEL` / `MAI_LLM_*`, затем «Обработать снова» |
| Документ «Не распознан» | Причины по каждому полю — в строке документа; уточните `keywords` / `hint` в `document_types.json` |
| «ИИ-агент не настроен» | Задайте `MAI_LLM_API_URL` и `MAI_LLM_MODEL` |
| n8n: 401 / не соединяется | Credential `X-API-Key`; адрес API, видимый из n8n, и брандмауэр (`standalone/README.md`) |
