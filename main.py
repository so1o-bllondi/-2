import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from xml.etree import ElementTree as ET
from typing import Dict, List, Set, Optional

_NUGET_CACHE = {}
_APT_CACHE: Dict[str, List[str]] = {}

#  Парсинг аргументов командной строки
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Визуализация графа зависимостей для менеджера пакетов"
    )
    parser.add_argument(
        "--package", "-p",
        type=str,
        default=None,
        help="Имя анализируемого пакета",
    )
    parser.add_argument(
        "--repository", "-r",
        type=str,
        default=None,
        help="URL репозитория или путь к файлу тестового репозитория",
    )
    parser.add_argument(
        "--mode", "-m",
        type=str,
        choices=["test", "nuget", "apt"],
        default=None,
        help="Режим работы: test (тестовый репозиторий), nuget, apt (Ubuntu)",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default="graph",
        help="Имя сгенерированного файла с изображением графа (без расширения)",
    )
    parser.add_argument(
        "--ascii-tree",
        action="store_true",
        help="Режим вывода зависимостей в формате ASCII-дерева",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=None,
        metavar="N",
        help="Максимальная глубина анализа зависимостей (0 = без ограничения)",
    )
    parser.add_argument(
        "--filter",
        type=str,
        default=None,
        metavar="SUBSTRING",
        help="Подстрока для фильтрации пакетов (исключать пакеты, содержащие подстроку)",
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default="config.json",
        help="Путь к файлу конфигурации",
    )
    return parser.parse_args()


#  Загрузка конфигурации (из файла; значения по умолчанию для отсутствующих ключей)
def load_config(path: str = "config.json") -> dict:
    defaults = {
        "package_name": "",
        "repository": "",
        "mode": "nuget",
        "filter_substring": "",
        "output_file": "graph",
        "ascii_tree": False,
        "max_depth": 0,
    }
    if not os.path.exists(path):
        return dict(defaults)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Ошибка разбора config.json: {e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Ошибка чтения '{path}': {e}", file=sys.stderr)
        sys.exit(1)

    for key, default in defaults.items():
        if key not in cfg:
            cfg[key] = default
    if cfg["mode"] not in ("test", "nuget", "apt"):
        print("mode должен быть 'test', 'nuget' или 'apt'", file=sys.stderr)
        sys.exit(1)
    return cfg


def merge_config(config: dict, args: argparse.Namespace) -> dict:
    """Объединить конфиг из файла с параметрами командной строки."""
    out = dict(config)
    if args.package is not None:
        out["package_name"] = args.package
    if args.repository is not None:
        out["repository"] = args.repository
    if args.mode is not None:
        out["mode"] = args.mode
    if args.output is not None:
        out["output_file"] = args.output
    out["ascii_tree"] = getattr(args, "ascii_tree", False) or out.get("ascii_tree", False)
    if args.max_depth is not None:
        out["max_depth"] = args.max_depth
    elif "max_depth" not in out:
        out["max_depth"] = 0
    if args.filter is not None:
        out["filter_substring"] = args.filter
    return out


def validate_config(cfg: dict) -> None:
    """Обработка ошибок для параметров конфигурации."""
    if not cfg.get("package_name"):
        print("Ошибка: имя пакета не задано (--package или package_name в config).", file=sys.stderr)
        sys.exit(1)
    if cfg["mode"] == "test" and not cfg.get("repository"):
        print("Ошибка: в режиме test необходимо указать repository (путь к файлу).", file=sys.stderr)
        sys.exit(1)
    if cfg["mode"] == "apt" and not os.path.exists("/var/lib/dpkg/status") and not _apt_available():
        print("Ошибка: режим apt доступен только в среде Ubuntu/Debian с установленным dpkg/apt.", file=sys.stderr)
        sys.exit(1)
    max_d = cfg.get("max_depth", 0)
    if not isinstance(max_d, int) or max_d < 0:
        print("Ошибка: max_depth должен быть неотрицательным целым числом.", file=sys.stderr)
        sys.exit(1)

def _apt_available() -> bool:
    try:
        subprocess.run(
            ["apt-cache", "show", "--no-all-versions", "coreutils"],
            capture_output=True,
            timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


#  Загрузка тестового репозитория
def load_test_repo(path: str) -> Dict[str, List[str]]:
    if not os.path.exists(path):
        print(f"Ошибка: тестовый репозиторий '{path}' не найден.", file=sys.stderr)
        sys.exit(1)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Нормализуем: все значения — списки строк
        return {str(k): [str(d) for d in (v if isinstance(v, list) else [])] for k, v in data.items()}
    except Exception as e:
        print(f"Ошибка загрузки тестового репозитория: {e}", file=sys.stderr)
        sys.exit(1)
#  Заглушки для NuGet (не используется в тесте)
def get_direct_deps_nuget(pkg: str) -> List[str]:

    # Кэширование: если уже загружали — сразу вернуть
    if pkg in _NUGET_CACHE:
        return _NUGET_CACHE[pkg]

    try:
        pkg_lower = pkg.lower()
        # Шаг 1: Получить список версий
        versions_url = f"https://api.nuget.org/v3-flatcontainer/{pkg_lower}/index.json"
        with urllib.request.urlopen(versions_url) as resp:
            versions_data = json.load(resp)
        versions = versions_data.get("versions", [])
        if not versions:
            _NUGET_CACHE[pkg] = []
            return []

        latest_version = versions[-1]

        # Шаг 2: Загрузить .nuspec
        nuspec_url = f"https://api.nuget.org/v3-flatcontainer/{pkg_lower}/{latest_version}/{pkg_lower}.nuspec"
        with urllib.request.urlopen(nuspec_url) as resp:
            nuspec_content = resp.read().decode("utf-8")

        # Шаг 3: Распарсить XML
        root = ET.fromstring(nuspec_content)
        ns = {"ns": "http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd"}
        deps = set()
        for dep in root.findall(".//ns:dependency", ns):
            dep_id = dep.get("id")
            if dep_id:
                deps.add(dep_id)
        deps = list(deps)

        # Сохраняем в кэш
        _NUGET_CACHE[pkg] = deps
        return deps

    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"Пакет '{pkg}' не найден в NuGet.", file=sys.stderr)
        else:
            print(f"HTTP ошибка для '{pkg}': {e}", file=sys.stderr)
        _NUGET_CACHE[pkg] = []
        return []
    except Exception as e:
        print(f"Ошибка при загрузке '{pkg}' из NuGet: {e}", file=sys.stderr)
        _NUGET_CACHE[pkg] = []
        return []


#  Зависимости в формате Ubuntu (apt) без сторонних библиотек
def _parse_apt_depends(depends_line: str) -> List[str]:
    """Парсит строку Depends из вывода apt-cache show. Возвращает список имён пакетов."""
    if not depends_line or not depends_line.strip():
        return []
    result = []
    # Разделяем по запятой (зависимости через | — альтернативы, берём первую)
    for part in depends_line.split(","):
        part = part.strip()
        # Альтернативы: "a | b | c" -> берём первую
        if "|" in part:
            part = part.split("|")[0].strip()
        # Убираем версию в скобках: "libc6 (>= 2.2)" -> "libc6"
        if "(" in part:
            part = part[: part.index("(")].strip()
        part = part.strip()
        if part and part not in result:
            result.append(part)
    return result


def get_direct_deps_apt(pkg: str) -> List[str]:
    """Получить прямые зависимости пакета через apt-cache (без сторонних библиотек)."""
    if pkg in _APT_CACHE:
        return _APT_CACHE[pkg]
    try:
        proc = subprocess.run(
            ["apt-cache", "show", "--no-all-versions", pkg],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0 or not proc.stdout:
            _APT_CACHE[pkg] = []
            return []
        depends_line = None
        for line in proc.stdout.splitlines():
            if line.startswith("Depends:"):
                depends_line = line[8:].strip()
                break
        if not depends_line:
            _APT_CACHE[pkg] = []
            return []
        deps = _parse_apt_depends(depends_line)
        _APT_CACHE[pkg] = deps
        return deps
    except FileNotFoundError:
        print("Ошибка: apt-cache не найден (режим apt только для Ubuntu/Debian).", file=sys.stderr)
        _APT_CACHE[pkg] = []
        return []
    except subprocess.TimeoutExpired:
        print(f"Ошибка: таймаут при запросе apt-cache для '{pkg}'.", file=sys.stderr)
        _APT_CACHE[pkg] = []
        return []
    except Exception as e:
        print(f"Ошибка при получении зависимостей apt для '{pkg}': {e}", file=sys.stderr)
        _APT_CACHE[pkg] = []
        return []


#  Основной обход DFS с обнаружением циклов (max_depth: 0 = без ограничения)
def dfs_build_graph(
    pkg: str,
    deps_func,
    filter_str: str,
    graph: Dict[str, List[str]],
    visited: Set[str],
    rec_stack: Set[str],
    max_depth: int = 0,
    current_depth: int = 0,
) -> bool:
    if pkg in rec_stack:
        print(f"Цикл обнаружен: {pkg}", file=sys.stderr)
        return True
    if pkg in visited:
        return False

    visited.add(pkg)
    rec_stack.add(pkg)

    raw_deps = deps_func(pkg)
    if filter_str == "":
        filtered = raw_deps
    else:
        filtered = [d for d in raw_deps if filter_str not in d]
    graph[pkg] = filtered

    # Ограничение глубины: при достижении max_depth не идём в детей
    if max_depth > 0 and current_depth >= max_depth:
        rec_stack.remove(pkg)
        return False

    cycle = False
    for dep in filtered:
        if dfs_build_graph(
            dep, deps_func, filter_str, graph, visited, rec_stack,
            max_depth=max_depth, current_depth=current_depth + 1,
        ):
            cycle = True

    rec_stack.remove(pkg)
    return cycle

#  Обратные зависимости
def get_reverse_deps(target: str, graph: Dict[str, List[str]]) -> Set[str]:
    # Строим обратный граф
    rev = {}
    for p, deps in graph.items():
        for d in deps:
            rev.setdefault(d, []).append(p)

    result = set()
    visited = set()

    def dfs_rev(node):
        if node in visited:
            return
        visited.add(node)
        for parent in rev.get(node, []):
            result.add(parent)
            dfs_rev(parent)

    dfs_rev(target)
    return result

#  ASCII-дерево зависимостей (path = текущий путь от корня для обнаружения циклов)
def to_ascii_tree(
    root: str,
    graph: Dict[str, List[str]],
    prefix: str = "",
    is_last: bool = True,
    path: Optional[Set[str]] = None,
) -> str:
    if path is None:
        path = set()
    if root in path:
        return prefix + ("└── " if is_last else "├── ") + root + " (цикл)\n"
    path.add(root)
    connector = "└── " if is_last else "├── "
    lines = [prefix + connector + root + "\n"]
    deps = graph.get(root, [])
    for i, dep in enumerate(deps):
        is_last_dep = i == len(deps) - 1
        sub_prefix = prefix + ("    " if is_last else "│   ")
        lines.append(to_ascii_tree(dep, graph, sub_prefix, is_last_dep, path))
    path.remove(root)
    return "".join(lines)


#  Mermaid
def to_mermaid(graph: Dict[str, List[str]]) -> str:
    lines = ["graph TD"]
    for pkg, deps in graph.items():
        for dep in deps:
            p = pkg.replace("-", "_").replace(".", "_")
            d = dep.replace("-", "_").replace(".", "_")
            if p[0].isdigit():
                p = "P" + p
            if d[0].isdigit():
                d = "P" + d
            lines.append(f"    {p} --> {d}")
    return "\n".join(lines)


#  PlantUML
def to_plantuml(graph: Dict[str, List[str]]) -> str:
    lines = ["@startuml", "skinparam defaultFontName Arial", ""]
    for pkg, deps in graph.items():
        for dep in deps:
            # Экранируем кавычки в названиях
            p = pkg.replace('"', '\\"')
            d = dep.replace('"', '\\"')
            lines.append(f'  "{p}" --> "{d}"')
    lines.append("@enduml")
    return "\n".join(lines)


#  Простой SVG (узлы и рёбра)
def to_svg(graph: Dict[str, List[str]], root: str) -> str:
    nodes = list(graph.keys()) if graph else [root]
    if root and root not in nodes:
        nodes.insert(0, root)
    node_id = {n: f"n{i}" for i, n in enumerate(nodes)}
    width, height = 800, 600
    box_w, box_h = 140, 28
    margin = 40
    # Простая сетка: один узел на строку по вертикали
    positions = {}
    for i, n in enumerate(nodes):
        x = margin + (i % 5) * (box_w + 50)
        y = margin + (i // 5) * (box_h + 40)
        positions[n] = (x, y)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">',
        "<defs><style>.node { fill: #e1f5fe; stroke: #01579b; } .edge { stroke: #37474f; fill: none; } .label { font: 12px sans-serif; }</style></defs>",
    ]
    for pkg, deps in graph.items():
        for dep in deps:
            if pkg in positions and dep in positions:
                x1, y1 = positions[pkg]
                x2, y2 = positions[dep]
                x1, y1 = x1 + box_w // 2, y1 + box_h
                x2, y2 = x2 + box_w // 2, y2
                lines.append(f'  <line class="edge" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}"/>')
    for n, (x, y) in positions.items():
        label = n[:20] + "…" if len(n) > 20 else n
        lines.append(f'  <rect class="node" x="{x}" y="{y}" width="{box_w}" height="{box_h}" rx="4"/>')
        lines.append(f'  <text class="label" x="{x+5}" y="{y+18}">{_svg_escape(label)}</text>')
    lines.append("</svg>")
    return "\n".join(lines)


def _svg_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

#  MAIN
def main():
    args = parse_args()
    config = load_config(args.config)
    config = merge_config(config, args)
    validate_config(config)

    package = config["package_name"]
    mode = config["mode"]
    repo_path = config["repository"]
    filter_str = config.get("filter_substring", "")
    output_base = config.get("output_file", "graph")
    ascii_tree_mode = config.get("ascii_tree", False)
    max_depth = config.get("max_depth", 0)

    # Этап 1: вывод всех параметров в формате ключ-значение
    print("Этап 1: Параметры конфигурации")
    for k, v in sorted(config.items()):
        print(f"  {k}: {v}")
    print()

    # Выбор источника зависимостей
    if mode == "test":
        test_repo = load_test_repo(repo_path)
        def get_deps(p): return test_repo.get(p, [])
    elif mode == "apt":
        get_deps = get_direct_deps_apt
    else:
        get_deps = get_direct_deps_nuget

    # Этап 2: прямые зависимости
    print("Этап 2: Прямые зависимости")
    direct = get_deps(package)
    for d in direct:
        print(f"  {d}")
    if not direct:
        print("  (нет)")
    print()

    # Этап 3: полный граф (с ограничением глубины и фильтром)
    print("Этап 3: Построение графа зависимостей")
    graph = {}
    visited = set()
    rec_stack = set()
    has_cycle = dfs_build_graph(
        package, get_deps, filter_str, graph, visited, rec_stack,
        max_depth=max_depth, current_depth=0,
    )
    if has_cycle:
        print("Обнаружены циклы в графе (см. сообщения выше).")
    else:
        print("Циклов не обнаружено.")
    print("Граф построен.")
    print()

    # Этап 4: обратные зависимости
    print("Этап 4: Обратные зависимости")
    rev_deps = get_reverse_deps(package, graph)
    if rev_deps:
        for r in sorted(rev_deps):
            print(f"  {r}")
    else:
        print("  (нет)")
    print()

    # Этап 5: Визуализация
    print("Этап 5: Визуализация")
    if ascii_tree_mode:
        print("ASCII-дерево зависимостей:")
        print(to_ascii_tree(package, graph))
    print("Mermaid:")
    print(to_mermaid(graph))
    print()

    # Сохранение SVG и PlantUML
    svg_path = output_base if output_base.endswith(".svg") else output_base + ".svg"
    base_name = output_base[:-4] if output_base.endswith(".svg") else output_base
    puml_path = base_name + ".puml"
    try:
        with open(svg_path, "w", encoding="utf-8") as f:
            f.write(to_svg(graph, package))
        print(f"Граф сохранён в SVG: {svg_path}")
    except OSError as e:
        print(f"Ошибка записи SVG: {e}", file=sys.stderr)
    try:
        with open(puml_path, "w", encoding="utf-8") as f:
            f.write(to_plantuml(graph))
        print(f"Граф сохранён в PlantUML: {puml_path}")
    except OSError as e:
        print(f"Ошибка записи PlantUML: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()