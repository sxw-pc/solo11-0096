import json
import sqlite3
from pathlib import Path

import pytest

from dimcheck.sampling import inspect, register


FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'


def test_registered_design_replays_and_preserves_strata(tmp_path):
    manifest = FIXTURES / 'sample-design.json'
    sample = FIXTURES / 'sample-ridership.csv'
    ledger = tmp_path / 'sample.sqlite3'
    receipt = register(manifest, sample, ledger)
    assert receipt == register(manifest, sample, ledger)
    assert [(x['name'], x['population_size'], x['sample_size']) for x in receipt['strata']] == [
        ('weekday', 20, 18), ('weekend', 10, 9)]
    with sqlite3.connect(ledger) as db:
        assert db.execute('SELECT count(*) FROM sample_design').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM sample_stratum').fetchone()[0] == 2


def test_selection_and_sample_file_are_independently_checked(tmp_path):
    original = json.loads((FIXTURES / 'sample-design.json').read_text(encoding='utf-8'))
    original['strata'][0]['sample_ids'][0] = 'E04'
    wrong = tmp_path / 'wrong.json'
    wrong.write_text(json.dumps(original), encoding='utf-8')
    with pytest.raises(ValueError, match='sample_selection_unverifiable'):
        inspect(wrong, FIXTURES / 'sample-ridership.csv')
    csv = tmp_path / 'wrong.csv'
    csv.write_text((FIXTURES / 'sample-ridership.csv').read_text(encoding='utf-8').replace(
        'E16,1,1,1', 'E04,1,1,1'), encoding='utf-8')
    with pytest.raises(ValueError, match='sample_csv_selection_mismatch'):
        inspect(FIXTURES / 'sample-design.json', csv)
