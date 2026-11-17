from pathlib import Path

import pytest

from dimcheck.tap_capture import register

FIX = Path(__file__).resolve().parents[1]/'fixtures'


def test_freeze_synthetic_taps(tmp_path):
    output = tmp_path/'capture.json'
    result = register(FIX/'tap-events.jsonl', FIX/'dim_station.csv',
                      FIX/'transfer-links.json', output)
    assert result['tap_count'] == 4 and result['link_count'] == 1
    assert register(FIX/'tap-events.jsonl', FIX/'dim_station.csv',
                    FIX/'transfer-links.json', output) == result


def test_changed_source_cannot_reuse_capture(tmp_path):
    output = tmp_path/'capture.json'
    register(FIX/'tap-events.jsonl', FIX/'dim_station.csv',
             FIX/'transfer-links.json', output)
    changed = tmp_path/'changed.jsonl'
    changed.write_text((FIX/'tap-events.jsonl').read_text().replace('08:25:00Z','08:26:00Z'))
    with pytest.raises(ValueError, match='capture_identity_conflict'):
        register(changed, FIX/'dim_station.csv', FIX/'transfer-links.json', output)
