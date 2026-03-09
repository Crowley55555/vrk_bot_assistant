# Фильтры по сценариям перед выдачей

Перед поиском в ChromaDB в `_build_where_filter` и `_validate_product` используются только ключи из **METADATA_FILTER_KEYS**:  
`product_type`, `location`, `size_group`, `material`, `main_category`, `regulated`, `form`, `scenario_block`.

Шаги воронки (step_id) пишутся в `active_filters[step_id]`, но в **where** попадают только те, что есть в метаданных БД. Остальные (ac_type, slot_mount, part_type и т.д.) влияют только на **allowed_subcats**.

---

## 1. Вентиляционные решётки (grille)

### 1.1 Вход в сценарий
- **active_filters:** `product_type = grille`
- **allowed_subcats:** все slug из SUBCATEGORY_RULES

### 1.2 Шаг «Где установлена» (location)
- **active_filters:** + `location` = outdoor | indoor | duct
- **allowed_subcats:** пересчёт по `_filter_subcats_by_location(location)`

### 1.3 Ветка «На фасад» (detail_branch = facade)
После прохождения FACADE_STEPS поиск идёт в **`_detail_search`**:
- **allowed_subcats:**
  - при **Инерционная** — только slug с `feature == "inertial"` (reshetki-inertsionnye);
  - иначе — не меняется (outdoor без инерционных).
- **active_filters:**
  - `form` — из facade_form (прямоугольные/квадратные/круглые); для инерционных сбрасывается;
  - `regulated` — из facade_regulated: regulated | fixed; для инерционных = fixed.
- Исключение: накладная + регулируемая — поиск не выполняется, ответ «таких решёток нет».

### 1.4 Ветка «В помещении» (detail_branch = indoor)
Поиск после INDOOR_STEPS через **`_do_filtered_search`** (без отдельной подстановки фильтров из detail_answers в active_filters по коду).

### 1.5 «В воздуховод»
- **active_filters:** + `form = cylindrical`
- **allowed_subcats:** все SUBCATEGORY_RULES (фильтр по форме в метаданных).

### 1.6 Динамический Smart Routing (mount / feature)
- **allowed_subcats:** сужаются по `_filter_subcats_by_mount` и `_filter_subcats_by_feature`;
- **active_filters:** + `regulated` = regulated | fixed при выборе «Регулируемая»/«Нерегулируемая».

### Что уходит в where (grille)
- product_type, location, size_group, form (если есть), regulated (если regulated; fixed не в where, только в _validate_product);
- scenario_block = grille_{location}_{size_group}[_{form} для outdoor];
- category in allowed_subcats.

---

## 2. Щелевые решётки (slot_grille)

### Вход в сценарий
- **active_filters:** `product_type = slot_grille`
- **allowed_subcats:** все slug из CATEGORY_SLUG_MAP с main_category = slot_grille (4 slug).

### Шаги
- **active_filters:** + `slot_mount`, `slot_ceiling_type`, `size_group`  
  В METADATA_FILTER_KEYS нет slot_mount/slot_ceiling_type → в where не попадают.
- **allowed_subcats:** перед поиском пересчитываются в **`_do_filtered_search`** через  
  `_filter_slot_grille_subcats(subcats, session["active_filters"])` по SLOT_GRILLE_SUBCAT_FILTER.

### Что уходит в where (slot_grille)
- product_type, size_group;
- category in allowed_subcats (после фильтра по типу монтажа/потолка);
- scenario_block для slot не задаётся (loc в active_filters не ставится) — поиск по category + product_type + size_group.

---

## 3. Диффузоры (diffuser)

### Вход в сценарий
- **active_filters:** `product_type = diffuser`
- **allowed_subcats:** [] (фильтр по подкатегориям не используется).

### Шаги
- **active_filters:** + `location` = indoor, + `size_group`.

### Что уходит в where (diffuser)
- product_type, location, size_group;
- scenario_block = diffuser_indoor_{size_group};
- allowed_subcats не используется.

---

## 4. Корзины для кондиционеров (ac_basket)

### Вход в сценарий
- **active_filters:** `product_type = ac_basket`
- **allowed_subcats:** []

### Шаг ac_type
- **active_filters:** + `ac_type` = basket | screen | bracket (в where не идёт).
- **allowed_subcats:** AC_BASKET_SUBCAT_FILTER[chosen_value] (корзины / экраны+панели / кронштейны).

### Шаг size_group
- **active_filters:** + `size_group`.

### Что уходит в where (ac_basket)
- product_type, size_group;
- category in allowed_subcats;
- scenario_block по product_type + location + size_group (location для ac_basket в воронке не задаётся — может быть unknown в БД).

---

## 5. Воздухораспределители (distributor)

### Вход в сценарий
- **active_filters:** `product_type = distributor`
- **allowed_subcats:** []

### Шаг distributor_type
- **active_filters:** + `distributor_type` = panel | low_speed | disk | clean_rooms (в where не идёт).
- **allowed_subcats:** DISTRIBUTOR_SUBCAT_FILTER[chosen_value].

### Шаг size_group
- **active_filters:** + `size_group`.

### Что уходит в where (distributor)
- product_type, size_group;
- category in allowed_subcats;
- scenario_block = distributor_{location}_{size_group} (location из БД/парсера, в воронке не спрашивается).

---

## 6. Детали систем вентиляции (vent_parts)

### Вход в сценарий
- **active_filters:** `product_type = vent_parts`
- **allowed_subcats:** []

### Шаг part_type
- **active_filters:** + `part_type` = adapter | silencer | valve (в where не идёт).
- **allowed_subcats:** VENT_PARTS_SUBCAT_FILTER[chosen_value].

### Шаг size_group
- **active_filters:** + `size_group`.

### Что уходит в where (vent_parts)
- product_type, size_group;
- category in allowed_subcats;
- scenario_block по product_type + location + size_group.

---

## Сводка: кто куда попадает

| Сценарий   | В active_filters из шагов (все)     | В where (только из METADATA_FILTER_KEYS)     | allowed_subcats источник                    |
|-----------|--------------------------------------|----------------------------------------------|---------------------------------------------|
| grille    | location, size_group, form, regulated| product_type, location, size_group, form, regulated, scenario_block | SUBCATEGORY_RULES; для фасада — facade или outdoor без inertial |
| slot_grille | slot_mount, slot_ceiling_type, size_group | product_type, size_group                     | 4 slug slot → _filter_slot_grille_subcats    |
| diffuser  | location, size_group                 | product_type, location, size_group, scenario_block | не используется                             |
| ac_basket | ac_type, size_group                  | product_type, size_group, scenario_block     | AC_BASKET_SUBCAT_FILTER                     |
| distributor | distributor_type, size_group        | product_type, size_group, scenario_block     | DISTRIBUTOR_SUBCAT_FILTER                   |
| vent_parts | part_type, size_group               | product_type, size_group, scenario_block    | VENT_PARTS_SUBCAT_FILTER                    |

Место вызова поиска:
- **grille, фасад:** `_detail_search` (подстановка form, regulated, allowed_subcats для инерционных).
- **grille, остальное; все остальные сценарии:** `_do_filtered_search` → `_search_with_fallback(query, active_filters, scenario, subcats)`.
