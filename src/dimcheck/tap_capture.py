"""Freeze synthetic AFC tap observations against station natural identities."""
import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path


def _instant(value):
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError('tap_timezone_required')
    return moment


def register(taps_file, stations_file, links_file, out_file):
    taps_file = Path(taps_file).resolve()
    stations_file = Path(stations_file).resolve()
    links_file = Path(links_file).resolve()
    rows = [json.loads(line) for line in taps_file.read_text(encoding='utf-8').splitlines()
            if line.strip()]
    stations = {row['station_code'] for row in csv.DictReader(
        stations_file.open(encoding='utf-8', newline=''))}
    links = json.loads(links_file.read_text(encoding='utf-8'))
    seen = set()
    for row in rows:
        if (not row.get('event_id') or row['event_id'] in seen or
                not row.get('card_token') or row.get('kind') not in ('entry', 'exit') or
                row.get('station_code') not in stations or
                row.get('clock_error_seconds', -1) < 0):
            raise ValueError('bad_tap_observation')
        _instant(row['at'])
        seen.add(row['event_id'])
    for link in links:
        if (link['from_station'] not in stations or link['to_station'] not in stations or
                link['max_walk_seconds'] < 0):
            raise ValueError('bad_transfer_link')
    record = {'taps_sha256': hashlib.sha256(taps_file.read_bytes()).hexdigest(),
              'stations_sha256': hashlib.sha256(stations_file.read_bytes()).hexdigest(),
              'links_sha256': hashlib.sha256(links_file.read_bytes()).hexdigest(),
              'tap_count': len(rows), 'link_count': len(links)}
    out_file = Path(out_file).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    if out_file.exists():
        if json.loads(out_file.read_text(encoding='utf-8')) != record:
            raise ValueError('capture_identity_conflict')
    else:
        out_file.write_text(json.dumps(record, sort_keys=True), encoding='utf-8')
    return record
