from pathlib import Path

from kyra_memory import synth


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_same_seed_is_byte_identical_and_names_follow_the_pattern(tmp_path):
    a = synth.generate(tmp_path / "a", seed=7, tenants=3)
    synth.generate(tmp_path / "b", seed=7, tenants=3)
    assert list(a["tenants"]) == ["tenant-0001", "tenant-0002", "tenant-0003"]
    assert tree_bytes(tmp_path / "a") == tree_bytes(tmp_path / "b")
    synth.generate(tmp_path / "c", seed=8, tenants=3)
    assert tree_bytes(tmp_path / "a") != tree_bytes(tmp_path / "c")


def test_refuses_a_non_empty_directory_and_leaves_it_alone(tmp_path):
    (tmp_path / "keep.txt").write_text("x")
    assert synth.main([str(tmp_path)]) == 2
    assert [p.name for p in tmp_path.iterdir()] == ["keep.txt"]


def test_module_entry_writes_only_under_the_given_path(tmp_path):
    out = tmp_path / "out"
    assert synth.main([str(out), "--tenants", "2", "--facts", "5"]) == 0
    assert {p.name for p in tmp_path.iterdir()} == {"out"}
    assert (out / "manifest.json").is_file()
