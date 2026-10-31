"""Capability probe: canned machines (macOS, Linux, WSL, bare), no real calls."""
from pathlib import Path

from companion import capability_probe, test_markers

REQ = "numpy>=1.24\npython-dotenv>=1.0  # comment stripped\npsycopg[binary]>=3.1\nmissing-pkg==1.0\n"


def fake_run(responses):
    """run(argv) -> canned stdout by command prefix; git keys ignore the -C path."""
    def run(argv):
        argv = list(argv)
        key = ("git", argv[3]) if argv[:2] == ["git", "-C"] else tuple(argv[:2])
        return responses.get(key, responses.get(tuple(argv)))
    return run


def make_repo(tmp_path, data=False, requirements=True):
    if data:
        (tmp_path / "data").mkdir()
    if requirements:
        (tmp_path / "requirements.txt").write_text(REQ, encoding="utf-8")
    return tmp_path


def probe_machine(repo, **overrides):
    base = dict(
        run=fake_run({}), env={}, which=lambda name: None, loopback_check=lambda: False,
        find_spec=lambda name: object() if name in {"numpy", "dotenv"} else None,
        cpu_count=lambda: None, total_ram_bytes=lambda: None,
    )
    base.update(overrides)
    return capability_probe.probe(repo, **base)


def test_macos_laptop(tmp_path):
    repo = make_repo(tmp_path, data=True)
    facts = probe_machine(
        repo, system="Darwin", machine="arm64",
        run=fake_run({
            ("sysctl", "-n", "hw.ncpu"): "8\n",
            ("sysctl", "-n", "hw.memsize"): "17179869184\n",
            ("system_profiler", "SPDisplaysDataType"):
                "Graphics/Displays:\n    Example GPU:\n      Chipset Model: Example GPU\n      VRAM (Total): 8 GB\n",
            ("git", "rev-list"): "1204\n",
        }),
        which=lambda name: f"/usr/bin/{name}" if name != "ruff" else None,
        loopback_check=lambda: True,
    )
    assert facts["os"] == "macos" and facts["arch"] == "arm64"
    assert facts["cores"] == 8 and facts["ram_gb"] == 16.0
    # nvidia-smi absent on this OS, so the Metal path reports the GPU.
    assert facts["gpus"] == [{"name": "Example GPU", "vram_gb": 8.0}]
    assert facts["git_depth"] == 1204 and facts["git_history"] is True
    assert facts["tools"] == ["git", "node", "npm", "pytest", "python3", "ssh"]
    assert facts["packages"] == {"numpy": True, "python-dotenv": True, "psycopg": False, "missing-pkg": False}
    caps = capability_probe.to_capabilities(facts)
    assert caps == test_markers.Capabilities(
        os="macos", reach="local", may_see_real_data=True, git_history=True,
        loopback=True, gpu_vram_gb=8.0, timing_safe=False)
    assert test_markers.missing_capabilities(
        ["needs_macos", "needs_real_data", "needs_gpu"], caps) == ()


def test_linux_box(tmp_path):
    repo = make_repo(tmp_path)  # no data/ on this runner
    facts = probe_machine(
        repo, system="Linux", machine="x86_64",
        run=fake_run({
            ("cat", "/proc/version"): "Linux version 6.8.0-generic\n",
            ("cat", "/proc/meminfo"): "MemTotal:       67108864 kB\nMemFree: 1 kB\n",
            ("nproc",): "32\n",
            ("nvidia-smi", "--query-gpu=name,memory.total"): "Example RTX, 24564\nExample RTX, 24564\n",
            ("git", "rev-list"): "1\n",  # a one-commit archive checkout
        }),
        which=lambda name: "/usr/bin/" + name, loopback_check=lambda: True,
    )
    assert facts["os"] == "linux" and facts["arch"] == "x86_64"
    assert facts["cores"] == 32 and facts["ram_gb"] == 64.0
    assert len(facts["gpus"]) == 2 and facts["gpu_vram_gb"] == 23.988
    caps = capability_probe.to_capabilities(facts)
    assert caps.git_history is False and caps.may_see_real_data is False
    assert test_markers.missing_capabilities(["needs_git_history", "needs_real_data"], caps) == (
        "needs_git_history", "needs_real_data")
    assert test_markers.missing_capabilities(["needs_gpu", "needs_network_loopback"], caps) == ()


def test_wsl_on_windows(tmp_path):
    repo = make_repo(tmp_path, requirements=False)
    facts = probe_machine(
        repo, system="Linux", machine="AMD64", env={"WSL_DISTRO_NAME": "ExampleDistro"},
        run=fake_run({
            ("cat", "/proc/meminfo"): "MemTotal:       33554432 kB\n",
            ("nproc",): "16\n",
            ("nvidia-smi", "--query-gpu=name,memory.total"): "Example Laptop GPU, 8192\n",
            ("git", "rev-list"): "57\n",
        }),
    )
    assert facts["os"] == "linux-wsl2" and facts["arch"] == "x86_64"
    assert facts["cores"] == 16 and facts["ram_gb"] == 32.0
    assert facts["gpu_vram_gb"] == 8.0 and facts["packages"] == {}
    assert facts["loopback"] is False  # injected check failed, reported as a fact


def test_wsl_detected_from_proc_version_without_env(tmp_path):
    facts = probe_machine(
        make_repo(tmp_path, requirements=False), system="Linux", machine="x86_64",
        run=fake_run({("cat", "/proc/version"): "Linux version 5.15 microsoft-standard-WSL2\n"}),
    )
    assert facts["os"] == "linux-wsl2"


def test_nothing_installed(tmp_path):
    facts = probe_machine(make_repo(tmp_path, requirements=False), system="Linux", machine="")
    assert facts["os"] == "linux" and facts["arch"] == "unknown"
    assert facts["cores"] == 0 and facts["ram_gb"] == 0.0
    assert facts["tools"] == [] and facts["packages"] == {} and facts["gpus"] == []
    assert facts["git_depth"] == 0
    caps = capability_probe.to_capabilities(facts)
    assert caps == test_markers.Capabilities(os="linux", reach="local")
    for marker in test_markers.MARKERS:
        assert test_markers.missing_capabilities([marker], caps) == (marker,)


def test_requirements_parsing_and_import_names():
    text = "# top\n-r other.txt\nNumpy>=1\npython-dotenv\npsycopg[binary]>=3; python_version>'3'\nnumpy\n"
    assert capability_probe.parse_requirements(text) == ["numpy", "python-dotenv", "psycopg"]
    assert capability_probe.import_name("python-dotenv") == "dotenv"
    assert capability_probe.import_name("pyjwt") == "jwt"
    assert capability_probe.import_name("some-pkg") == "some_pkg"


def test_fallbacks_when_os_tools_are_missing(tmp_path):
    facts = probe_machine(
        make_repo(tmp_path, requirements=False), system="Darwin", machine="arm64",
        cpu_count=lambda: 4, total_ram_bytes=lambda: 8 * 1024**3,
    )
    assert facts["cores"] == 4 and facts["ram_gb"] == 8.0


def test_probe_never_raises_on_a_broken_loopback_check(tmp_path):
    def broken():
        raise OSError("no loopback here")
    facts = probe_machine(make_repo(tmp_path, requirements=False), system="Linux",
                          machine="x86_64", loopback_check=broken)
    assert facts["loopback"] is False


def test_capabilities_entry_is_exactly_the_dataclass_fields(tmp_path):
    facts = probe_machine(make_repo(tmp_path, requirements=False), system="Linux", machine="x86_64")
    assert set(facts["capabilities"]) == set(capability_probe.CAPABILITY_KEYS)
    assert test_markers.Capabilities(**facts["capabilities"]) == capability_probe.to_capabilities(facts)


def test_modules_contain_no_machine_identity_lookups():
    root = Path(__file__).resolve().parents[1]
    forbidden = ["gethost" + "name", "getlog" + "in", "getus" + "er", "platform.node",
                 "HOST" + "NAME", "LOG" + "NAME", "expandus" + "er", '"USER"']
    for rel in ("src/companion/capability_probe.py", "src/companion/runner_bootstrap.py"):
        text = (root / rel).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{rel} contains {token}"
