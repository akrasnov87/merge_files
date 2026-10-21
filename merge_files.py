#!/usr/bin/env python3
"""
Утилита для склейки файлов из папки (рекурсивно) в один или несколько файлов.
Без внешних зависимостей — только стандартная библиотека Python 3.
"""

__version__ = "1.3.0"

import argparse
import os
import re
import sys
from pathlib import Path


# Бинарные расширения — по умолчанию пропускаем
DEFAULT_BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".xz",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".class", ".pyc",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".flac",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".db", ".sqlite", ".sqlite3",
    ".jar", ".war",
    ".iso", ".img",
}

# Префикс «приватного» правила: для git это обычный комментарий,
# для утилиты — обычное правило игнорирования.
PRIVATE_IGNORE_PREFIX = "#!#"


# ---------------------------------------------------------------------------
# Реализация gitignore-подобных правил
# ---------------------------------------------------------------------------

class IgnoreRule:
    """Одно правило из gitignore."""

    __slots__ = ("pattern", "regex", "negated", "dir_only", "anchored",
                 "raw", "private")

    def __init__(self, raw: str, regex: re.Pattern, negated: bool,
                 dir_only: bool, anchored: bool, private: bool = False):
        self.raw = raw
        self.regex = regex
        self.negated = negated
        self.dir_only = dir_only
        self.anchored = anchored
        self.private = private

    def __repr__(self):
        return f"IgnoreRule({self.raw!r}, private={self.private})"


def _translate_glob_to_regex(glob: str) -> str:
    """Переводит gitignore-глоб в регулярное выражение (без якорей ^/$)."""
    i = 0
    n = len(glob)
    out = []

    while i < n:
        c = glob[i]

        if c == "*":
            if i + 1 < n and glob[i + 1] == "*":
                j = i + 2
                if j < n and glob[j] == "/":
                    out.append("(?:.*/)?")
                    i = j + 1
                    continue
                else:
                    out.append(".*")
                    i = j
                    continue
            else:
                out.append("[^/]*")
                i += 1
                continue

        elif c == "?":
            out.append("[^/]")
            i += 1
            continue

        elif c == "[":
            j = i + 1
            if j < n and glob[j] in ("!", "^"):
                j += 1
            if j < n and glob[j] == "]":
                j += 1
            while j < n and glob[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape("["))
                i += 1
                continue
            content = glob[i + 1:j]
            content = content.replace("\\", "\\\\")
            if content.startswith("!"):
                content = "^" + content[1:]
            out.append("[" + content + "]")
            i = j + 1
            continue

        elif c == "\\":
            if i + 1 < n:
                out.append(re.escape(glob[i + 1]))
                i += 2
            else:
                out.append(re.escape("\\"))
                i += 1
            continue

        else:
            out.append(re.escape(c))
            i += 1

    return "".join(out)


def _parse_one_pattern(line: str, private: bool) -> IgnoreRule | None:
    """Разбирает одну строку-шаблон в IgnoreRule. Возвращает None, если строка пустая."""
    stripped = line
    while stripped.endswith(" ") and not stripped.endswith("\\ "):
        stripped = stripped[:-1]
    if not stripped:
        return None
    line = stripped

    negated = False
    if line.startswith("!"):
        negated = True
        line = line[1:]
    elif line.startswith("\\!") or line.startswith("\\#"):
        line = line[1:]

    if not line:
        return None

    dir_only = line.endswith("/")
    if dir_only:
        line = line[:-1]
    if not line:
        return None

    anchored = line.startswith("/")
    if anchored:
        line = line[1:]

    if "/" in line:
        anchored = True

    try:
        rx_body = _translate_glob_to_regex(line)
        if anchored:
            regex = re.compile("^" + rx_body + r"(?:/.*)?$")
        else:
            regex = re.compile(r"(?:^|.*/)" + rx_body + r"(?:/.*)?$")
    except re.error:
        return None

    return IgnoreRule(line, regex, negated, dir_only, anchored, private)


def parse_ignore_file(path: Path, allow_private: bool = True) -> list[IgnoreRule]:
    """
    Разбирает файл .gitignore в список правил.

    Поддерживает «приватные» правила: строки, начинающиеся с ``#!#``.
    Для git такие строки — обычный комментарий, поэтому он их не видит,
    а утилита разбирает как обычные правила игнорирования.
    """
    rules: list[IgnoreRule] = []
    if not path.exists():
        return rules

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n").rstrip("\r")
            if not line:
                continue

            # Приватное правило: #!#...
            if allow_private and line.startswith(PRIVATE_IGNORE_PREFIX):
                payload = line[len(PRIVATE_IGNORE_PREFIX):]
                # Разрешаем один пробел после префикса для читаемости:
                # "#!# pear.key" и "#!#pear.key" эквивалентны.
                if payload.startswith(" "):
                    payload = payload[1:]
                rule = _parse_one_pattern(payload, private=True)
                if rule is not None:
                    rules.append(rule)
                continue

            # Обычный комментарий
            if line.startswith("#"):
                continue

            rule = _parse_one_pattern(line, private=False)
            if rule is not None:
                rules.append(rule)

    return rules


def is_ignored(rel_path: str, is_dir: bool, rules: list[IgnoreRule]) -> bool:
    """Проверяет, игнорируется ли путь."""
    ignored = False
    for rule in rules:
        if rule.dir_only and not is_dir:
            pass
        if rule.regex.match(rel_path):
            ignored = not rule.negated
    return ignored


# ---------------------------------------------------------------------------
# Проверка бинарности
# ---------------------------------------------------------------------------

def is_binary(file_path: Path) -> bool:
    if file_path.suffix.lower() in DEFAULT_BINARY_EXT:
        return True
    try:
        with file_path.open("rb") as f:
            chunk = f.read(4096)
        if b"\x00" in chunk:
            return True
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError:
            return False
    except OSError:
        return True
    return False


# ---------------------------------------------------------------------------
# Сбор файлов
# ---------------------------------------------------------------------------

def collect_files(root: Path, rules: list[IgnoreRule],
                  skip_binary: bool) -> list[Path]:
    """Рекурсивно собирает файлы, применяя правила игнора."""
    result = []

    for dirpath, dirnames, filenames in os.walk(root):
        current_dir = Path(dirpath)
        rel_dir = current_dir.relative_to(root) if current_dir != root else Path(".")

        pruned = []
        for d in sorted(dirnames):
            rel = (rel_dir / d).as_posix() if rel_dir != Path(".") else d
            if is_ignored(rel, is_dir=True, rules=rules):
                continue
            pruned.append(d)
        dirnames[:] = pruned

        for name in sorted(filenames):
            fp = current_dir / name
            rel = fp.relative_to(root)
            rel_posix = rel.as_posix()

            if is_ignored(rel_posix, is_dir=False, rules=rules):
                continue

            if skip_binary and is_binary(fp):
                continue

            result.append(fp)

    return result


def group_files_by_top_dir(files: list[Path], root: Path) -> dict[str, list[Path]]:
    """Разбивает файлы на группы по первой части относительного пути."""
    groups: dict[str, list[Path]] = {}
    for fp in files:
        try:
            rel = fp.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) == 1:
            key = ""
        else:
            key = parts[0]
        groups.setdefault(key, []).append(fp)
    return groups


# ---------------------------------------------------------------------------
# Запись
# ---------------------------------------------------------------------------

def write_file_content(out, file_path: Path, root: Path) -> int:
    try:
        rel = file_path.relative_to(root)
    except ValueError:
        rel = file_path

    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        out.write(f"\n===== FILE: {rel} (ОШИБКА ЧТЕНИЯ: {e}) =====\n")
        return 0

    header = f"\n===== FILE: {rel.as_posix()} =====\n"
    footer = "\n===== END FILE =====\n"

    out.write(header)
    out.write(content)
    if not content.endswith("\n"):
        out.write("\n")
    out.write(footer)

    return len(header.encode("utf-8")) + len(content.encode("utf-8")) + len(footer.encode("utf-8"))


class BundleWriter:
    """Пишет набор файлов в один или несколько выходных бандлов."""

    def __init__(self, output_path: Path, max_size_bytes: int):
        self.output_path = output_path
        self.max_size_bytes = max_size_bytes
        self.part_idx = 1
        self.current_size = 0
        self.out = None
        self.total_written = 0
        self.written_files = 0
        self.created_paths: list[Path] = []

        if max_size_bytes > 0:
            self.stem = output_path.stem
            self.suffix = output_path.suffix or ".txt"
            self.out_dir = output_path.parent
            self.out_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.stem = ""
            self.suffix = ""
            self.out_dir = None
            output_path.parent.mkdir(parents=True, exist_ok=True)

    def _open_new_part(self):
        if self.out is not None:
            self.out.close()
        if self.max_size_bytes > 0:
            name = f"{self.stem}_part{self.part_idx:03d}{self.suffix}"
            path = self.out_dir / name
            self.part_idx += 1
        else:
            path = self.output_path
        self.out = path.open("w", encoding="utf-8")
        self.current_size = 0
        self.created_paths.append(path)
        print(f"→ Запись: {path}")

    def add_files(self, files: list[Path], root: Path):
        if not files:
            return
        if self.out is None:
            self._open_new_part()
        for fp in files:
            try:
                fsize = fp.stat().st_size
            except OSError:
                fsize = 0
            estimated = fsize + 80

            if (self.max_size_bytes > 0 and self.current_size > 0
                    and self.current_size + estimated > self.max_size_bytes):
                self._open_new_part()

            written = write_file_content(self.out, fp, root)
            self.current_size += written
            self.total_written += written
            self.written_files += 1

    def close(self):
        if self.out is not None:
            self.out.close()
            self.out = None


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Склеивает файлы из директории в один/несколько файлов.",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"merge_files {__version__}",
        help="Показать версию скрипта и выйти",
    )
    parser.add_argument("source", help="Исходная директория")
    parser.add_argument("output", help="Выходной файл (или префикс при разбиении)")
    parser.add_argument("--max-size", type=float, default=0,
                        help="Максимальный размер одного файла в МБ (0 = без ограничения)")
    parser.add_argument("--ignore", default=".gitignore",
                        help="Файл с правилами игнорирования (по умолчанию .gitignore)")
    parser.add_argument("--include-binary", action="store_true",
                        help="Включать бинарные файлы")
    parser.add_argument("--no-ignore", action="store_true",
                        help="Не использовать файл игнорирования")
    parser.add_argument("--no-private-ignore", action="store_true",
                        help="Не обрабатывать приватные правила (#!#...) — "
                             "трактовать их как обычные комментарии")
    parser.add_argument("--split-by-dir", action="store_true",
                        help="Каждая папка верхнего уровня получает свой бандл; "
                             "в корневой bundle попадают только файлы, лежащие прямо в корне")
    args = parser.parse_args()

    source = Path(args.source).resolve()
    if not source.is_dir():
        print(f"Ошибка: {source} не является директорией")
        sys.exit(1)

    rules: list[IgnoreRule] = []
    if not args.no_ignore:
        ignore_path = Path(args.ignore)
        if not ignore_path.is_absolute():
            ignore_path = source / ignore_path
        if ignore_path.exists():
            rules = parse_ignore_file(
                ignore_path, allow_private=not args.no_private_ignore,
            )
            n_private = sum(1 for r in rules if r.private)
            n_regular = len(rules) - n_private
            print(f"Правил из {ignore_path}: {n_regular} обычных, {n_private} приватных")
        elif args.ignore != ".gitignore":
            print(f"Предупреждение: файл {ignore_path} не найден")

    files = collect_files(source, rules, skip_binary=not args.include_binary)

    if not files:
        print("Не найдено подходящих файлов.")
        sys.exit(0)

    print(f"Найдено файлов для обработки: {len(files)}")

    max_size_bytes = int(args.max_size * 1024 * 1024) if args.max_size > 0 else 0
    output_path = Path(args.output)

    grand_total_bytes = 0
    grand_total_files = 0

    if args.split_by_dir:
        groups = group_files_by_top_dir(files, source)
        keys = [""] + sorted(k for k in groups if k != "")

        for key in keys:
            group_files = groups.get(key, [])
            if not group_files:
                continue

            if key == "":
                target = output_path
                label = "корень"
            else:
                if output_path.suffix:
                    target = output_path.with_name(
                        f"{output_path.stem}_{key}{output_path.suffix}"
                    )
                else:
                    target = output_path.with_name(f"{output_path.name}_{key}")
                label = key

            print(f"\n[{label}] файлов: {len(group_files)}")
            writer = BundleWriter(target, max_size_bytes)
            writer.add_files(group_files, source)
            writer.close()
            grand_total_bytes += writer.total_written
            grand_total_files += writer.written_files

        print(f"\nГотово. Всего записано файлов: {grand_total_files}, "
              f"общий объём: {grand_total_bytes / (1024 * 1024):.2f} МБ")
    else:
        writer = BundleWriter(output_path, max_size_bytes)
        writer.add_files(files, source)
        writer.close()

        print(f"Готово. Записано файлов: {writer.written_files}, "
              f"общий объём: {writer.total_written / (1024 * 1024):.2f} МБ")


if __name__ == "__main__":
    main()