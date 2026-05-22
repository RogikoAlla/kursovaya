import json
import math
from collections import defaultdict

def read_hardware(file_path):
    """Читает JSON с описанием железа."""
    with open(file_path, 'r') as f:
        data = json.load(f)
    
    stage = data['StageDescription']
    sram = stage['SRAMResources']
    tcam = stage['TCAMMatResources']['PerTCAMMatBlockSpec']
    
    hardware = {
        'sram_blocks': sram['MemoryBlockCount'],
        'sram_width': sram['MemoryBlockBitWidth'],
        'sram_depth': sram['MemoryBlockRowCount'],
        'tcam_blocks': stage['TCAMMatResources']['BlockCount'],
        'tcam_width': tcam['TCAMBitWidth'],
        'tcam_depth': tcam['TCAMRowCount'],
    }
    return hardware

def read_tables(file_path):
    """Читает JSON с описанием таблиц."""
    with open(file_path, 'r') as f:
        data = json.load(f)
    return data['tables']

def calc_blocks_needed(table, hw):
    """Считает, сколько блоков памяти нужно таблице."""
    if table['match_type'] == 'exact':
        block_width = hw['sram_width']
        block_depth = hw['sram_depth']
    else:  # ternary or lpm
        block_width = hw['tcam_width']
        block_depth = hw['tcam_depth']
    
    blocks_per_entry = math.ceil(table['width'] / block_width)
    total_rows = table['entries'] * blocks_per_entry
    blocks_needed = math.ceil(total_rows / block_depth)
    return blocks_needed

def build_dependency_graph(tables):
    """
    Строит граф зависимостей.
    Возвращает:
        depends_on: словарь {таблица: список (предок, тип)}
        dependents: словарь {таблица: список (потомок, тип)}
    """
    depends_on = defaultdict(list)   # кто от кого зависит
    dependents = defaultdict(list)   # на кого влияет
    
    table_names = {t['name']: t for t in tables}
    
    for table in tables:
        if 'dependencies' in table:
            for dep_info in table['dependencies']:
                if isinstance(dep_info, dict):
                    dep_name = dep_info['table']
                    dep_type = dep_info['type']
                else:
                    dep_name = dep_info
                    dep_type = 'unknown'
                
                if dep_name in table_names:
                    depends_on[table['name']].append((dep_name, dep_type))
                    dependents[dep_name].append((table['name'], dep_type))
    
    return depends_on, dependents

def can_place_fragment(table_name, stage_idx, table_to_fragments, depends_on, dependents):
    """
    Проверяет, можно ли поместить фрагмент таблицы в стадию.
    
    Правила:
    1. Match и reverse_match: строгое разделение (предок < потомок)
    2. Action и successor: можно в одной стадии (предок <= потомок)
    """
    # Проверка зависимостей с предками (от кого зависит эта таблица)
    if table_name in depends_on:
        for pred_name, dep_type in depends_on[table_name]:
            if pred_name in table_to_fragments and table_to_fragments[pred_name]:
                # Находим последнюю стадию предка (максимальную)
                pred_stages = [f['stage'] for f in table_to_fragments[pred_name]]
                max_pred_stage = max(pred_stages)
                
                if dep_type == 'match' or dep_type == 'reverse_match':
                    # Строгое разделение: текущая стадия > последней стадии предка
                    if stage_idx <= max_pred_stage:
                        return False
                else:  # action, successor
                    # Можно в одной стадии: текущая стадия >= последней стадии предка
                    if stage_idx < max_pred_stage:
                        return False
    
    # Проверка зависимостей с потомками (кто зависит от этой таблицы)
    if table_name in dependents:
        for child_name, dep_type in dependents[table_name]:
            if child_name in table_to_fragments and table_to_fragments[child_name]:
                # Находим первую стадию потомка (минимальную)
                child_stages = [f['stage'] for f in table_to_fragments[child_name]]
                min_child_stage = min(child_stages)
                
                if dep_type == 'match' or dep_type == 'reverse_match':
                    # Строгое разделение: текущая стадия < первой стадии потомка
                    if stage_idx >= min_child_stage:
                        return False
                else:  # action, successor
                    # Можно в одной стадии: текущая стадия <= первой стадии потомка
                    if stage_idx > min_child_stage:
                        return False
    
    return True

def try_place_table(table, stages, hw, table_to_fragments, depends_on, dependents):
    """
    Пытается разместить таблицу (с разрезанием) в существующих стадиях.
    Возвращает True, если таблица полностью размещена.
    """
    table_name = table['name']
    mem_type = 'sram' if table['match_type'] == 'exact' else 'tcam'
    mem_name = "SRAM" if mem_type == 'sram' else "TCAM"
    blocks_total = table['blocks_total']
    max_blocks_per_stage = hw[f'{mem_type}_blocks']
    
    print(f"\n  Попытка разместить {table_name} (нужно {blocks_total} блоков {mem_name})")
    
    blocks_remaining = blocks_total
    fragments_placed = []
    
    # Пытаемся разместить фрагменты
    while blocks_remaining > 0:
        placed = False
        
        # Пробуем существующие стадии
        for stage_idx, stage in enumerate(stages):
            free_space = max_blocks_per_stage - stage[mem_type]
            if free_space <= 0:
                continue
            
            if can_place_fragment(table_name, stage_idx, table_to_fragments, 
                                  depends_on, dependents):
                take = min(blocks_remaining, free_space)
                stage[mem_type] += take
                stage['tables'].append(f"{table_name}[{take}]")
                fragments_placed.append({'stage': stage_idx, 'blocks': take})
                blocks_remaining -= take
                placed = True
                print(f"    → фрагмент {len(fragments_placed)}: {take} блоков в стадию {stage_idx}")
                break
        
        # Если не поместился в существующие стадии
        if not placed:
            # Создаём новую стадию
            new_stage_idx = len(stages)
            new_stage = {
                'sram': 0,
                'tcam': 0,
                'tables': []
            }
            
            if can_place_fragment(table_name, new_stage_idx, table_to_fragments,
                                  depends_on, dependents):
                take = min(blocks_remaining, max_blocks_per_stage)
                new_stage[mem_type] = take
                new_stage['tables'].append(f"{table_name}[{take}]")
                stages.append(new_stage)
                fragments_placed.append({'stage': new_stage_idx, 'blocks': take})
                blocks_remaining -= take
                placed = True
                print(f"    → фрагмент {len(fragments_placed)}: {take} блоков в НОВУЮ стадию {new_stage_idx}")
            else:
                print(f"    ❌ НЕВОЗМОЖНО РАЗМЕСТИТЬ: зависимости не позволяют создать новую стадию")
                return False
    
    # Сохраняем информацию о размещённых фрагментах
    for frag in fragments_placed:
        table_to_fragments[table_name].append({
            'stage': frag['stage'], 
            'blocks': frag['blocks']
        })
    
    return True

def ffd_mapping_with_splitting(tables, hw):
    """
    Многопроходный жадный алгоритм FFD с поддержкой горизонтального разрезания таблиц.
    
    Алгоритм:
    1. Сортируем таблицы по убыванию требуемых блоков (классический FFD)
    2. Многопроходный цикл:
       - Проходим по всем ещё не размещённым таблицам
       - Если все предки таблицы уже размещены → пытаемся разместить
       - Если предки ещё не размещены → пропускаем (откладываем на следующий проход)
    3. Если за проход не размещено ни одной таблицы → тупик (невозможно разместить)
    """
    # Считаем блоки для каждой таблицы
    for table in tables:
        table['blocks_total'] = calc_blocks_needed(table, hw)
    
    # Сортируем по убыванию требуемых блоков (классический FFD)
    sorted_tables = sorted(tables, key=lambda t: t['blocks_total'], reverse=True)
    
    # Строим граф зависимостей
    depends_on, dependents = build_dependency_graph(tables)
    
    stages = []
    table_to_fragments = defaultdict(list)  # имя таблицы -> список фрагментов
    placed_tables = set()  # имена полностью размещённых таблиц
    
    print("\n" + "="*60)
    print("ПРОЦЕСС РАЗМЕЩЕНИЯ (многопроходный FFD с разрезанием)")
    print("="*60)
    
    # Список таблиц, которые ещё не размещены
    remaining = list(sorted_tables)
    pass_num = 0
    
    while remaining:
        pass_num += 1
        print(f"\n--- ПРОХОД {pass_num} ---")
        print(f"Осталось разместить: {[t['name'] for t in remaining]}")
        
        progress_made = False
        
        for table in remaining[:]:  # проходим по копии списка
            table_name = table['name']
            
            # Проверка: все ли предки уже размещены?
            all_preds_placed = True
            missing_preds = []
            
            if table_name in depends_on:
                for pred_name, _ in depends_on[table_name]:
                    if pred_name not in placed_tables:
                        all_preds_placed = False
                        missing_preds.append(pred_name)
            
            if not all_preds_placed:
                print(f"\n  {table_name}: пропуск (жду {missing_preds})")
                continue
            
            # Все предки размещены → пробуем разместить таблицу
            print(f"\n  {table_name}: все предки размещены, пробуем разместить")
            
            if try_place_table(table, stages, hw,table_to_fragments, depends_on, dependents):
                remaining.remove(table)
                placed_tables.add(table_name)
                progress_made = True
                print(f"  ✅ {table_name} полностью размещена")
            else:
                print(f"  ❌ {table_name} не удалось разместить (тупик)")
                # Выходим из цикла, так как дальше размещать бессмысленно
                return stages, table_to_fragments, depends_on, dependents, remaining
        
        # Если за проход не разместили ни одной таблицы - тупик
        if not progress_made and remaining:
            print("\n⚠️ ТУПИК: не удалось разместить оставшиеся таблицы")
            print(f"   Неразмещённые таблицы: {[t['name'] for t in remaining]}")
            break
    
    return stages, table_to_fragments, depends_on, dependents, remaining

def main():
    #import time
    #start_total = time.time()
    
    # Загружаем описание железа
    hw = read_hardware('hardware.json')
    
    print("="*60)
    print("ХАРАКТЕРИСТИКИ ЖЕЛЕЗА")
    print("="*60)
    print(f"SRAM: {hw['sram_blocks']} блоков по {hw['sram_width']}×{hw['sram_depth']}")
    print(f"TCAM: {hw['tcam_blocks']} блоков по {hw['tcam_width']}×{hw['tcam_depth']}")
    
    # Загружаем таблицы
    file_path = input("Введите путь к JSON файлу с таблицами: ")
    tables_data = read_tables(file_path)
    
    print("\n" + "="*60)
    print("ИСХОДНЫЕ ТАБЛИЦЫ (сортировка FFD по размеру)")
    print("="*60)
    
    # Вычисляем блоки для вывода
    for t in tables_data:
        blocks = calc_blocks_needed(t, hw)
        deps_str = ""
        if 'dependencies' in t:
            deps_list = []
            for d in t['dependencies']:
                if isinstance(d, dict):
                    deps_list.append(f"{d['table']}({d['type']})")
                else:
                    deps_list.append(d)
            deps_str = f", зависит от: {deps_list}"
        mem_type = "SRAM" if t['match_type'] == 'exact' else "TCAM"
        print(f"  {t['name']}: {t['match_type']} → {mem_type}, "
              f"записей={t['entries']}, нужно блоков={blocks}{deps_str}")
    
    # Запускаем алгоритм
    stages, table_to_fragments, depends_on, dependents, remaining = ffd_mapping_with_splitting(tables_data, hw)
    
    print("\n" + "="*60)
    print("РЕЗУЛЬТАТ")
    print("="*60)
    print(f"Использовано стадий: {len(stages)}")
    for i, stage in enumerate(stages):
        print(f"\nСтадия {i}:")
        print(f"  SRAM: {stage['sram']}/{hw['sram_blocks']} блоков")
        print(f"  TCAM: {stage['tcam']}/{hw['tcam_blocks']} блоков")
        print(f"  Таблицы: {', '.join(stage['tables'])}")
    
    # Вывод детализации фрагментов
    print("\n" + "="*60)
    print("ДЕТАЛИЗАЦИЯ ФРАГМЕНТОВ")
    print("="*60)
    for table_name, fragments in table_to_fragments.items():
        if len(fragments) > 1:
            frag_str = " → ".join([f"ст.{f['stage']}({f['blocks']}б)" for f in fragments])
            print(f"  {table_name}: {frag_str}")
        else:
            print(f"  {table_name}: ст.{fragments[0]['stage']} ({fragments[0]['blocks']} блоков)")
    
    # Вывод неразмещённых таблиц
    if remaining:
        print("\n⚠️ НЕРАЗМЕЩЁННЫЕ ТАБЛИЦЫ:")
        for t in remaining:
            print(f"  - {t['name']}")
    
    # Проверка зависимостей
    print("\n" + "="*60)
    print("ПРОВЕРКА ЗАВИСИМОСТЕЙ")
    print("="*60)
    
    all_deps = []
    for table, preds in depends_on.items():
        for pred, dep_type in preds:
            all_deps.append((pred, table, dep_type))
    
    for pred, succ, dep_type in all_deps:
        if pred in table_to_fragments and succ in table_to_fragments:
            pred_stages = [f['stage'] for f in table_to_fragments[pred]]
            succ_stages = [f['stage'] for f in table_to_fragments[succ]]
            max_pred = max(pred_stages)
            min_succ = min(succ_stages)
            
            if dep_type == 'match' or dep_type == 'reverse_match':
                ok = max_pred < min_succ
                requirement = f"макс({max_pred}) < мин({min_succ})"
            else:
                ok = max_pred <= min_succ
                requirement = f"макс({max_pred}) <= мин({min_succ})"
            
            status = "✅" if ok else "❌"
            arrow = "←" if dep_type == 'reverse_match' else "→"
            print(f"  {pred}{arrow} {succ} ({dep_type}): {requirement} {status}")
            if not ok:
                print(f"      Предикат: фрагменты {pred} в стадиях {pred_stages}")
                print(f"      Потомок: фрагменты {succ} в стадиях {succ_stages}")
        else:
            print(f"  {pred} → {succ}: ⚠️ не все таблицы размещены")
    #print(f"\n⏱️ Общее время: {(time.time() - start_total)*1000:.2f} мс")

if __name__ == '__main__':
    main()