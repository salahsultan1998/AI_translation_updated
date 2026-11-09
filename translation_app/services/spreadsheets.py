"""Read mapped spreadsheet data and export without blanking untranslated cells."""
import csv
from contextlib import contextmanager
from pathlib import Path
import shutil
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook


CSV_SHEET = "CSV_Sheet"
# Limits apply across selected workbook sheets, excluding their header rows.
MAX_ROWS = 100_000
MAX_COLUMNS = 500
MAX_SHEETS = 100
MAX_XLSX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_CSV_LINE_CHARS = 1_000_000


def _headers(values):
    if len(values) > MAX_COLUMNS:
        raise ValueError(f"文件列数不可超过 {MAX_COLUMNS} 列")
    headers = [str(value).strip() if value is not None else "" for value in values]
    nonempty = [value for value in headers if value]
    if not nonempty or len(nonempty) != len(set(nonempty)):
        raise ValueError("工作表表头为空或存在重复列名")
    return headers


@contextmanager
def _csv_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        def bounded_lines():
            for line in handle:
                if len(line) > MAX_CSV_LINE_CHARS:
                    raise ValueError("CSV 单行内容过长")
                yield line

        reader = csv.reader(bounded_lines())
        try:
            raw_headers = next(reader)
        except StopIteration:
            raise ValueError("文件为空") from None
        except csv.Error as exc:
            raise ValueError("CSV 格式无效或单元格内容过长") from exc
        headers = _headers(raw_headers)

        def limited_rows():
            try:
                for index, row in enumerate(reader):
                    if index >= MAX_ROWS:
                        raise ValueError(f"文件数据不可超过 {MAX_ROWS} 行")
                    if len(row) > MAX_COLUMNS:
                        raise ValueError(f"文件列数不可超过 {MAX_COLUMNS} 列")
                    yield index, row
            except csv.Error as exc:
                raise ValueError("CSV 格式无效或单元格内容过长") from exc
        yield raw_headers, headers, limited_rows()


def _open_workbook(path, **kwargs):
    try:
        with ZipFile(path) as archive:
            if sum(item.file_size for item in archive.infolist()) > MAX_XLSX_UNCOMPRESSED_BYTES:
                raise ValueError("工作簿解压后过大")
    except BadZipFile as exc:
        raise ValueError("XLSX 文件格式无效") from exc
    book = load_workbook(path, **kwargs)
    if len(book.sheetnames) > MAX_SHEETS:
        book.close()
        raise ValueError(f"工作表数量不可超过 {MAX_SHEETS}")
    return book


def _check_dimensions(sheet):
    if sheet.max_column is not None and sheet.max_column > MAX_COLUMNS:
        raise ValueError(f"文件列数不可超过 {MAX_COLUMNS} 列")
    if sheet.max_row is not None and sheet.max_row - 1 > MAX_ROWS:
        raise ValueError(f"文件数据不可超过 {MAX_ROWS} 行")


def _check_count(count):
    if count > MAX_ROWS:
        raise ValueError(f"文件数据不可超过 {MAX_ROWS} 行")


def _display(value):
    return "" if value is None else str(value)


def _preview_row(columns, row):
    return [_display(row[i] if i < len(row) else None) for i, name in enumerate(columns) if name]


def inspect_file(path):
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with _csv_rows(path) as (_, columns, rows):
            preview, count = [], 0
            for _, row in rows:
                count += 1
                if count <= 10:
                    preview.append(_preview_row(columns, row))
        return {"sheets": [{"name": CSV_SHEET, "columns": [x for x in columns if x],
                            "preview": preview, "row_count": count}]}
    if path.suffix.lower() != ".xlsx":
        raise ValueError("仅支持 CSV 和 XLSX 文件")
    book = _open_workbook(path, read_only=True, data_only=True)
    try:
        sheets, total = [], 0
        for sheet in book:
            _check_dimensions(sheet)
            rows = sheet.iter_rows(values_only=True)
            columns = _headers(next(rows, ()))
            preview, count = [], 0
            for row in rows:
                count += 1
                total += 1
                _check_count(total)
                if len(row) > MAX_COLUMNS:
                    raise ValueError(f"文件列数不可超过 {MAX_COLUMNS} 列")
                if count <= 10:
                    preview.append(_preview_row(columns, row))
            sheets.append({"name": sheet.title, "columns": [x for x in columns if x], "preview": preview, "row_count": count})
        return {"sheets": sheets}
    finally:
        book.close()


def _validate_mapping(headers, mapping):
    if not isinstance(mapping, dict):
        raise ValueError("列映射无效")
    source, context, target = (mapping.get(name) for name in ("source", "context", "target"))
    if not isinstance(source, str) or source not in headers:
        raise ValueError("源文本列不存在")
    if context and context not in headers:
        raise ValueError("上下文列不存在")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("请指定目标列")
    return headers.index(source), headers.index(context) if context else None, target.strip()


def read_mapped_rows(path, mappings):
    """Return {sheet: [(row index, source, context)]}, omitting blank sources."""
    path = Path(path)
    if not isinstance(mappings, dict) or not mappings:
        raise ValueError("请选择至少一个工作表")
    result = {}
    if path.suffix.lower() == ".csv":
        if set(mappings) != {CSV_SHEET}:
            raise ValueError("CSV 工作表映射无效")
        with _csv_rows(path) as (_, headers, rows):
            source_idx, context_idx, _ = _validate_mapping(headers, mappings[CSV_SHEET])
            def read_value(row, index):
                return _display(row[index]).strip() if index is not None and index < len(row) else ""
            result[CSV_SHEET] = [(i, read_value(row, source_idx), read_value(row, context_idx))
                                 for i, row in rows if read_value(row, source_idx)]
        return result
    if path.suffix.lower() != ".xlsx":
        raise ValueError("仅支持 CSV 和 XLSX 文件")
    book = _open_workbook(path, read_only=True, data_only=True)
    try:
        total = 0
        for name, mapping in mappings.items():
            if name not in book.sheetnames:
                raise ValueError(f"工作表不存在：{name}")
            _check_dimensions(book[name])
            rows = book[name].iter_rows(values_only=True)
            headers = _headers(next(rows, ()))
            source_idx, context_idx, _ = _validate_mapping(headers, mapping)
            values = []
            for i, row in enumerate(rows):
                total += 1
                _check_count(total)
                if len(row) > MAX_COLUMNS:
                    raise ValueError(f"文件列数不可超过 {MAX_COLUMNS} 列")
                source = _display(row[source_idx] if source_idx < len(row) else None).strip()
                context = _display(row[context_idx] if context_idx is not None and context_idx < len(row) else None).strip()
                if source:
                    values.append((i, source, context))
            result[name] = values
        return result
    finally:
        book.close()


def write_translations(original_path, output_path, mappings, results):
    """Only update cells with returned translations; preserve other cells and sheets."""
    original_path, output_path = Path(original_path), Path(output_path)
    if original_path.suffix.lower() == ".csv":
        with _csv_rows(original_path) as (original_headers, headers, rows):
            _, _, target = _validate_mapping(headers, mappings[CSV_SHEET])
            if target not in headers:
                if len(headers) >= MAX_COLUMNS:
                    raise ValueError(f"文件列数不可超过 {MAX_COLUMNS} 列")
                headers.append(target)
                original_headers.append(target)
            index = headers.index(target)
            translations = results.get(CSV_SHEET, {})
            remaining = set(translations)
            with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(original_headers)
                for row_id, row in rows:
                    if row_id in translations:
                        while len(row) <= index:
                            row.append("")
                        row[index] = translations[row_id]
                        remaining.remove(row_id)
                    writer.writerow(row)
            if remaining:
                raise ValueError("翻译行号无效")
        return
    if original_path.suffix.lower() != ".xlsx":
        raise ValueError("仅支持 CSV 和 XLSX 文件")
    shutil.copyfile(original_path, output_path)
    book = _open_workbook(output_path)
    try:
        for name, mapping in mappings.items():
            if name not in book.sheetnames:
                raise ValueError("工作表不存在")
            sheet = book[name]
            _check_dimensions(sheet)
            headers = _headers([cell.value for cell in sheet[1]])
            _, _, target = _validate_mapping(headers, mapping)
            if target in headers:
                index = headers.index(target) + 1
            else:
                if len(headers) >= MAX_COLUMNS:
                    raise ValueError(f"文件列数不可超过 {MAX_COLUMNS} 列")
                index = len(headers) + 1
                sheet.cell(1, index, target)
            for row_id, text in results.get(name, {}).items():
                if row_id < 0 or row_id + 2 > sheet.max_row:
                    raise ValueError("翻译行号无效")
                cell = sheet.cell(row_id + 2, index, text)
                # Model output is text, even when it begins with '='.
                cell.data_type = "s"
        book.save(output_path)
    finally:
        book.close()
