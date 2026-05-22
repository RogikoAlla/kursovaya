import json
import math
from collections import defaultdict, deque

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
    else:
        block_width = hw['tcam_width']
        block_depth = hw['tcam_depth']
    
    blocks_per_entry = math.ceil(table['width'] / block_width)
    total_rows = table['entries'] * blocks_per_entry
    blocks_needed = math.ceil(total_rows / block_depth)
    return blocks_needed

def build_dependency_graph(tables):
    """Строит граф зависимостей."""
    depends_on = defaultdict(list)   # таблица -> список (предок, тип)
    dependents = defaultdict(list)   # таблица -> список (потомок, тип)
    
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

def compute_levels(tables, depends_on):
    """
    Вычисляет уровень для каждой таблицы.
    Уровень = длина самого длинного пути от любого источника.
    Таблицы без зависимостей имеют уровень 0.
    """
    # Строим граф в прямом направлении (предок -> потомок)
    graph = defaultdict(list)
    for child, preds in depends_on.items():
        for pred, _ in preds:
            graph[pred].append(child)
    
    # Находим все вершины
    all_nodes = {t['name'] for t in tables}
    
    # Инициализируем уровни
    level = {node: 0 for node in all_nodes}
    
    # Топологическая сортировка (Kahn's algorithm)
    in_degree = {node: 0 for node in all_nodes}
    for node in all_nodes:
        for child in graph[node]:
            in_degree[child] += 1
    
    queue = deque([node for node in all_nodes if in_degree[node] == 0])
    
    while queue:
        node = queue.popleft()
        for child in graph[node]:
            level[child] = max(level[child], level[node] + 1)
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)
    
    return level

def can_place_fragment_ffls(table_name, stage_idx, table_to_fragments, depends_on, dependents):
    """
    Проверяет, можно ли поместить фрагмент таблицы в стадию.
    
    Правила:
    1. Match и reverse_match: строгое разделение (предок < потомок)
    2. Action и successor: можно в одной стадии (предок <= потомок)
    """
    # Проверка зависимостей с предками
    if table_name in depends_on:
        for pred_name, dep_type in depends_on[table_name]:
            if pred_name in table_to_fragments and table_to_fragments[pred_name]:
                pred_stages = [f['stage'] for f in table_to_fragments[pred_name]]
                max_pred_stage = max(pred_stages)
                
                if dep_type == 'match' or dep_type == 'reverse_match':
                    if stage_idx <= max_pred_stage:
                        return False
                else:
                    if stage_idx < max_pred_stage:
                        return False
    
    # Проверка зависимостей с потомками
    if table_name in dependents:
        for child_name, dep_type in dependents[table_name]:
            if child_name in table_to_fragments and table_to_fragments[child_name]:
                child_stages = [f['stage'] for f in table_to_fragments[child_name]]
                min_child_stage = min(child_stages)
                
                if dep_type == 'match' or dep_type == 'reverse_match':
                    if stage_idx >= min_child_stage:
                        return False
                else:
                    if stage_idx > min_child_stage:
                        return False
    
    return True

def ffls_mapping(tables, hw, enable_splitting=True):
    """
    Алгоритм FFLS (First Fit by Level and Size) с поддержкой разрезания.
    
    Этапы:
    1. Вычисление уровней таблиц (длина цепочки зависимостей)
    2. Сортировка: по уровню (возрастание), затем по размеру (убывание)
    3. Размещение в порядке сортировки (First Fit)
    """
    # Считаем блоки для каждой таблицы
    for table in tables:
        table['blocks_total'] = calc_blocks_needed(table, hw)
    
    # Строим граф зависимостей
    depends_on, dependents = build_dependency_graph(tables)
    
    # Вычисляем уровни
    level = compute_levels(tables, depends_on)
    
    # Добавляем уровень к каждой таблице
    for table in tables:
        table['level'] = level[table['name']]
    
    # Сортировка: сначала по уровню (возрастание), потом по размеру (убывание)
    sorted_tables = sorted(tables, key=lambda t: (t['level'], -t['blocks_total']))
    
    stages = []
    table_to_fragments = defaultdict(list)
    
    print("\n" + "="*60)
    print("АЛГОРИТМ FFLS (First Fit by Level and Size)")
    print("="*60)
    
    print("\nУРОВНИ ТАБЛИЦ:")
    for table in sorted_tables:
        print(f"  {table['name']}: уровень={table['level']}, блоков={table['blocks_total']}")
    
    print("\n" + "="*60)
    print("ПРОЦЕСС РАЗМЕЩЕНИЯ")
    print("="*60)
    
    for table in sorted_tables:
        mem_type = 'sram' if table['match_type'] == 'exact' else 'tcam'
        mem_name = "SRAM" if mem_type == 'sram' else "TCAM"
        blocks_total = table['blocks_total']
        max_blocks_per_stage = hw[f'{mem_type}_blocks']
        
        print(f"\n▶ Таблица: {table['name']} (уровень={table['level']})")
        print(f"   match_type={table['match_type']} → {mem_name}")
        print(f"   нужно блоков: {blocks_total}")
        
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
                
                if can_place_fragment_ffls(table['name'], stage_idx, table_to_fragments,
                                           depends_on, dependents):
                    take = min(blocks_remaining, free_space)
                    stage[mem_type] += take
                    stage['tables'].append(f"{table['name']}[{take}]")
                    fragments_placed.append({'stage': stage_idx, 'blocks': take})
                    blocks_remaining -= take
                    placed = True
                    print(f"   → фрагмент {len(fragments_placed)}: {take} блоков в стадию {stage_idx}")
                    break
            
            # Если не поместился - создаём новую стадию
            if not placed:
                new_stage_idx = len(stages)
                new_stage = {'sram': 0, 'tcam': 0, 'tables': []}
                
                if can_place_fragment_ffls(table['name'], new_stage_idx, table_to_fragments,
                                           depends_on, dependents):
                    take = min(blocks_remaining, max_blocks_per_stage)
                    new_stage[mem_type] = take
                    new_stage['tables'].append(f"{table['name']}[{take}]")
                    stages.append(new_stage)
                    fragments_placed.append({'stage': new_stage_idx, 'blocks': take})
                    blocks_remaining -= take
                    placed = True
                    print(f"   → фрагмент {len(fragments_placed)}: {take} блоков в НОВУЮ стадию {new_stage_idx}")
                else:
                    print(f"   ❌ НЕВОЗМОЖНО РАЗМЕСТИТЬ: зависимости не позволяют")
                    break
        
        # Сохраняем фрагменты
        for frag in fragments_placed:
            table_to_fragments[table['name']].append({
                'stage': frag['stage'],
                'blocks': frag['blocks']
            })
    
    return stages, table_to_fragments, depends_on, dependents

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
    print("ИСХОДНЫЕ ТАБЛИЦЫ")
    print("="*60)
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
    
    # Запускаем алгоритм FFLS
    stages, table_to_fragments, depends_on, dependents = ffls_mapping(tables_data, hw)
    
    print("\n" + "="*60)
    print("РЕЗУЛЬТАТ")
    print("="*60)
    print(f"Использовано стадий: {len(stages)}")
    for i, stage in enumerate(stages):
        print(f"\nСтадия {i}:")
        print(f"  SRAM: {stage['sram']}/{hw['sram_blocks']} блоков")
        print(f"  TCAM: {stage['tcam']}/{hw['tcam_blocks']} блоков")
        print(f"  Таблицы: {', '.join(stage['tables'])}")
    
    # Детализация фрагментов
    print("\n" + "="*60)
    print("ДЕТАЛИЗАЦИЯ ФРАГМЕНТОВ")
    print("="*60)
    for table_name, fragments in table_to_fragments.items():
        if len(fragments) > 1:
            frag_str = " → ".join([f"ст.{f['stage']}({f['blocks']}б)" for f in fragments])
            print(f"  {table_name}: {frag_str}")
        else:
            print(f"  {table_name}: ст.{fragments[0]['stage']} ({fragments[0]['blocks']} блоков)")
    
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
    #print(f"\n⏱️ Общее время: {(time.time() - start_total)*1000:.2f} мс")

if __name__ == '__main__':
    main()