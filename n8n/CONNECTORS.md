# Контракт внешних источников

Backend обновляет источники для каждой скважины перед ручным или ежедневным анализом. Историю работ и расписание бригад получают backend-коннекторы; LiteLLM видит только подготовленный паспорт и структурированные ответы внешних сервисов.

## Поддерживаемые ответы

| Профиль | Ответ API | Что сохраняется |
| --- | --- | --- |
| dars | Excel .xlsx для well_id | Телеметрические измерения |
| khimik | Excel .xlsx для well_id | Химические и коррозионные показатели |
| era | Excel .xlsx для well_id | История работ/отказов |
| crew_schedule | JSON-массив или объект с crew_availability | Доступность, специальность и даты бригады |

Общий HTTP-адаптер выполняет GET по адресу <URL>?well_id=<ID>, добавляет Accept и Bearer-токен, если он задан. Excel возвращается непосредственно в теле. Код 404 значит, что выгрузки для ID нет. Для другого метода, авторизации, параметров или формата реализуйте SourceAdapter; инструкция находится в основном README.

## Excel и несколько листов

Профили находятся в config/source_profiles.json. field_map сопоставляет варианты заголовков с каноническими полями, required_columns задаёт обязательные колонки, scale_fields конвертирует единицы.

Без sheet_name и sheet_names backend автоматически принимает листы, где найдены обязательные поля, остальные пропускаются. Выбранные листы книги должны иметь одинаковую структуру таблиц. sheet_name выбирает один лист, sheet_names — заданный список, sheet_header_rows настраивает строку заголовка для каждого листа. Лимит MAI_MAX_EXCEL_ROWS относится ко всей книге; размер файла ограничен MAI_MAX_EXCEL_BYTES.

Для телеметрии нужны timestamp и well_id (в таблице или общем поле загрузки); каждая строка должна содержать минимум один показатель. Для запуска аналитики по умолчанию необходимо 8 уникальных отметок, 8 измерений в окне 28 дней, минимум 2 разных сигнала за 7 дней и свежесть до 48 часов. При недостатке модели и LLM не запускаются.

Нормализуемые поля: timestamp, water_cut_pct, co2_pct, chlorides_mg_l, inhibitor_efficiency, injection_deviation_pct, corrosion_rate_mm_year, wall_thickness_mm, initial_wall_thickness_mm, metal_loss_mm, corrosion_load, water_compatibility_issue. Единицы: проценты, мг/л, мм/год, мм или доля 0..1. Незнакомые колонки игнорируются и показываются в результате; исходный Excel не сохраняется. Переименование заголовка исправляется alias в field_map. При смене единицы или смысла обновите DQ, канонический контракт и модели.

ERA ожидает дату, well_id и распознаваемый event_type или описание; доп. поля: work_type, reason, equipment, result, failure_type, cause, corrosion_detected, repair, downtime_days.

## JSON расписания

~~~json
{
  "crew_availability": [{
    "crew_id": "BRIGADE-17",
    "specialties": ["corrosion inspection"],
    "region": "north",
    "status": "available",
    "available_from": "2026-09-29T08:00:00+03:00",
    "available_to": "2026-09-29T20:00:00+03:00"
  }]
}
~~~

Допустимый status: available, busy или unknown. Backend добавит source_id. Не отправляйте личные данные сотрудников.

## Workflow n8n без email

- well_analysis_operator.json: Form Trigger → источники/readiness → анализ → расписание → LiteLLM → сохранение в PostgreSQL и ответ формы. Используется responseMode lastNode; Respond to Webhook отсутствует.
- scheduled_risk_monitor.json: расписание → полный цикл → только alerts → паспорт/история → бригады → LiteLLM → сохранение отчёта для BI. Email-узлов нет.

Около каждого рабочего блока на холсте есть заметка с назначением, входом и выходом. При включённом MAI_API_KEY задайте HTTP Header Auth credential с заголовком X-API-Key всем HTTP Request nodes FastAPI. Дашборд показывает рейтинг и статусы источников.
