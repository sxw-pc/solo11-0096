import pytest

from dimcheck.contract import ContractError, load_contract


def _codes(exc):
    return [item.code for item in exc.value.findings]


def test_loads_normalized_contract(project):
    contract = load_contract(project.contract)
    assert contract.name == "test_dw"
    assert contract.run_epoch == 1767225600
    assert contract.max_rows == 1000
    assert [item.name for item in contract.dimensions] == [
        "dim_customer", "dim_product", "dim_date"
    ]
    assert contract.facts[0].grain == ("sale_id",)
    assert contract.facts[0].refs[0].role == "buyer"
    assert len(contract.fingerprint) == 64


def test_fingerprint_is_stable_and_input_sensitive(project):
    first = load_contract(project.contract)
    second = load_contract(project.contract)
    assert first.fingerprint == second.fingerprint
    project.model["dataset"]["name"] = "another_dw"
    project.save()
    third = load_contract(project.contract)
    assert third.fingerprint != first.fingerprint


def test_missing_contract_path_is_coded(tmp_path):
    with pytest.raises(ContractError) as exc:
        load_contract(tmp_path / "absent.yaml")
    assert _codes(exc) == ["DIM-PATH-002"]


def test_unsupported_schema_version(project):
    project.model["schema_version"] = 2
    project.save()
    with pytest.raises(ContractError) as exc:
        load_contract(project.contract)
    assert _codes(exc) == ["DIM-CONTRACT-004"]


def test_unknown_dimension_reference(project):
    project.model["facts"][0]["refs"][0]["dimension"] = "dim_missing"
    project.save()
    with pytest.raises(ContractError) as exc:
        load_contract(project.contract)
    assert _codes(exc) == ["DIM-CONTRACT-003"]


def test_duplicate_dimension_name(project):
    project.model["dimensions"].append(dict(project.model["dimensions"][0]))
    project.save()
    with pytest.raises(ContractError) as exc:
        load_contract(project.contract)
    assert _codes(exc) == ["DIM-CONTRACT-002"]


def test_duplicate_yaml_key(project):
    text = project.contract.read_text(encoding="utf-8")
    project.contract.write_text(
        text.replace("schema_version: 1\n", "schema_version: 1\nschema_version: 1\n", 1),
        encoding="utf-8",
    )
    with pytest.raises(ContractError) as exc:
        load_contract(project.contract)
    assert _codes(exc) == ["DIM-CONTRACT-002"]


def test_malformed_yaml(project):
    project.contract.write_text("dimensions: [\n", encoding="utf-8")
    with pytest.raises(ContractError) as exc:
        load_contract(project.contract)
    assert _codes(exc) == ["DIM-CONTRACT-001"]


def test_network_or_sensitive_dataset_root_rejected(project):
    for root in ("//server/share", "/etc"):
        project.model["dataset"]["root"] = root
        project.save()
        with pytest.raises(ContractError) as exc:
            load_contract(project.contract)
        assert _codes(exc) == ["DIM-PATH-001"]


def test_dataset_root_must_exist(project):
    project.model["dataset"]["root"] = "./missing"
    project.save()
    with pytest.raises(ContractError) as exc:
        load_contract(project.contract)
    assert _codes(exc) == ["DIM-PATH-002"]
    assert exc.value.findings[0].object_name == "$.dataset.root"


def test_duplicate_attribute_name(project):
    project.model["dimensions"][0]["attributes"].append(
        {"name": "customer_key", "type": "integer"}
    )
    project.save()
    with pytest.raises(ContractError) as exc:
        load_contract(project.contract)
    assert _codes(exc) == ["DIM-CONTRACT-002"]


def test_unknown_ratio_component(project):
    ratio = project.model["facts"][0]["measures"][3]
    ratio["denominator"] = "missing_measure"
    project.save()
    with pytest.raises(ContractError) as exc:
        load_contract(project.contract)
    assert _codes(exc) == ["DIM-CONTRACT-003"]


def test_unsupported_source_format(project):
    project.model["dimensions"][0]["format"] = "parquet"
    project.save()
    with pytest.raises(ContractError) as exc:
        load_contract(project.contract)
    assert _codes(exc) == ["DIM-CONTRACT-001"]


def test_duplicate_measure_name(project):
    measures = project.model["facts"][0]["measures"]
    measures.append(dict(measures[0]))
    project.save()
    with pytest.raises(ContractError) as exc:
        load_contract(project.contract)
    assert _codes(exc) == ["DIM-CONTRACT-002"]
