"""最小依存環境でもSQLiteスナップショットから28列のCSV/Excel出力を検証する。"""
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
import json
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openpyxl import load_workbook
from src.io.input_loader import load_table
from src.io.output_writer import csv_bytes
from src.master.comdesk import COMDESK_HEADERS, infer_comdesk_columns
from src.master.filters import Filters
from src.master.fixed_export import export_fixed, fixed_row


class SnapshotStore:
    """本番DBの出力に必要な既存スナップショットを読み取るアダプター。"""
    def __init__(self, path):
        self.path = path
        with self.connect() as connection:
            connection.executescript('''
                CREATE TABLE clinics(id INTEGER PRIMARY KEY, uuid TEXT, effective_json TEXT,
                    merged_into INTEGER, active INTEGER DEFAULT 0, hp_status TEXT DEFAULT 'UNRESEARCHED', hp_url TEXT DEFAULT '');
                CREATE TABLE templates(id TEXT PRIMARY KEY, headers_json TEXT, mapping_json TEXT);
                CREATE TABLE comdesk_original_rows(id INTEGER PRIMARY KEY, clinic_id INTEGER,
                    template_id TEXT, row_json TEXT, uuid TEXT);
            ''')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _get(self, connection, cid):
        row = connection.execute('SELECT * FROM clinics WHERE id=?', (cid,)).fetchone()
        return {**json.loads(row['effective_json']), 'id': cid, 'uuid': row['uuid']}

    def add(self, data, headers=None, raw=None):
        with self.connect() as connection:
            cid = connection.execute('INSERT INTO clinics(uuid,effective_json) VALUES(?,?)',
                (data.get('uuid', ''), json.dumps(data))).lastrowid
            if headers is not None:
                table = load_table(csv_bytes(headers, [raw]), 'fixture.csv')
                mapping = infer_comdesk_columns(table)
                connection.execute('INSERT INTO templates VALUES(?,?,?)',
                    (str(cid), json.dumps(headers), json.dumps(mapping)))
                connection.execute('INSERT INTO comdesk_original_rows(clinic_id,template_id,row_json,uuid) VALUES(?,?,?,?)',
                    (cid, str(cid), json.dumps(raw), data.get('uuid', '')))


def main():
    checks = 0
    filters = Filters(active_only=False, hp_only=False)
    with tempfile.TemporaryDirectory() as temporary:
        store = SnapshotStore(str(Path(temporary) / 'snapshot.db'))

        def verify(expected_rows, selected_filters=filters):
            nonlocal checks
            with store.connect() as connection:
                before = list(connection.iterdump())
            files = export_fixed(store, selected_filters)
            for filename, content in files.items():
                output = load_table(content, filename)
                assert output.headers == COMDESK_HEADERS
                assert output.data.values.tolist() == expected_rows
                checks += 1
            with store.connect() as connection:
                assert list(connection.iterdump()) == before
            checks += 1
            return files

        verify([])
        expected_rows = []
        for heading in ['クリニック名', '名前', '医院名']:
            raw = ['架空' + heading + '医院', '見本 太郎']
            store.add({'clinic_name': raw[0], 'manager_name': raw[1]}, [heading, '院長名'], raw)
            row = [''] * 28
            row[2], row[26] = raw
            expected_rows.append(row)
        verify(expected_rows)

        data = {'clinic_name': '厚生局だけの架空医院', 'manager_name': '見本 一郎',
            'address': '東京都架空区1-2-3', 'phone': '0312345678',
            'designation_date': '2025-01-01', 'hp_status': 'REVIEW', 'hp_url': 'https://candidate.example/'}
        store.add(data)
        row = [''] * 28
        row[2], row[5], row[6], row[9], row[26] = data['clinic_name'], '東京都', '架空区1-2-3', data['phone'], data['manager_name']
        expected_rows.append(row)
        verify(expected_rows)

        raw = [f'元値{i}' for i in range(28)]
        raw[0], raw[2], raw[4], raw[5], raw[6] = '000001', '全項目の架空医院', '0010001', '東京都', '架空区9-9'
        raw[9], raw[15], raw[18], raw[26] = '0311110000', '  元の備考\n改行  ', '=1+1', ''
        store.add({'uuid': raw[0], 'clinic_name': raw[2], 'manager_name': '上書きしない院長',
            'phone': '0399999999', 'hp_status': 'VERIFIED', 'hp_url': 'https://replace.example/'}, COMDESK_HEADERS, raw)
        expected_rows.append(raw)
        files = verify(expected_rows)
        workbook = load_workbook(BytesIO(files['final_comdesk_import.xlsx']))
        assert workbook.active['S6'].value == '=1+1' and workbook.active['S6'].data_type == 's'
        checks += 1
        verify([], Filters())  # 未確認医院を営業対象として勝手に通さない。
        try:
            fixed_row({}, ['住所１', '住所1'], {}, ['住所A', '住所B'])
        except ValueError:
            checks += 1
        else:
            raise AssertionError('矛盾する重複見出しを検出できていない')
    print(f'PASS: {checks} checks (fixed CSV/XLSX headers, names, original values, empty output, filters, unchanged DB)')


if __name__ == '__main__':
    main()
