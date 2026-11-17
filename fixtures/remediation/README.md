# AFC repair case

`case.json` freezes the original contract and all eight declared source files, then appends three fact rows in two fact tables. Station keys 98 and 99 are absent from `dim_station`. The independent dimension supplements supply those stations, but station 98 refers to line 5, so a station-only repair leaves a snowflake orphan. Removing the three appended fact rows and applying both dimension supplements are separate valid interventions with different preserved coverage.

Run `PYTHONPATH=src python -B -m dimcheck.remediation fixtures/remediation/case.json` (PowerShell: `$env:PYTHONPATH='src'; python -B -m dimcheck.remediation fixtures/remediation/case.json`) to verify every case input digest and replay key, reference and fan-out checks on the observed defective state. This command does not change any source file.
